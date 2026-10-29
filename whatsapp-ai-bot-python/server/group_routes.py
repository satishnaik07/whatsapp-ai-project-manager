"""
REST API for the WhatsApp AI Project Manager / CRM dashboard.

Mounted onto the existing FastAPI `api` app in main.py under /api/pm and
/webhook (the sending helper). Everything here is additive — none of the
routes in main.py are removed or renamed.
"""
import asyncio

from fastapi import APIRouter, Depends, HTTPException

from server import group_db, bridge_client
from server.repositories import crm
from server.services import poc_router, audit, verified_updates
from server.auth import create_token, get_current_poc, hash_password, require_role, verify_password

router = APIRouter(prefix="/api/pm")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@router.post("/auth/bootstrap-admin")
async def bootstrap_admin(payload: dict):
    """One-time setup route: creates the first admin account. Refuses to
    run once any poc_users row already exists, so this can't be used to
    mint extra admins later — see README for the bootstrap flow."""
    if await group_db.count_poc_users() > 0:
        raise HTTPException(status_code=403, detail="Admin already bootstrapped; log in and use /pocs instead")
    username = payload.get("username")
    password = payload.get("password")
    name = payload.get("name") or username
    if not username or not password or len(password) < 8:
        raise HTTPException(status_code=400, detail="username and a password (8+ chars) are required")
    poc_id = await group_db.create_poc_user(username, hash_password(password), name, role="admin")
    token = create_token(poc_id, username, "admin")
    return {"token": token, "poc": {"id": poc_id, "username": username, "name": name, "role": "admin"}}


