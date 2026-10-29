"""
POC / WhatsApp-account resolution and sending.

* A department POC is "available" only if they are active AND have a linked
  WhatsApp account whose status is 'connected'.
* Replies go out through ONE account: the responsible user's account, never
  broadcast through every connected account. If that send fails, we fall back
  (manager account, then the group's original listener account) and log it.
"""
import logging

from server import bridge_client
from server.repositories import crm

log = logging.getLogger("router")


def poc_problem(dept_row: dict) -> str | None:
    """Why this department's POC cannot take the message, or None if fine."""
    if not dept_row.get("poc_user_id"):
        return "no_poc_assigned"
    if not dept_row.get("poc_account_id"):
        return "poc_has_no_whatsapp_account"
    if dept_row.get("poc_account_status") != "connected":
        return "poc_whatsapp_not_connected"
    return None


async def user_connected_account(user_id: int | None) -> str | None:
    if not user_id:
        return None
    acc = await crm.get_user_account(user_id)
    return acc["account_id"] if acc and acc["status"] == "connected" else None


async def sending_account_candidates(group: dict, thread: dict | None, project: dict | None = None) -> list[str]:
    """Ordered, de-duplicated account ids to try for a reply."""
    out: list[str] = []
    responsible = (thread or {}).get("assigned_poc_id")
    manager = (project or {}).get("main_manager_id")
    for uid in (responsible, manager):
        acc = await user_connected_account(uid)
        if acc and acc not in out:
            out.append(acc)
    if group["account_id"] not in out:
        out.append(group["account_id"])  # legacy / listener account is always the last resort
    return out


async def send_with_fallback(group: dict, thread: dict | None, text: str) -> tuple[bool, str | None]:
    project = await crm.get_project_by_group(group["id"])
    if thread and thread.get("current_project_id") and (not project or project["id"] != thread["current_project_id"]):
        project = await crm.get_project(thread["current_project_id"]) or project
    candidates = await sending_account_candidates(group, thread, project)
    for i, account_id in enumerate(candidates):
        sent = await bridge_client.send_message(account_id, group["whatsapp_group_id"], text)
        log.info("[SEND] group=%s account=%s sent=%s attempt=%d", group["id"], account_id, sent, i + 1)
        if sent:
            return True, account_id
        log.warning("[SEND] failed via account=%s group=%s, trying next", account_id, group["id"])
    return False, None
