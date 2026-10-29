"""Verified updates: facts a Main Manager / POC / admin has confirmed.

One place for the rules, used by both the project page and the group dashboard:

* who may add one (admin, the project's Main Manager, a department POC of the project; for a group with no project,
  anyone who may open the group dashboard, which is how it worked before)
* visibility: internal = saved in the CRM only, client = sent to the WhatsApp group, both = saved AND sent
* every entry keeps who added it and when, and everybody on the project can see the list
* the AI reads all of them (see services/ai_context.py)
"""
import json

from fastapi import HTTPException

from server import group_db
from server.repositories import crm
from server.services import audit, notification_service, poc_router

VISIBILITIES = crm.VERIFIED_VISIBILITIES


def clean_visibility(value) -> str:
    v = (value or "internal").strip().lower()
    if v not in VISIBILITIES:
        raise HTTPException(status_code=400, detail="visibility must be 'internal', 'client' or 'both'")
    return v


async def can_add_to_project(user: dict, project_id: int) -> bool:
    if user["role"] == "admin" or await crm.user_can_manage_project(user, project_id):
        return True
    return any(d["poc_user_id"] == user["id"] for d in await crm.list_project_departments(project_id))


async def can_delete(user: dict, row: dict) -> bool:
    if user["role"] == "admin" or row.get("author_id") == user["id"]:
        return True
    return bool(row.get("project_id")) and await crm.user_can_manage_project(user, row["project_id"])


def legacy_entries(context_row: dict | None) -> list[dict]:
    """Updates saved by older versions inside project_context.verified_updates (JSON). Shown read-only."""
    if not context_row or not context_row.get("verified_updates"):
        return []
    try:
        items = json.loads(context_row["verified_updates"])
    except Exception:  # noqa: BLE001
        return []
    out = []
    for i, u in enumerate(reversed(items or [])):
        if isinstance(u, dict) and u.get("text"):
            out.append({"id": None, "legacy": True, "text": u["text"], "author_name": u.get("added_by"),
                        "author_role": None, "visibility": "internal", "sent_to_client": 0,
                        "created_at": u.get("at"), "project_id": None, "project_name": None})
    return out


async def post(user: dict, text: str, visibility: str, *, group: dict | None, project: dict | None) -> dict:
    """Saves the update, optionally sends it to the group, tells the people involved. Returns the API result."""
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")
    if len(text) > 2000:
        raise HTTPException(status_code=400, detail="Keep a verified update under 2000 characters")
    visibility = clean_visibility(visibility)
    group_id = group["id"] if group else (project or {}).get("group_id")
    project_id = project["id"] if project else None
    if visibility in ("client", "both") and not group_id:
        raise HTTPException(status_code=400, detail="This project has no WhatsApp group yet, so it can only be saved as internal")

    uid = await crm.add_verified_update(text, group_id=group_id, project_id=project_id, author=user, visibility=visibility)
    sent, delivery_error = False, None
    if visibility in ("client", "both"):
        g = group or await group_db.get_group(group_id)
        thread = await group_db.get_or_create_open_thread(g["id"], g.get("project_name"))
        message = f"Update: {text}"
        sent, _acc = await poc_router.send_with_fallback(g, {"assigned_poc_id": user["id"], "current_project_id": project_id}, message)
        await group_db.save_group_message(
            thread_id=thread["id"], group_id=g["id"], whatsapp_message_id=None, sender_jid=None,
            sender_name=user.get("name") or user.get("username"), sender_role="internal", direction="out", body=message,
            processing_status="processed", delivery_status="sent" if sent else "failed")
        if sent:
            await crm.mark_verified_sent(uid)
        else:
            delivery_error = "WhatsApp send failed (the update is saved in the CRM)"
    await audit.record(user["id"], "verified_update_added", "project" if project_id else "group", project_id or group_id,
                       f"verified_update={uid} visibility={visibility} sent={sent}", project_id=project_id)

    # everybody working on the project can see it in the list; the Main Manager and POCs also get a bell notification
    audience = set()
    if project_id:
        if project.get("main_manager_id"):
            audience.add(project["main_manager_id"])
        audience |= {d["poc_user_id"] for d in await crm.list_project_departments(project_id) if d["poc_user_id"]}
    audience.discard(user["id"])
    who = user.get("name") or user.get("username") or "Someone"
    for target in audience:
        await notification_service.notify(target, "verified_update", f"{who} added a verified update: {text[:300]}", project_id,
                                          title="Verified update added", entity_type="project", entity_id=project_id,
                                          dedupe_key=f"vu{uid}:{target}")
    return {"id": uid, "visibility": visibility, "sent_to_client": sent, "delivery_error": delivery_error}


async def can_add_to_group(user: dict, group: dict) -> bool:
    """Group dashboard: admin, the legacy login POC of the group, or a manager / POC of one of its projects.
    Plain department members can read the list but not add to it."""
    if user["role"] == "admin" or group.get("assigned_poc_id") == user["id"]:
        return True
    for p in await crm.list_projects_by_group(group["id"]):
        if await can_add_to_project(user, p["id"]):
            return True
    return False


async def group_view(user: dict, group: dict) -> dict:
    ctx = await group_db.get_project_context(group["id"])
    projects = await crm.list_projects_by_group(group["id"])
    items = await crm.list_verified_updates(group_id=group["id"]) + legacy_entries(ctx)
    for r in items:
        r["can_delete"] = bool(r.get("id")) and await can_delete(user, r)
    return {"items": items, "can_add": await can_add_to_group(user, group),
            "projects": [{"id": p["id"], "name": p["name"]} for p in projects], "has_group": True}


async def project_view(user: dict, project: dict) -> dict:
    items = await crm.list_verified_updates(project_id=project["id"])
    for r in items:
        r["can_delete"] = await can_delete(user, r)
    return {"items": items, "can_add": await can_add_to_project(user, project["id"]),
            "projects": [], "has_group": bool(project.get("group_id"))}
