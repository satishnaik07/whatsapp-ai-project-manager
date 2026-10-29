"""
Main Python app: dashboard REST API + Socket.IO (live updates) + the
webhook receiver that the Node/Baileys bridge posts events to.

This replaces server/index.js and all the business logic that used to
live in sessionManager.js. The bridge (bridge/index.js) only knows about
the WhatsApp protocol; everything else — the database, the AI replier,
Google Calendar, and the dashboard's live updates — lives here.
"""
import asyncio
import os
import uuid
from datetime import datetime
from pathlib import Path

import socketio
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles

load_dotenv()

from server import db
from server import bridge_client
from server import group_db
from server import group_ai_replier
from server.ai_replier import generate_reply, is_relevant_topic, _load_history
from server.group_routes import router as pm_router
from server import crm_schema
from server.services import message_router, poc_router, notification_service, whatsapp_link
from server.routes import crm_router

PUBLIC_DIR = Path(__file__).resolve().parent.parent / "public"

# How long to wait after an inbound message before actually sending the AI's
# reply, so a human (account holder) typing at roughly the same time gets
# the chance to take over first instead of the two answers colliding.
AI_REPLY_DELAY_SECONDS = int(os.getenv("AI_REPLY_DELAY_SECONDS", "150"))

# Same idea for client-group conversations (the PM/CRM workflow), but a
# shorter window by default — this is what makes "Pause AI" actually
# useful: without a delay the AI could already have sent before anyone had
# a chance to click Pause.
GROUP_AI_REPLY_DELAY_SECONDS = int(os.getenv("GROUP_AI_REPLY_DELAY_SECONDS", "20"))

# This app actually has TWO separate AI personas sharing one WhatsApp
# connection:
#   1. The original 1:1 "sales qualification" bot (ai_replier.py) — replies
#      to ANY 1:1 chat on this account about a website/software topic,
#      whoever it's from. This has nothing to do with client groups.
#   2. The Project Manager / CRM bot (group_ai_replier.py) — only ever
#      replies inside a client group you've explicitly configured on the
#      "Client Groups" page, and only to members you've marked "Client".
# If you only want #2 (the PM workflow) and don't want this WhatsApp
# number auto-replying to random 1:1 chats at all, set
# ENABLE_1TO1_AI_REPLIER=false in .env. Configured/unconfigured *groups*
# were already unaffected by this flag either way — see webhook_message
# below: an unconfigured group is only ever logged, never auto-replied to.
ONE_TO_ONE_AI_ENABLED = os.getenv("ENABLE_1TO1_AI_REPLIER", "false").lower() not in ("false", "0", "no")

sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
api = FastAPI()
api.include_router(pm_router)
api.include_router(crm_router)


# ---------------------------------------------------------------------------
# REST API — same routes/shapes as the original Node app
# ---------------------------------------------------------------------------


@api.get("/api/accounts")
async def list_accounts():
    return await db.query(
        "SELECT id, phone_number, name, status, created_at FROM accounts ORDER BY created_at ASC"
    )


@api.post("/api/accounts")
async def add_account():
    """Starts linking a new WhatsApp account. Nothing is written to the DB
    here — the account only becomes real once WhatsApp actually confirms
    the pairing (see the /webhook/status handler below)."""
    account_id = str(uuid.uuid4())
    await bridge_client.start_session(account_id)
    return {"accountId": account_id}


@api.delete("/api/accounts/{account_id}")
async def remove_one_account(account_id: str):
    await bridge_client.remove_session(account_id)
    await db.delete_account(account_id)
    await sio.emit("accounts:updated")
    return {"removed": account_id}


@api.delete("/api/accounts")
async def remove_all_accounts():
    rows = await db.query("SELECT id FROM accounts")
    for row in rows:
        await bridge_client.remove_session(row["id"])
        await db.delete_account(row["id"])
    await sio.emit("accounts:updated")
    return {"removed": len(rows)}


@api.get("/api/accounts/{account_id}/contacts")
async def account_contacts(account_id: str):
    return await db.query(
        """
        SELECT c.id, c.jid, c.name,
          (SELECT MAX(m.created_at) FROM messages m WHERE m.contact_id = c.id) AS last_message_at
        FROM contacts c
        WHERE c.account_id = %s
        ORDER BY COALESCE(last_message_at, c.created_at) DESC
        """,
        (account_id,),
    )


