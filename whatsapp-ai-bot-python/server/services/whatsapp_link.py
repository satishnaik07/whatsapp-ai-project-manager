"""
Self-service WhatsApp linking for Main Managers and POCs.

A user starts linking from their own dashboard; we remember (account_link_requests)
who asked, show the QR the bridge sends, and when WhatsApp confirms the pairing
(/webhook/status 'connected') the new account is mapped to that user automatically.
That mapping is what makes a POC's replies go out from their own number.
"""
import logging
import uuid

from server import bridge_client, db
from server.db import execute, query
from server.repositories import crm
from server.services import audit

log = logging.getLogger("whatsapp_link")
LATEST_QR: dict[str, str] = {}      # account_id -> latest QR data URL (in memory, short lived)


async def start_link(user_id: int) -> str:
    for old in await query("SELECT account_id FROM account_link_requests WHERE user_id = %s", (user_id,)):
        await _cancel(old["account_id"])
    account_id = str(uuid.uuid4())
    await execute("INSERT INTO account_link_requests (account_id, user_id) VALUES (%s,%s)", (account_id, user_id))
    await bridge_client.start_session(account_id)
    return account_id


async def _cancel(account_id: str):
    LATEST_QR.pop(account_id, None)
    try:
        await bridge_client.remove_session(account_id)
    except Exception as err:  # noqa: BLE001
        log.warning("could not remove stale session %s: %s", account_id, err)
    await execute("DELETE FROM account_link_requests WHERE account_id = %s", (account_id,))


def on_qr(account_id: str, data_url: str):
    LATEST_QR[account_id] = data_url


async def on_connected(account_id: str):
    """Called by /webhook/status. No-op for accounts nobody asked to link from a dashboard."""
    rows = await query("SELECT user_id FROM account_link_requests WHERE account_id = %s", (account_id,))
    LATEST_QR.pop(account_id, None)
    if not rows:
        return
    user_id = rows[0]["user_id"]
    await crm.map_account_user(account_id, user_id)
    await execute("DELETE FROM account_link_requests WHERE account_id = %s", (account_id,))
    await audit.record(user_id, "whatsapp_connected", "account", account_id)
    log.info("[WHATSAPP] account=%s linked to user=%s", account_id, user_id)


async def status_for(user_id: int) -> dict:
    acc = await crm.get_user_account(user_id)
    pending = await query("SELECT account_id FROM account_link_requests WHERE user_id = %s ORDER BY created_at DESC LIMIT 1", (user_id,))
    p = None
    if pending:
        aid = pending[0]["account_id"]
        p = {"account_id": aid, "qr": LATEST_QR.get(aid)}
    return {"account": acc, "pending": p}


async def unlink(user_id: int) -> bool:
    acc = await crm.get_user_account(user_id)
    for req in await query("SELECT account_id FROM account_link_requests WHERE user_id = %s", (user_id,)):
        await _cancel(req["account_id"])
    if not acc:
        return False
    await bridge_client.remove_session(acc["account_id"])
    await db.delete_account(acc["account_id"])
    await audit.record(user_id, "whatsapp_disconnected", "account", acc["account_id"])
    return True
