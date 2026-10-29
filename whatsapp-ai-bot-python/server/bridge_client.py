"""
Small HTTP client for talking to the Node/Baileys bridge (bridge/index.js).
"""
import os
from urllib.parse import quote

import httpx

BRIDGE_URL = os.getenv("BRIDGE_URL", "http://localhost:4000")


async def start_session(account_id: str):
    async with httpx.AsyncClient() as client:
        await client.post(f"{BRIDGE_URL}/sessions/{account_id}", timeout=10)


async def send_message(account_id: str, jid: str, text: str) -> bool:
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{BRIDGE_URL}/sessions/{account_id}/send",
                json={"jid": jid, "text": text},
                timeout=30,
            )
            return resp.status_code == 200
        except httpx.HTTPError as e:
            print(f"[bridge_client] send_message failed: {e}")
            return False


async def list_groups(account_id: str) -> list[dict]:
    """Groups the linked WhatsApp session currently participates in —
    used by the dashboard's 'Add group' screen so the admin can pick a
    group by name instead of typing a raw JID."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(f"{BRIDGE_URL}/sessions/{account_id}/groups", timeout=15)
            if resp.status_code != 200:
                return []
            return resp.json().get("groups", [])
        except httpx.HTTPError as e:
            print(f"[bridge_client] list_groups failed: {e}")
            return []


async def list_group_members(account_id: str, group_jid: str) -> list[dict]:
    """Real participants of one WhatsApp group — used so 'Add client
    group' can fetch the roster instead of the admin hand-typing JIDs
    (which is exactly how a mistyped JID like '+918957394688' instead of
    '918957394688@s.whatsapp.net' ends up never matching a real sender)."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.get(
                f"{BRIDGE_URL}/sessions/{account_id}/groups/{quote(group_jid, safe='')}/members",
                timeout=15,
            )
            if resp.status_code != 200:
                return []
            return resp.json().get("members", [])
        except httpx.HTTPError as e:
            print(f"[bridge_client] list_group_members failed: {e}")
            return []


async def lookup_member_by_number(account_id: str, group_jid: str, number: str) -> dict:
    """Which participant of this group has this phone number? (number -> lid
    direction — the only lookup WhatsApp supports.)"""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{BRIDGE_URL}/sessions/{account_id}/groups/{quote(group_jid, safe='')}/lookup-number",
                json={"number": number},
                timeout=20,
            )
            return resp.json()
        except (httpx.HTTPError, ValueError) as e:
            print(f"[bridge_client] lookup_member_by_number failed: {e}")
            return {"error": "bridge unreachable"}


async def sync_group_history(account_id: str, group_jid: str, pages: int = 3) -> dict:
    """Best-effort: ask the bridge to pull more of this group's WhatsApp
    backlog (see bridge/index.js's /sync-history). Fire-and-forget from the
    caller's point of view — results land later via the normal
    /webhook/history flow into group_history_buffer, not in this response."""
    async with httpx.AsyncClient() as client:
        try:
            resp = await client.post(
                f"{BRIDGE_URL}/sessions/{account_id}/groups/{quote(group_jid, safe='')}/sync-history",
                json={"pages": pages},
                timeout=10,
            )
            return resp.json()
        except (httpx.HTTPError, ValueError) as e:
            print(f"[bridge_client] sync_group_history failed: {e}")
            return {"requested": False, "reason": "bridge unreachable"}


async def remove_session(account_id: str):
    async with httpx.AsyncClient() as client:
        try:
            await client.delete(f"{BRIDGE_URL}/sessions/{account_id}", timeout=15)
        except httpx.HTTPError as e:
            print(f"[bridge_client] remove_session failed: {e}")