@api.get("/api/contacts/{contact_id}/messages")
async def contact_messages(contact_id: int):
    return await db.query(
        "SELECT id, direction, body, ai_generated, created_at FROM messages "
        "WHERE contact_id = %s ORDER BY id ASC",
        (contact_id,),
    )


@api.post("/api/send")
async def send_message_api(payload: dict):
    """
    Business-initiated (first-touch) send: posts a message to WhatsApp even
    if this contact has never messaged in before. Creates the thread
    (contacts row) if it doesn't exist yet, and returns BOTH ids:
      - thread_id  -> contacts.id, identifies the whole conversation
      - message_id -> messages.id, identifies this one message
    Body: { "account_id": str, "jid": str, "text": str, "name"?: str }
    "jid" is the recipient's WhatsApp id, e.g. "919876543210@s.whatsapp.net".
    """
    account_id = payload.get("account_id")
    jid = payload.get("jid")
    text = payload.get("text")
    name = payload.get("name")

    if not account_id or not jid or not text:
        raise HTTPException(status_code=400, detail="account_id, jid and text are required")

    # Creates the contact/thread now if this is genuinely the first time
    # we're talking to this number — same call the webhook uses, so an
    # existing thread is reused instead of duplicated.
    contact_id = await db.upsert_contact(account_id, jid, name)

    sent = await bridge_client.send_message(account_id, jid, text)
    if not sent:
        raise HTTPException(status_code=502, detail="WhatsApp send failed (bridge/session issue)")

    message_id = await db.save_message(account_id, contact_id, "out", text, False)
    # Business-initiated, not the AI — mark it like any manual/human send so
    # the AI doesn't also jump in and reply to its own outbound message.
    await db.mark_human_reply(contact_id)

    await sio.emit(
        f"message:{account_id}",
        {"contactId": contact_id, "jid": jid, "direction": "out", "body": text, "aiGenerated": False},
    )
    await sio.emit("accounts:updated")

    return {"thread_id": contact_id, "message_id": message_id}


@api.get("/api/contacts/all")
async def all_contacts():
    return await db.query(
        """
        SELECT c.id, c.jid, c.name, c.account_id, a.phone_number, a.name AS account_name,
          (SELECT MAX(m.created_at) FROM messages m WHERE m.contact_id = c.id) AS last_message_at
        FROM contacts c
        JOIN accounts a ON a.id = c.account_id
        ORDER BY COALESCE(last_message_at, c.created_at) DESC
        """
    )


# ---------------------------------------------------------------------------
# Webhooks — the bridge posts every WhatsApp event here
# ---------------------------------------------------------------------------


@api.post("/webhook/qr")
async def webhook_qr(payload: dict):
    whatsapp_link.on_qr(payload["accountId"], payload["dataUrl"])
    await sio.emit(f"qr:{payload['accountId']}", {"dataUrl": payload["dataUrl"]})
    return {"ok": True}


@api.post("/webhook/status")
async def webhook_status(payload: dict):
    account_id = payload["accountId"]
    status = payload["status"]

    if status == "connected":
        await db.upsert_account_connected(account_id, payload.get("phone"), payload.get("name"))
        await whatsapp_link.on_connected(account_id)   # maps the account to the user who started linking
        await sio.emit(f"status:{account_id}", {"status": "connected", "phone": payload.get("phone")})
        await sio.emit("accounts:updated")
    elif status == "pending_qr":
        await db.update_account_status(account_id, "pending_qr")
        await sio.emit(f"status:{account_id}", {"status": "pending_qr"})
    elif status == "disconnected":
        await db.update_account_status(account_id, "disconnected")
        await sio.emit(f"status:{account_id}", {"status": "disconnected"})
        await sio.emit("accounts:updated")
    # "reconnecting" is a transient state (e.g. Baileys' 515 "restart
    # required" right after a QR scan) — deliberately not persisted.

    return {"ok": True}


async def _reply_after_delay(account_id: str, jid: str, contact_id: int, body: str):
    """Runs in the background so the webhook can return immediately. Waits
    a bit before replying so a human typing at the same moment can take
    over first; re-checks right before sending in case they did."""
    try:
        wait_started_at = datetime.now()
        await asyncio.sleep(AI_REPLY_DELAY_SECONDS)
        if await db.human_replied_since(contact_id, wait_started_at):
            # Account holder took over while we were waiting — back off.
            return

        reply, meeting_booked = await generate_reply(account_id, contact_id, body)
        sent = await bridge_client.send_message(account_id, jid, reply)
        if sent:
            await db.save_message(account_id, contact_id, "out", reply, True)
            await sio.emit(
                f"message:{account_id}",
                {"contactId": contact_id, "jid": jid, "direction": "out", "body": reply, "aiGenerated": True},
            )
            if meeting_booked:
                await db.set_contact_status(contact_id, "closed")
    except Exception as err:  # noqa: BLE001
        print(f"[webhook_message] AI reply failed: {err}")