@router.post("/auth/login")
async def login(payload: dict):
    username = payload.get("username")
    password = payload.get("password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="username and password are required")
    poc = await group_db.get_poc_by_username(username)
    if not poc or not verify_password(password, poc["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_token(poc["id"], poc["username"], poc["role"])
    return {
        "token": token,
        "poc": {"id": poc["id"], "username": poc["username"], "name": poc["name"], "role": poc["role"]},
    }


@router.get("/auth/me")
async def me(current=Depends(get_current_poc)):
    return {"id": current["id"], "username": current["username"], "name": current["name"], "role": current["role"]}


@router.get("/pocs")
async def list_pocs(current=Depends(get_current_poc)):
    require_role(current, ("admin",))
    return await group_db.list_poc_users()


@router.post("/pocs")
async def create_poc(payload: dict, current=Depends(get_current_poc)):
    require_role(current, ("admin",))
    username = payload.get("username")
    password = payload.get("password")
    name = payload.get("name") or username
    role = payload.get("role", "poc")
    if role not in ("admin", "poc"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'poc'")
    if not username or not password or len(password) < 8:
        raise HTTPException(status_code=400, detail="username and a password (8+ chars) are required")
    poc_id = await group_db.create_poc_user(username, hash_password(password), name, role)
    return {"id": poc_id, "username": username, "name": name, "role": role}


# ---------------------------------------------------------------------------
# Access-control helper
# ---------------------------------------------------------------------------


async def _require_group_access(group_id: int, current: dict) -> dict:
    group = await group_db.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if not await group_db.poc_can_access_group(current["role"], current["id"], group_id):
        raise HTTPException(status_code=403, detail="Not authorized for this group")
    return group


# ---------------------------------------------------------------------------
# Client groups
# ---------------------------------------------------------------------------


@router.get("/groups")
async def list_groups(current=Depends(get_current_poc)):
    poc_filter = None if current["role"] == "admin" else current["id"]
    return await group_db.list_groups(poc_filter)


async def _own_account_only(current: dict, account_id: str):
    """A Main Manager may list groups/members only through the WhatsApp account linked to them."""
    if current["role"] == "admin":
        return
    from server.repositories import crm
    acc = await crm.get_user_account(current["id"])
    if not acc or acc["account_id"] != account_id:
        raise HTTPException(status_code=403, detail="You can only browse groups of your own linked WhatsApp account")


@router.get("/discover-groups")
async def discover_groups(account_id: str, current=Depends(get_current_poc)):
    """Real WhatsApp groups the given linked account currently participates
    in — for the 'Add group' picker, so the admin doesn't have to hand-type
    a raw JID like 120363xxxxxxxx@g.us."""
    require_role(current, ("admin", "main_manager"))
    await _own_account_only(current, account_id)
    return await bridge_client.list_groups(account_id)


@router.get("/discover-groups/{group_jid}/members")
async def discover_group_members(group_jid: str, account_id: str, current=Depends(get_current_poc)):
    """Real members of one WhatsApp group (by jid) — powers the 'fetch
    members' step in the Add Client Group modal, so client/internal roles
    are picked from the real roster instead of typed by hand."""
    require_role(current, ("admin", "main_manager"))
    await _own_account_only(current, account_id)
    return await bridge_client.list_group_members(account_id, group_jid)


@router.post("/discover-groups/{group_jid}/lookup-number")
async def lookup_member_number(group_jid: str, account_id: str, payload: dict,
                               current=Depends(get_current_poc)):
    """Admin types a phone number; we find which member of the group it is
    (see bridge/index.js lookup-number)."""
    require_role(current, ("admin", "main_manager"))
    result = await bridge_client.lookup_member_by_number(account_id, group_jid, payload.get("number", ""))
    if result.get("error"):
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/groups")
async def create_group(payload: dict, current=Depends(get_current_poc)):
    require_role(current, ("admin",))
    account_id = payload.get("account_id")
    whatsapp_group_id = payload.get("whatsapp_group_id")
    if not account_id or not whatsapp_group_id:
        raise HTTPException(status_code=400, detail="account_id and whatsapp_group_id are required")
    if await group_db.get_group_by_whatsapp_id(account_id, whatsapp_group_id):
        raise HTTPException(status_code=409, detail="This WhatsApp group is already configured")

    poc_member = payload.get("poc_member") or {}
    client_jids = {m.get("jid") for m in payload.get("members", []) if m.get("role") == "client"}
    if poc_member.get("jid") and poc_member["jid"] in client_jids:
        raise HTTPException(status_code=400, detail="A member can't be both Client and POC")

    group_id = await group_db.create_group(
        account_id=account_id,
        whatsapp_group_id=whatsapp_group_id,
        group_name=payload.get("group_name"),
        project_name=payload.get("project_name"),
        assigned_poc_id=payload.get("assigned_poc_id"),
        ai_mode=payload.get("ai_mode", "approval"),
    )
    for m in payload.get("members", []):
        if m.get("jid") and m.get("role") in ("client", "internal"):
            await group_db.upsert_member(
                group_id, m["jid"], m.get("name"), m["role"], member_phone=m.get("phone")
            )
    if poc_member.get("jid"):
        await group_db.set_poc_member(
            group_id, poc_member["jid"], poc_member.get("name"), poc_member.get("phone")
        )
    if payload.get("description") or payload.get("scope"):
        await group_db.upsert_project_context(
            group_id, description=payload.get("description"), scope=payload.get("scope")
        )

    # ------------------------------------------------------------------
    # Bring in this group's backlog (spec: "group add ho, uska purana chats
    # thread mai store ho"). Two sources, both feeding the same thread:
    #   1. Whatever WhatsApp already handed us for this group at some past
    #      connection/history-sync — sitting in group_history_buffer,
    #      imported synchronously below so it's in the response already.
    #   2. An on-demand ask for MORE backlog (bridge's fetchMessageHistory),
    #      which can't answer inline (WhatsApp responds asynchronously via
    #      its own event), so that top-up runs in the background and the
    #      newly-arrived messages just show up in the thread a few seconds
    #      later — see _import_more_history_soon below.
    # Member roles are already saved above, so resolve_sender_role can
    # correctly tag each buffered message as client/internal here.
    # ------------------------------------------------------------------
    imported_now = await group_db.import_group_history(group_id, account_id, whatsapp_group_id)
    asyncio.create_task(_import_more_history_soon(group_id, account_id, whatsapp_group_id))

    group = await group_db.get_group(group_id)
    group["history_imported"] = imported_now
    return group


async def _import_more_history_soon(group_id: int, account_id: str, whatsapp_group_id: str):
    """Background top-up: ask the bridge for older WhatsApp history, wait
    for it to land in group_history_buffer, then import whatever showed
    up. Best-effort — a bridge/WhatsApp hiccup here just means the group
    keeps whatever backlog was already imported synchronously."""
    try:
        await bridge_client.sync_group_history(account_id, whatsapp_group_id, pages=3)
        await asyncio.sleep(10)  # let /webhook/history deliveries catch up
        more = await group_db.import_group_history(group_id, account_id, whatsapp_group_id)
        if more:
            from server.main import sio  # lazy import: avoids a circular import at module load
            await sio.emit(f"pm:history-imported:{group_id}", {"count": more})
    except Exception as err:  # noqa: BLE001
        print(f"[group] background history top-up failed for group {group_id}: {err}")


@router.get("/groups/{group_id}")
async def get_group(group_id: int, current=Depends(get_current_poc)):
    group = await _require_group_access(group_id, current)
    group["members"] = await group_db.list_members(group_id)
    group["project_context"] = await group_db.get_project_context(group_id)
    return group


@router.patch("/groups/{group_id}")
async def patch_group(group_id: int, payload: dict, current=Depends(get_current_poc)):
    await _require_group_access(group_id, current)
    # A POC (non-admin) may toggle AI on/off for their own group but not
    # reassign it or rename the project — that stays admin-only.
    if current["role"] != "admin":
        payload = {k: v for k, v in payload.items() if k in ("ai_enabled", "ai_mode")}
    await group_db.update_group(group_id, **payload)
    return await group_db.get_group(group_id)


@router.delete("/groups/{group_id}")
async def delete_group(group_id: int, current=Depends(get_current_poc)):
    """Fully removes a configured client group (and its members, threads,
    messages, approvals and notifications — see group_db.delete_group).
    Admin-only, same as creating one. This only removes it from this
    dashboard; it does not leave or delete the actual WhatsApp group."""
    require_role(current, ("admin",))
    await _require_group_access(group_id, current)
    await group_db.delete_group(group_id)
    return {"deleted": group_id}


@router.put("/groups/{group_id}/poc-member")
async def set_poc_member(group_id: int, payload: dict, current=Depends(get_current_poc)):
    """Tags (or retags) which WhatsApp group member is this group's POC —
    the 'fetch group members, pick one not already a Client' step, usable
    from the group detail page after creation too. Pass jid=null (or omit
    it) to clear the POC without setting a new one."""
    require_role(current, ("admin",))
    await _require_group_access(group_id, current)
    jid = payload.get("jid")
    if not jid:
        await group_db.clear_poc_member(group_id)
        return await group_db.get_group(group_id)
    is_client = await group_db.member_has_role(group_id, jid, "client")
    if is_client:
        raise HTTPException(status_code=400, detail="This member is already assigned as a Client and can't also be the POC")
    try:
        await group_db.set_poc_member(group_id, jid, payload.get("name"), payload.get("phone"))
    except ValueError as err:
        raise HTTPException(status_code=400, detail=str(err))
    group = await _require_group_access(group_id, current)
    group["members"] = await group_db.list_members(group_id)
    return group


@router.post("/groups/{group_id}/members")
async def add_member(group_id: int, payload: dict, current=Depends(get_current_poc)):
    require_role(current, ("admin",))
    await _require_group_access(group_id, current)
    jid = payload.get("jid")
    role = payload.get("role")
    if not jid or role not in ("client", "internal"):
        raise HTTPException(status_code=400, detail="jid and role ('client' or 'internal') are required")
    await group_db.upsert_member(group_id, jid, payload.get("name"), role, member_phone=payload.get("phone"))
    return await group_db.list_members(group_id)


@router.delete("/groups/{group_id}/members/{member_jid}")
async def remove_member(group_id: int, member_jid: str, current=Depends(get_current_poc)):
    require_role(current, ("admin",))
    await _require_group_access(group_id, current)
    await group_db.deactivate_member(group_id, member_jid)
    return await group_db.list_members(group_id)


# ---------------------------------------------------------------------------
# Project context
# ---------------------------------------------------------------------------


@router.put("/groups/{group_id}/context")
async def put_context(group_id: int, payload: dict, current=Depends(get_current_poc)):
    await _require_group_access(group_id, current)
    await group_db.upsert_project_context(
        group_id,
        description=payload.get("description"),
        scope=payload.get("scope"),
        status=payload.get("status"),
        communication_instructions=payload.get("communication_instructions"),
    )
    return await group_db.get_project_context(group_id)


@router.get("/groups/{group_id}/verified-updates")
async def list_group_verified(group_id: int, current=Depends(get_current_poc)):
    """Every verified update of this group (all of its projects), newest first, with who added it."""
    group = await _require_group_access(group_id, current)
    return await verified_updates.group_view(current, group)


@router.post("/groups/{group_id}/context/updates")
async def add_update(group_id: int, payload: dict, current=Depends(get_current_poc)):
    """Add a verified update from the group dashboard.
    payload: text, visibility ('internal' | 'client' | 'both'), optional project_id (when the group serves several projects)."""
    group = await _require_group_access(group_id, current)
    if not await verified_updates.can_add_to_group(current, group):
        raise HTTPException(status_code=403, detail="Only the Main Manager, a POC of this group or an admin can add a verified update")
    projects = await crm.list_projects_by_group(group_id)
    project = None
    wanted = payload.get("project_id")
    if wanted not in (None, ""):
        project = next((p for p in projects if p["id"] == int(wanted)), None)
        if not project:
            raise HTTPException(status_code=400, detail="That project is not linked to this group")
    elif len(projects) == 1:
        project = projects[0]
    result = await verified_updates.post(current, payload.get("text"), payload.get("visibility"), group=group, project=project)
    return {**result, **await verified_updates.group_view(current, group)}


# ---------------------------------------------------------------------------
# Conversations / threads
# ---------------------------------------------------------------------------


@router.get("/groups/{group_id}/threads")
async def group_threads(group_id: int, current=Depends(get_current_poc)):
    await _require_group_access(group_id, current)
    return await group_db.list_threads_for_group(group_id)


@router.post("/groups/{group_id}/sync-history")
async def sync_group_history(group_id: int, current=Depends(get_current_poc)):
    """For a group that was added before this feature existed (or whose
    backlog simply wasn't buffered yet at the time) — pulls in whatever
    WhatsApp backlog is available now, the same way a brand-new "Add
    group" does. Safe to click more than once; already-imported messages
    are never duplicated (group_messages' UNIQUE(group_id,
    whatsapp_message_id) — see save_group_message)."""
    require_role(current, ("admin",))
    group = await _require_group_access(group_id, current)
    imported_now = await group_db.import_group_history(
        group_id, group["account_id"], group["whatsapp_group_id"]
    )
    asyncio.create_task(
        _import_more_history_soon(group_id, group["account_id"], group["whatsapp_group_id"])
    )
    return {"imported_now": imported_now, "more_requested": True}


async def _require_thread_access(thread_id: int, current: dict) -> dict:
    thread = await group_db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    await _require_group_access(thread["group_id"], current)
    return thread


@router.get("/threads/{thread_id}")
async def get_thread_detail(thread_id: int, current=Depends(get_current_poc)):
    """Single thread's current state (status + ai_paused) — used by the
    dashboard to refresh the Pause/Resume buttons so they reflect reality
    instead of going stale as soon as the conversation modal is opened."""
    return await _require_thread_access(thread_id, current)


@router.get("/threads/{thread_id}/messages")
async def thread_messages(thread_id: int, current=Depends(get_current_poc)):
    await _require_thread_access(thread_id, current)
    return await group_db.list_messages_for_thread(thread_id)


@router.post("/threads/{thread_id}/status")
async def set_thread_status(thread_id: int, payload: dict, current=Depends(get_current_poc)):
    await _require_thread_access(thread_id, current)
    status = payload.get("status")
    if status not in ("open", "resolved", "closed"):
        raise HTTPException(status_code=400, detail="status must be open, resolved, or closed")
    await group_db.set_thread_status(thread_id, status)
    return await group_db.get_thread(thread_id)


@router.post("/threads/{thread_id}/pause")
async def pause_thread(thread_id: int, current=Depends(get_current_poc)):
    """Human handoff: pause AI for this one conversation so the POC can
    reply manually."""
    await _require_thread_access(thread_id, current)
    await group_db.set_thread_ai_paused(thread_id, True)
    return await group_db.get_thread(thread_id)


@router.post("/threads/{thread_id}/resume")
async def resume_thread(thread_id: int, current=Depends(get_current_poc)):
    await _require_thread_access(thread_id, current)
    await group_db.set_thread_ai_paused(thread_id, False)
    return await group_db.get_thread(thread_id)


@router.post("/threads/{thread_id}/reply")
async def manual_reply(thread_id: int, payload: dict, current=Depends(get_current_poc)):
    """Authorized manual reply from the POC, sent to the WhatsApp group
    right away (spec 12's 'support authorized manual replies')."""
    thread = await _require_thread_access(thread_id, current)
    group = await group_db.get_group(thread["group_id"])
    text = payload.get("text")
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    sent, _used = await poc_router.send_with_fallback(group, thread, text)
    delivery_status = "sent" if sent else "failed"
    await group_db.save_group_message(
        thread_id=thread_id, group_id=group["id"], whatsapp_message_id=None,
        sender_jid=None, sender_name=current["name"] or current["username"], sender_role="internal",
        direction="out", body=text, delivery_status=delivery_status, processing_status="processed",
    )
    if not sent:
        raise HTTPException(status_code=502, detail="WhatsApp send failed (bridge/session issue)")
    return {"sent": True}


# ---------------------------------------------------------------------------
# Human-approval queue (Mode B groups)
# ---------------------------------------------------------------------------


@router.get("/approvals")
async def list_approvals(group_id: int | None = None, current=Depends(get_current_poc)):
    if group_id is not None:
        await _require_group_access(group_id, current)
        return await group_db.list_pending_approvals(group_id)
    if current["role"] == "admin":
        return await group_db.list_pending_approvals(None)
    # Non-admin: only approvals for groups they're assigned to.
    my_groups = {g["id"] for g in await group_db.list_groups(current["id"])}
    all_pending = await group_db.list_pending_approvals(None)
    return [a for a in all_pending if a["group_id"] in my_groups]


@router.post("/approvals/{approval_id}/approve")
async def approve(approval_id: int, payload: dict | None = None, current=Depends(get_current_poc)):
    approval = await group_db.get_pending_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    await _require_group_access(approval["group_id"], current)
    if approval["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"Already {approval['status']}")

    final_text = (payload or {}).get("edited_text") or approval["draft_reply"]
    group = await group_db.get_group(approval["group_id"])
    thread = await group_db.get_thread(approval["thread_id"])
    sent, _used = await poc_router.send_with_fallback(group, thread, final_text)
    await audit.record(current["id"], "ai_reply_sent" if sent else "ai_reply_send_failed", "approval", approval_id)

    await group_db.save_group_message(
        thread_id=approval["thread_id"], group_id=approval["group_id"], whatsapp_message_id=None,
        sender_jid=None, sender_name="AI Project Manager", sender_role="ai",
        direction="out", body=final_text, processing_status="processed",
        delivery_status="sent" if sent else "failed",
    )
    await group_db.decide_approval(approval_id, "approved" if sent else "pending", current["id"])
    if not sent:
        raise HTTPException(status_code=502, detail="Approved, but WhatsApp send failed — still pending, retry")
    await group_db.decide_approval(approval_id, "sent", current["id"])
    return {"sent": True}


@router.post("/approvals/{approval_id}/reject")
async def reject(approval_id: int, current=Depends(get_current_poc)):
    approval = await group_db.get_pending_approval(approval_id)
    if not approval:
        raise HTTPException(status_code=404, detail="Approval not found")
    await _require_group_access(approval["group_id"], current)
    await group_db.decide_approval(approval_id, "rejected", current["id"])
    return {"rejected": True}


# ---------------------------------------------------------------------------
# Message logs & dashboard overview
# ---------------------------------------------------------------------------


@router.get("/overview")
async def overview(current=Depends(get_current_poc)):
    stats = await group_db.overview_stats()
    if current["role"] != "admin":
        my_group_ids = {g["id"] for g in await group_db.list_groups(current["id"])}
        stats["recent_client_messages"] = [
            m for m in stats["recent_client_messages"] if m["group_id"] in my_group_ids
        ]
    return stats


@router.get("/messages")
async def message_logs(group_id: int | None = None, failed_only: bool = False, current=Depends(get_current_poc)):
    if group_id is not None:
        await _require_group_access(group_id, current)
        return await group_db.list_recent_messages(group_id, failed_only=failed_only)
    if current["role"] == "admin":
        return await group_db.list_recent_messages(None, failed_only=failed_only)
    my_group_ids = {g["id"] for g in await group_db.list_groups(current["id"])}
    rows = await group_db.list_recent_messages(None, limit=300, failed_only=failed_only)
    return [r for r in rows if r["group_id"] in my_group_ids]
