"""Targeted notifications. Exactly the people who need to act are notified, never every POC.

`notify` is idempotent when a dedupe_key is passed. `notify_event` fans an admin-style event (client or project changed,
group mapped, role changed ...) out to its authorised audience: admins, the project's Main Manager and any extra recipients,
never the person who did it."""
import logging

from server.repositories import crm

log = logging.getLogger("notify")


async def notify(user_id, kind: str, message: str, project_id=None, thread_id=None, client_message_id=None, ai_reply_id=None,
                 *, title=None, entity_type=None, entity_id=None, dedupe_key=None):
    if not user_id:
        return None
    try:
        return await crm.add_notification(user_id, kind, message, project_id, thread_id, client_message_id, ai_reply_id,
                                          title, entity_type, entity_id, dedupe_key)
    except Exception as err:  # noqa: BLE001
        log.error("[NOTIFY] failed for user=%s kind=%s: %s", user_id, kind, err)
        return None


async def notify_admins(kind: str, message: str, project_id=None, thread_id=None, *, title=None):
    for uid in await crm.admin_ids():
        await notify(uid, kind, message, project_id, thread_id, title=title)


async def notify_event(kind: str, title: str, message: str = "", *, actor_id=None, project_id=None, manager_id=None,
                       extra=(), entity_type=None, entity_id=None):
    """Admins + the responsible Main Manager + extra recipients, minus the actor. Returns who was notified."""
    audience = set(await crm.admin_ids()) | set(extra or ())
    if project_id is not None and not manager_id:
        p = await crm.get_project(project_id, include_deleted=True)
        manager_id = p["main_manager_id"] if p else None
    if manager_id:
        audience.add(manager_id)
    audience.discard(actor_id)
    for uid in audience:
        await notify(uid, kind, message or title, project_id, title=title, entity_type=entity_type, entity_id=entity_id)
    return audience
