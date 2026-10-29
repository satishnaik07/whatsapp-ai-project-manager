"""Audit trail helper. Never raises: an audit failure must not break the business operation, but it is logged loudly.

Edits store what changed (previous and new values). Anything that looks like a secret is replaced by "***" so
passwords, tokens and API keys never reach the log."""
import json
import logging

from server.repositories import crm

log = logging.getLogger("audit")
_SECRET = ("password", "passwd", "secret", "token", "api_key", "apikey", "hash", "authorization", "credential")


def redact(data):
    if isinstance(data, dict):
        return {k: ("***" if any(w in str(k).lower() for w in _SECRET) else redact(v)) for k, v in data.items()}
    if isinstance(data, (list, tuple)):
        return [redact(v) for v in data]
    return data


def changes(before: dict | None, after: dict) -> tuple[dict, dict]:
    """Only the fields that really changed, as (old_values, new_values)."""
    before = before or {}
    old, new = {}, {}
    for k, v in after.items():
        if str(before.get(k)) != str(v):
            old[k], new[k] = before.get(k), v
    return old, new


async def record(user_id, action: str, entity: str | None = None, entity_id=None, detail: str | None = None, *,
                 before: dict | None = None, after: dict | None = None, project_id=None, department_id=None, success: bool = True):
    try:
        if before is not None or after is not None:
            payload = {"before": redact(before or {}), "after": redact(after or {})}
            if detail:
                payload["note"] = detail
            detail = json.dumps(payload, default=str)[:60000]
        await crm.add_audit(user_id, action, entity, entity_id, detail, project_id, department_id, success)
    except Exception as err:  # noqa: BLE001
        log.error("[AUDIT] failed to write %s: %s", action, err)