async def _send_group_ai_reply_after_delay(
    account_id: str, jid: str, group: dict, thread_id: int, client_message_id: int | None, draft: str
):
    """Runs in the background so the webhook can return immediately.
    Waits GROUP_AI_REPLY_DELAY_SECONDS, then re-checks right before
    sending: has a human (account holder / POC manual reply) replied in
    this thread meanwhile, or has someone clicked Pause AI? Either one
    means back off and don't send the stale draft."""
    try:
        wait_started_at = datetime.now()
        await asyncio.sleep(GROUP_AI_REPLY_DELAY_SECONDS)

        thread = await group_db.get_thread(thread_id)
        if not thread or thread.get("ai_paused") or thread.get("status") != "open":
            return  # paused, or the thread moved on while we were waiting
        if await group_db.human_replied_in_thread_since(thread_id, wait_started_at):
            return  # a human took over this conversation while we waited

        sent, used_account = await poc_router.send_with_fallback(group, thread, draft)
        out_id = await group_db.save_group_message(
            thread_id=thread_id, group_id=group["id"], whatsapp_message_id=None,
            sender_jid=None, sender_name="AI Project Manager", sender_role="ai",
            direction="out", body=draft, processing_status="processed",
            delivery_status="sent" if sent else "failed",
        )
        if thread.get("assigned_poc_id"):
            await notification_service.notify(thread["assigned_poc_id"], "ai_reply_sent", "AI reply sent to client",
                                              None, thread_id, client_message_id, out_id)
        else:
            await group_db.create_notification(thread_id, group.get("assigned_poc_id"), client_message_id, out_id)
        if client_message_id is not None:
            await group_db.update_message_status(client_message_id, processing_status="processed")
        await sio.emit(f"pm:message:{group['id']}", {"threadId": thread_id, "role": "ai", "body": draft})
        if not sent:
            print(f"[group] WhatsApp send failed for group {group['whatsapp_group_id']}, thread {thread_id}")
            # tell the responsible person (and the Main Manager): the client has NOT received this reply
            from server.repositories import crm as _crm
            proj = await _crm.get_project(thread["current_project_id"]) if thread.get("current_project_id") else await _crm.get_project_by_group(group["id"])
            for uid in {thread.get("assigned_poc_id"), proj["main_manager_id"] if proj else None}:
                await notification_service.notify(uid, "send_failed", "The reply could not be delivered to the WhatsApp group. Send it manually.",
                                                  proj["id"] if proj else None, thread_id, client_message_id, out_id, title="Message NOT delivered",
                                                  entity_type="thread", entity_id=thread_id, dedupe_key=f"sendfail{out_id}")
    except Exception as err:  # noqa: BLE001
        print(f"[group] delayed AI reply failed: {err}")


async def _handle_group_message(group: dict, payload: dict):
    """Full group PM/CRM workflow for a message in a *configured* client
    group (spec sections 4-9). Runs instead of the 1:1 contact/message
    pipeline for this event — a configured group never touches the old
    `contacts`/`messages` tables, so the two workflows can't collide.
    """
    account_id = payload["accountId"]
    jid = payload["jid"]
    body = payload["body"]
    from_me = payload.get("fromMe", False)
    whatsapp_message_id = payload.get("messageId")
    sender_jid = payload.get("senderJid")
    # Resolved phone-number jid for sender_jid, when the bridge could work
    # it out (sender_jid itself may be an opaque "...@lid" — see
    # bridge/index.js's getParticipantPhoneMap and group_db.resolve_sender_role).
    sender_phone = payload.get("senderPhone")
    sender_name = payload.get("senderName")
    quoted_id = payload.get("quotedMessageId")

    # --- Idempotency: duplicate webhook delivery of the same WA message ---
    if await group_db.message_already_processed(group["id"], whatsapp_message_id):
        return

    thread = await group_db.get_or_create_open_thread(group["id"], group.get("project_name"))

    # --- Case A: the linked business account itself posted in the group ---
    # (its own phone / WhatsApp Web) — store as an internal message, never
    # trigger an auto-reply to it.
    if from_me:
        await group_db.save_group_message(
            thread_id=thread["id"], group_id=group["id"], whatsapp_message_id=whatsapp_message_id,
            sender_jid=sender_jid, sender_name=sender_name or "Account holder", sender_role="internal",
            direction="out", body=body, reply_to_whatsapp_id=quoted_id, processing_status="processed",
            delivery_status="sent",
        )
        return

    # --- Resolve sender against the configured roster (never trust the
    # display name alone — spec 4.8) ---
    role = await group_db.resolve_sender_role(group["id"], sender_jid, sender_phone)
    if role is None:
        await group_db.save_group_message(
            thread_id=thread["id"], group_id=group["id"], whatsapp_message_id=whatsapp_message_id,
            sender_jid=sender_jid, sender_name=sender_name, sender_role="unknown",
            direction="in", body=body, reply_to_whatsapp_id=quoted_id, processing_status="ignored",
        )
        print(f"[group] unverified sender {sender_jid!r} in group {group['whatsapp_group_id']} — logged, no reply sent")
        return

    saved_id = await group_db.save_group_message(
        thread_id=thread["id"], group_id=group["id"], whatsapp_message_id=whatsapp_message_id,
        sender_jid=sender_jid, sender_name=sender_name, sender_role=role,
        direction="in", body=body, reply_to_whatsapp_id=quoted_id,
    )
    if saved_id is None:
        return  # lost a duplicate-insert race — already handled by another worker

    await sio.emit(f"pm:message:{group['id']}", {"threadId": thread["id"], "role": role, "body": body})

    # Internal team messages become available to the AI via thread history
    # (see group_ai_replier._format_history) but never trigger a reply.
    if role != "client":
        return

    # --- Department routing (CRM projects). Returns None for legacy groups
    # that have no project, which then keep the original single-POC flow. ---
    decision = None
    try:
        decision = await message_router.route_client_message(group, thread, saved_id, body, sender_name)
    except Exception as err:  # noqa: BLE001
        print(f"[ROUTER] routing failed, using legacy flow: {err}")
    if decision:
        thread = await group_db.get_thread(thread["id"]) or thread  # pick up routing columns


    if not group.get("ai_enabled") or thread.get("ai_paused"):
        return  # AI disabled for this group, or a human has taken this thread over

    mode = group.get("ai_mode", "approval")
    if mode == "manual":
        return  # authorized human handles this conversation entirely

    try:
        draft = await group_ai_replier.build_reply(
            group, thread, body, sender_name, routing_ctx=decision.ai_context if decision else None)
    except Exception as err:  # noqa: BLE001
        print(f"[group] AI reply generation failed: {err}")
        await group_db.update_message_status(saved_id, processing_status="failed")
        return

    if not draft:
        # AI decided no reply is needed (acknowledgement, casual chat, already
        # answered). Stay silent: no approval card, no send, no notification.
        await group_db.update_message_status(saved_id, processing_status="processed")
        return

    escalate = (mode == "approval" or group_ai_replier.needs_escalation(body)
                or bool(decision and decision.force_approval))

    if escalate:
        approval_id = await group_db.create_pending_approval(thread["id"], group["id"], saved_id, draft)
        if decision:
            await notification_service.notify(decision.poc_user_id, "approval_pending",
                                              "AI draft waiting for your approval", decision.project_id, thread["id"], saved_id)
        else:
            await group_db.create_notification(thread["id"], group.get("assigned_poc_id"), saved_id)
        await sio.emit(
            f"pm:approval:{group['id']}",
            {"approvalId": approval_id, "threadId": thread["id"], "draft": draft},
        )
        return

    # Mode A (auto), not escalated — wait a short window (so Pause AI, or
    # the account holder jumping in themselves, can still pre-empt it)
    # before actually sending.
    asyncio.create_task(
        _send_group_ai_reply_after_delay(
            decision.account_id if decision else account_id, jid, group, thread["id"], saved_id, draft)
    )


@api.post("/webhook/message")
async def webhook_message(payload: dict):
    account_id = payload["accountId"]
    jid = payload["jid"]
    is_group = payload.get("isGroup", False)
    body = payload["body"]
    from_me = payload.get("fromMe", False)

    # ------------------------------------------------------------------
    # A configured client group runs entirely through the PM/CRM workflow
    # and never touches the 1:1 contacts/messages tables. An unconfigured
    # group falls through unchanged to the original behaviour below
    # (logged like any other chat, no auto-reply) — same as before this
    # feature existed.
    # ------------------------------------------------------------------
    if is_group:
        group = await group_db.get_group_by_whatsapp_id(account_id, jid)
        if group and group.get("active"):
            await _handle_group_message(group, payload)
            return {"ok": True}

    contact_id = await db.upsert_contact(account_id, jid, payload.get("name"))

    # ------------------------------------------------------------------
    # Case A: the account holder typed this themselves (their own phone /
    # WhatsApp Web) — save it, pause the AI for this chat, and stop here.
    # ------------------------------------------------------------------
    if from_me:
        await db.save_message(account_id, contact_id, "out", body, False)
        await db.mark_human_reply(contact_id)
        await sio.emit(
            f"message:{account_id}",
            {"contactId": contact_id, "jid": jid, "direction": "out", "body": body, "aiGenerated": False},
        )
        await sio.emit("accounts:updated")
        return {"ok": True}

    # ------------------------------------------------------------------
    # Case B: an inbound message from the recipient.
    # ------------------------------------------------------------------
    await db.save_message(account_id, contact_id, "in", body, False)
    await sio.emit(
        f"message:{account_id}",
        {"contactId": contact_id, "jid": jid, "direction": "in", "body": body, "aiGenerated": False},
    )
    await sio.emit("accounts:updated")

    # Groups have multiple participants — auto-replying there would be
    # spammy/confusing, so the AI only replies in 1:1 chats.
    if is_group:
        return {"ok": True}

    # The 1:1 sales-qualification bot is a separate feature from the PM/CRM
    # group workflow above — see ONE_TO_ONE_AI_ENABLED's comment. When it's
    # off, the message is still logged (so it still shows up in the
    # dashboard's Contacts view) but nothing auto-replies to it.
    if not ONE_TO_ONE_AI_ENABLED:
        return {"ok": True}

    try:
        history = await _load_history(contact_id)
        relevant = await is_relevant_topic(body, history)

        if not relevant:
            # Off-topic message — whether this is a brand-new chat or a
            # detour in the middle of an ongoing website/software
            # conversation, the AI stays completely silent. No redirect,
            # no reply, no interference at all.
            return {"ok": True}

        status = await db.get_contact_status(contact_id)
        if status in ("paused", "closed"):
            # The AI had stood down (human took over, or a previous ask
            # was already resolved). A fresh, on-topic request brings it
            # back into this chat.
            await db.set_contact_status(contact_id, "open")

        # Relevant topic — don't block this request on the delay; hand it
        # off to a background task and return right away.
        asyncio.create_task(_reply_after_delay(account_id, jid, contact_id, body))
    except Exception as err:  # noqa: BLE001
        print(f"[webhook_message] AI reply failed: {err}")

    return {"ok": True}


@api.post("/webhook/history")
async def webhook_history(payload: dict):
    account_id = payload["accountId"]
    # History can arrive before the "connected" webhook finishes — receiving
    # it at all is itself proof the pairing succeeded.
    await db.upsert_account_connected(account_id, payload.get("phone"), payload.get("name"))

    imported = False
    for item in payload.get("items", []):
        if item.get("isGroup"):
            # Group backlog doesn't go into the 1:1 contacts/messages tables —
            # it's parked in group_history_buffer until (if ever) this group
            # gets added as a client group, at which point
            # group_db.import_group_history() drains it into that group's
            # thread. See _handle_group_message / create_group for the rest
            # of this flow.
            await group_db.buffer_group_history(account_id, item["jid"], item)
            continue
        contact_id = await db.upsert_contact(account_id, item["jid"], item.get("name"))
        await db.save_message(account_id, contact_id, item["direction"], item["body"], False)
        imported = True

    if imported:
        await sio.emit("accounts:updated")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Startup + static frontend (same public/index.html, unchanged)
# ---------------------------------------------------------------------------


@api.on_event("startup")
async def on_startup():
    await db.init_db()
    await group_db.init_group_tables()
    await crm_schema.init_crm_tables()


api.mount("/", StaticFiles(directory=str(PUBLIC_DIR), html=True), name="public")

app = socketio.ASGIApp(sio, other_asgi_app=api)
