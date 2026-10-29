"""
Database layer for the WhatsApp Group AI Project Manager / CRM feature.

Additive to db.py — reuses the same connection pool (server.db.query /
server.db.execute) and the existing `accounts` table (a client group
belongs to one connected WhatsApp account/business line). None of the
original tables (accounts, contacts, messages, meetings) are touched;
this is a parallel set of tables for the *group* workflow, since 1:1
chats and group project-conversations have different shapes (a group
has members with roles, a project, a POC, etc).

Idempotency: group_messages has a UNIQUE key on (group_id,
whatsapp_message_id), so re-processing the same WhatsApp webhook event
(duplicate delivery) never creates a duplicate row — see save_group_message.
"""
import json
import secrets
from datetime import datetime

from server.db import query, execute, get_pool


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


async def init_group_tables():
    """Creates every table this feature needs, if missing. Safe to run on
    every startup (mirrors db.init_db's pattern)."""

    await execute(
        """
        CREATE TABLE IF NOT EXISTS poc_users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(64) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            name VARCHAR(128) DEFAULT NULL,
            role ENUM('admin','poc') NOT NULL DEFAULT 'poc',
            active TINYINT(1) DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
    )

    await execute(
        """
        CREATE TABLE IF NOT EXISTS client_groups (
            id INT AUTO_INCREMENT PRIMARY KEY,
            account_id VARCHAR(64) NOT NULL,
            whatsapp_group_id VARCHAR(128) NOT NULL,
            group_name VARCHAR(255) DEFAULT NULL,
            project_name VARCHAR(255) DEFAULT NULL,
            assigned_poc_id INT DEFAULT NULL,
            ai_enabled TINYINT(1) DEFAULT 1,
            ai_mode ENUM('auto','approval','manual') NOT NULL DEFAULT 'approval',
            active TINYINT(1) DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY unique_group (account_id, whatsapp_group_id),
            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
            FOREIGN KEY (assigned_poc_id) REFERENCES poc_users(id) ON DELETE SET NULL
        )
        """
    )

    await execute(
        """
        CREATE TABLE IF NOT EXISTS group_members (
            id INT AUTO_INCREMENT PRIMARY KEY,
            group_id INT NOT NULL,
            member_jid VARCHAR(128) NOT NULL,
            member_phone VARCHAR(128) DEFAULT NULL,
            member_name VARCHAR(128) DEFAULT NULL,
            role ENUM('client','internal','poc') NOT NULL,
            active TINYINT(1) DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_member (group_id, member_jid),
            FOREIGN KEY (group_id) REFERENCES client_groups(id) ON DELETE CASCADE
        )
        """
    )
    # Upgrading a database created before member_phone existed. WhatsApp's
    # "lid" (linked/hidden id) privacy mode means member_jid alone is often
    # NOT the phone-number jid — see resolve_sender_role below — so we also
    # keep the resolved phone-number jid, when known, to match against.
    try:
        await execute("ALTER TABLE group_members ADD COLUMN member_phone VARCHAR(128) DEFAULT NULL")
    except Exception:  # noqa: BLE001 — 1060 = duplicate column, safe to ignore
        pass
    # Upgrading a database created before 'poc' was a valid member role —
    # lets a WhatsApp group member (picked via "Fetch group members", same
    # as a Client) be tagged as this group's internal point of contact,
    # instead of that requiring a separate dashboard login. See
    # set_poc_member below.
    try:
        await execute("ALTER TABLE group_members MODIFY COLUMN role ENUM('client','internal','poc') NOT NULL")
    except Exception:  # noqa: BLE001 — already includes 'poc', safe to ignore
        pass

    await execute(
        """
        CREATE TABLE IF NOT EXISTS conversation_threads (
            id INT AUTO_INCREMENT PRIMARY KEY,
            thread_key VARCHAR(64) NOT NULL UNIQUE,
            group_id INT NOT NULL,
            project_name VARCHAR(255) DEFAULT NULL,
            status ENUM('open','resolved','closed') NOT NULL DEFAULT 'open',
            ai_paused TINYINT(1) NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES client_groups(id) ON DELETE CASCADE
        )
        """
    )
    # Upgrading a database created before ai_paused existed.
    try:
        await execute("ALTER TABLE conversation_threads ADD COLUMN ai_paused TINYINT(1) NOT NULL DEFAULT 0")
    except Exception:  # noqa: BLE001 — 1060 = duplicate column, safe to ignore
        pass

    await execute(
        """
        CREATE TABLE IF NOT EXISTS group_messages (
            id INT AUTO_INCREMENT PRIMARY KEY,
            thread_id INT NOT NULL,
            group_id INT NOT NULL,
            whatsapp_message_id VARCHAR(128) DEFAULT NULL,
            sender_jid VARCHAR(128) DEFAULT NULL,
            sender_name VARCHAR(128) DEFAULT NULL,
            sender_role ENUM('client','internal','ai','unknown') NOT NULL DEFAULT 'unknown',
            direction ENUM('in','out') NOT NULL,
            body TEXT,
            message_type VARCHAR(32) DEFAULT 'text',
            reply_to_whatsapp_id VARCHAR(128) DEFAULT NULL,
            is_client_visible TINYINT(1) DEFAULT 1,
            processing_status ENUM('received','processed','ignored','failed') NOT NULL DEFAULT 'received',
            delivery_status ENUM('n_a','pending','sent','failed') NOT NULL DEFAULT 'n_a',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_wa_message (group_id, whatsapp_message_id),
            FOREIGN KEY (thread_id) REFERENCES conversation_threads(id) ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES client_groups(id) ON DELETE CASCADE
        )
        """
    )

    await execute(
        """
        CREATE TABLE IF NOT EXISTS project_context (
            id INT AUTO_INCREMENT PRIMARY KEY,
            group_id INT NOT NULL UNIQUE,
            description TEXT,
            scope TEXT,
            status VARCHAR(64) DEFAULT NULL,
            verified_updates TEXT,
            communication_instructions TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (group_id) REFERENCES client_groups(id) ON DELETE CASCADE
        )
        """
    )

    await execute(
        """
        CREATE TABLE IF NOT EXISTS pending_approvals (
            id INT AUTO_INCREMENT PRIMARY KEY,
            thread_id INT NOT NULL,
            group_id INT NOT NULL,
            client_message_id INT DEFAULT NULL,
            draft_reply TEXT,
            status ENUM('pending','approved','rejected','sent') NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            decided_at TIMESTAMP NULL DEFAULT NULL,
            decided_by INT DEFAULT NULL,
            FOREIGN KEY (thread_id) REFERENCES conversation_threads(id) ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES client_groups(id) ON DELETE CASCADE,
            FOREIGN KEY (client_message_id) REFERENCES group_messages(id) ON DELETE SET NULL
        )
        """
    )

    await execute(
        """
        CREATE TABLE IF NOT EXISTS poc_notifications (
            id INT AUTO_INCREMENT PRIMARY KEY,
            poc_id INT DEFAULT NULL,
            thread_id INT NOT NULL,
            client_message_id INT DEFAULT NULL,
            ai_reply_id INT DEFAULT NULL,
            status ENUM('unread','read') NOT NULL DEFAULT 'unread',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (poc_id) REFERENCES poc_users(id) ON DELETE SET NULL,
            FOREIGN KEY (thread_id) REFERENCES conversation_threads(id) ON DELETE CASCADE
        )
        """
    )

    await execute(
        """
        CREATE TABLE IF NOT EXISTS group_history_buffer (
            id INT AUTO_INCREMENT PRIMARY KEY,
            account_id VARCHAR(64) NOT NULL,
            group_jid VARCHAR(128) NOT NULL,
            whatsapp_message_id VARCHAR(128) DEFAULT NULL,
            sender_jid VARCHAR(128) DEFAULT NULL,
            sender_name VARCHAR(128) DEFAULT NULL,
            direction ENUM('in','out') NOT NULL,
            body TEXT,
            wa_timestamp INT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY unique_buffered_msg (account_id, group_jid, whatsapp_message_id)
        )
        """
    )

    print("[group_db] schema ready")


# ---------------------------------------------------------------------------
# POC users
# ---------------------------------------------------------------------------


async def get_poc_by_username(username: str):
    rows = await query("SELECT * FROM poc_users WHERE username = %s AND active = 1", (username,))
    return rows[0] if rows else None


async def get_poc_by_id(poc_id: int):
    rows = await query("SELECT id, username, name, role FROM poc_users WHERE id = %s", (poc_id,))
    return rows[0] if rows else None


async def create_poc_user(username: str, password_hash: str, name: str, role: str = "poc") -> int:
    return await execute(
        "INSERT INTO poc_users (username, password_hash, name, role) VALUES (%s, %s, %s, %s)",
        (username, password_hash, name, role),
    )


async def list_poc_users():
    return await query("SELECT id, username, name, role, active, created_at FROM poc_users ORDER BY created_at ASC")


async def count_poc_users() -> int:
    rows = await query("SELECT COUNT(*) AS c FROM poc_users")
    return rows[0]["c"]


# ---------------------------------------------------------------------------
# Client groups
# ---------------------------------------------------------------------------


async def create_group(account_id: str, whatsapp_group_id: str, group_name: str,
                        project_name: str, assigned_poc_id: int | None,
                        ai_mode: str = "approval") -> int:
    return await execute(
        """
        INSERT INTO client_groups
            (account_id, whatsapp_group_id, group_name, project_name, assigned_poc_id, ai_mode)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (account_id, whatsapp_group_id, group_name, project_name, assigned_poc_id, ai_mode),
    )


async def update_group(group_id: int, **fields):
    if not fields:
        return
    allowed = {"group_name", "project_name", "assigned_poc_id", "ai_enabled", "ai_mode", "active"}
    cols = [k for k in fields if k in allowed]
    if not cols:
        return
    set_clause = ", ".join(f"{c} = %s" for c in cols)
    params = tuple(fields[c] for c in cols) + (group_id,)
    await execute(f"UPDATE client_groups SET {set_clause} WHERE id = %s", params)


async def get_group_by_whatsapp_id(account_id: str, whatsapp_group_id: str):
    rows = await query(
        "SELECT * FROM client_groups WHERE account_id = %s AND whatsapp_group_id = %s",
        (account_id, whatsapp_group_id),
    )
    return rows[0] if rows else None


async def get_group(group_id: int):
    rows = await query("SELECT * FROM client_groups WHERE id = %s", (group_id,))
    return rows[0] if rows else None


async def delete_group(group_id: int):
    """Hard-deletes a client group and everything under it (members,
    threads, messages, approvals, notifications, project context) — every
    child table's FOREIGN KEY is ON DELETE CASCADE (see init_group_tables),
    so one DELETE is enough. This does NOT touch the underlying WhatsApp
    group itself — only this app's configuration of it.

    BEFORE deleting, the group's conversation is copied into
    group_history_buffer (keyed by account + WhatsApp group jid, not by
    group_id). So if the same WhatsApp group is added again later,
    import_group_history() restores the full conversation into the new
    group's thread instead of starting blank. Safe to repeat: the buffer
    has a UNIQUE key on (account, group, message id)."""
    grp = await get_group(group_id)
    if grp:
        await execute(
            """
            INSERT IGNORE INTO group_history_buffer
                (account_id, group_jid, whatsapp_message_id, sender_jid, sender_name,
                 direction, body, wa_timestamp)
            SELECT %s, %s, m.whatsapp_message_id, m.sender_jid, m.sender_name,
                   m.direction, m.body, UNIX_TIMESTAMP(m.created_at)
            FROM group_messages m
            WHERE m.group_id = %s AND m.whatsapp_message_id IS NOT NULL
            """,
            (grp["account_id"], grp["whatsapp_group_id"], group_id),
        )
    await execute("DELETE FROM client_groups WHERE id = %s", (group_id,))


async def list_groups(poc_id: int | None = None):
    """All groups, or (for a non-admin POC) only the groups assigned to
    them — enforces the 'POC sees only authorized conversations' rule at
    the query level, not just in the UI.

    poc_name/poc_phone here is the WhatsApp member tagged role='poc' (see
    set_poc_member) — the actual point of contact shown on the dashboard.
    login_poc_name is the separate, optional dashboard-login assignment
    (assigned_poc_id -> poc_users), used only for restricting who can log
    in and manage this group, not for display."""
    base_select = """
        SELECT g.*, p.name AS login_poc_name, a.phone_number, a.name AS account_name,
               mp.member_name AS poc_name, mp.member_phone AS poc_phone, mp.member_jid AS poc_jid
        FROM client_groups g
        LEFT JOIN poc_users p ON p.id = g.assigned_poc_id
        LEFT JOIN group_members mp ON mp.group_id = g.id AND mp.role = 'poc' AND mp.active = 1
        JOIN accounts a ON a.id = g.account_id
    """
    if poc_id is None:
        return await query(base_select + " ORDER BY g.created_at DESC")
    return await query(
        base_select + """ WHERE g.assigned_poc_id = %s OR g.id IN (
               SELECT p.group_id FROM projects p WHERE p.group_id IS NOT NULL AND (
                   p.main_manager_id = %s
                   OR EXISTS (SELECT 1 FROM project_departments pd WHERE pd.project_id = p.id AND pd.poc_user_id = %s)
                   OR EXISTS (SELECT 1 FROM project_department_members m WHERE m.project_id = p.id AND m.user_id = %s)))
               ORDER BY g.created_at DESC""",
        (poc_id, poc_id, poc_id, poc_id),
    )


async def poc_can_access_group(poc_role: str, poc_id: int, group_id: int) -> bool:
    """Admin: always. Legacy: the login POC assigned to the group. CRM: the
    project's Main Manager, or a POC / member of any of its departments."""
    if poc_role == "admin":
        return True
    rows = await query(
        "SELECT id FROM client_groups WHERE id = %s AND assigned_poc_id = %s", (group_id, poc_id)
    )
    if rows:
        return True
    rows = await query(
        """SELECT p.id FROM projects p WHERE p.group_id = %s AND (
               p.main_manager_id = %s
               OR EXISTS (SELECT 1 FROM project_departments pd WHERE pd.project_id = p.id AND pd.poc_user_id = %s)
               OR EXISTS (SELECT 1 FROM project_department_members m WHERE m.project_id = p.id AND m.user_id = %s))""",
        (group_id, poc_id, poc_id, poc_id),
    )
    return bool(rows)


# ---------------------------------------------------------------------------
# Group members (client vs internal) — sender-role resolution
# ---------------------------------------------------------------------------


async def set_poc_member(group_id: int, member_jid: str, member_name: str | None,
                          member_phone: str | None = None):
    """Tags one WhatsApp group member as this group's POC (point of
    contact) — the internal person responsible for this client, chosen the
    same way a Client is (via 'Fetch group members'), so there's no
    separate login/account to set up for it. At most one member per group
    can hold this role: any previous poc-tagged member is demoted back to
    'internal' first. A member can't be both 'client' and 'poc' — the
    frontend already filters clients out of the POC picker, but this is
    re-checked here too since payloads shouldn't be trusted blindly."""
    existing = await query(
        "SELECT role FROM group_members WHERE group_id = %s AND member_jid = %s", (group_id, member_jid)
    )
    if existing and existing[0]["role"] == "client":
        raise ValueError("This member is already assigned as a Client and can't also be the POC")
    await execute(
        "UPDATE group_members SET role = 'internal' WHERE group_id = %s AND role = 'poc'", (group_id,)
    )
    await upsert_member(group_id, member_jid, member_name, "poc", member_phone=member_phone)


async def clear_poc_member(group_id: int):
    await execute(
        "UPDATE group_members SET role = 'internal' WHERE group_id = %s AND role = 'poc'", (group_id,)
    )


async def upsert_member(group_id: int, member_jid: str, member_name: str | None, role: str,
                         member_phone: str | None = None):
    await execute(
        """
        INSERT INTO group_members (group_id, member_jid, member_phone, member_name, role)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            member_phone = COALESCE(VALUES(member_phone), member_phone),
            member_name = COALESCE(VALUES(member_name), member_name),
            role = VALUES(role),
            active = 1
        """,
        (group_id, member_jid, member_phone, member_name, role),
    )


async def member_has_role(group_id: int, member_jid: str, role: str) -> bool:
    rows = await query(
        "SELECT id FROM group_members WHERE group_id = %s AND member_jid = %s AND role = %s AND active = 1",
        (group_id, member_jid, role),
    )
    return bool(rows)


async def deactivate_member(group_id: int, member_jid: str):
    await execute(
        "UPDATE group_members SET active = 0 WHERE group_id = %s AND member_jid = %s",
        (group_id, member_jid),
    )


async def list_members(group_id: int):
    # active = 1 only — otherwise a "removed" member (deactivate_member
    # just flips the flag, it doesn't delete the row) kept showing up in
    # the dashboard's member list forever, even though resolve_sender_role
    # correctly stopped treating them as a roster member.
    return await query(
        "SELECT * FROM group_members WHERE group_id = %s AND active = 1 ORDER BY role, member_name",
        (group_id,),
    )


async def resolve_sender_role(group_id: int, sender_jid: str, sender_phone: str | None = None) -> str | None:
    """Returns 'client' / 'internal' for a known, active member of this
    group, or None if the sender isn't a configured member at all (i.e.
    identity can't be verified against the roster) — per spec 5.4: never
    rely on display name alone, and never auto-reply to an unverified
    sender.

    Matches on EITHER identifier: WhatsApp's "lid" privacy mode means the
    id a message actually arrives with (sender_jid) can differ from the
    phone-number jid an admin configured the member by (sender_phone, when
    the bridge could resolve it) — see bridge/index.js's
    getParticipantPhoneMap. Without this OR, a client added via "Fetch
    group members" (which stores the lid) or typed in by phone number
    (which stores the phone jid) could silently never match, depending on
    which form this particular message happened to arrive in."""
    if not sender_jid and not sender_phone:
        return None
    conditions = ["member_jid = %s"]
    params: list = [sender_jid or ""]
    if sender_phone:
        conditions.append("member_phone = %s")
        params.append(sender_phone)
    rows = await query(
        f"SELECT role FROM group_members WHERE group_id = %s AND active = 1 AND ({' OR '.join(conditions)})",
        (group_id, *params),
    )
    return rows[0]["role"] if rows else None


# ---------------------------------------------------------------------------
# Group history buffer — raw WhatsApp backlog for groups that AREN'T
# configured (yet). Whatever WhatsApp hands us via 'messaging-history.set'
# for a group jid lands here (see webhook_history in main.py), keyed only
# by account_id + group_jid — no client_groups row needs to exist yet. The
# moment that group IS added as a client group, import_group_history()
# drains this buffer into that group's first thread, so "Add group" shows
# the real backlog instead of starting from a blank conversation.
# ---------------------------------------------------------------------------


async def buffer_group_history(account_id: str, group_jid: str, item: dict):
    """Stash one historical WhatsApp message for a not-yet-configured (or
    already-configured — harmless either way) group. Safe to call
    repeatedly for the same message (INSERT IGNORE on the unique key)."""
    await execute(
        """
        INSERT IGNORE INTO group_history_buffer
            (account_id, group_jid, whatsapp_message_id, sender_jid, sender_name,
             direction, body, wa_timestamp)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (
            account_id, group_jid, item.get("messageId"), item.get("senderJid"),
            item.get("senderName"), item.get("direction"), item.get("body"),
            item.get("timestamp"),
        ),
    )


async def count_buffered_history(account_id: str, group_jid: str) -> int:
    rows = await query(
        "SELECT COUNT(*) AS c FROM group_history_buffer WHERE account_id = %s AND group_jid = %s",
        (account_id, group_jid),
    )
    return rows[0]["c"] if rows else 0


async def import_group_history(group_id: int, account_id: str, whatsapp_group_id: str) -> int:
    """Imports whatever's buffered for this group into its conversation
    thread — this is what makes a freshly-added client group start with
    its real backlog instead of an empty chat (spec: "purana chats bhi
    thread id mai store hona chahiye").

    Sender roles are resolved against the roster that was JUST saved for
    this group (upsert_member happens right before this is called — see
    group_routes.create_group), so a buffered message only becomes
    'client'/'internal' if its sender matches a member you actually
    configured; anyone else is stored as 'unknown' (visible for context,
    but — same as the live pipeline — never fed to the AI or replied to).
    Never triggers an AI draft or an approval: this is pure history, not a
    new inbound message.

    Returns how many messages were imported (0 if nothing was buffered)."""
    rows = await query(
        "SELECT * FROM group_history_buffer WHERE account_id = %s AND group_jid = %s "
        "ORDER BY wa_timestamp ASC, id ASC",
        (account_id, whatsapp_group_id),
    )
    # Always make sure the group has its thread right away, even when there is
    # no backlog yet, so the thread shows up in the dashboard as soon as the
    # group is added (not only after the first new client message).
    project_name = None
    grp = await get_group(group_id)
    if grp:
        project_name = grp.get("project_name")
    thread = await get_or_create_open_thread(group_id, project_name)
    if not rows:
        return 0

    imported = 0
    for row in rows:
        # The bridge already tells us direction: 'out' is something the
        # account holder (or another team device on that number) sent —
        # same treatment as a live from_me message (see _handle_group_message).
        if row["direction"] == "out":
            role = "internal"
        else:
            role = await resolve_sender_role(group_id, row["sender_jid"]) or "unknown"
        created_at = (
            datetime.fromtimestamp(row["wa_timestamp"]) if row.get("wa_timestamp") else None
        )
        saved = await save_group_message(
            thread_id=thread["id"], group_id=group_id,
            whatsapp_message_id=row["whatsapp_message_id"],
            sender_jid=row["sender_jid"], sender_name=row["sender_name"],
            sender_role=role, direction=row["direction"], body=row["body"],
            processing_status="processed" if role != "unknown" else "ignored",
            delivery_status="sent" if row["direction"] == "out" else "n_a",
            created_at=created_at,
        )
        if saved is not None:
            imported += 1

    # The buffer is intentionally KEPT (not drained). Re-importing is safe
    # because save_group_message is idempotent on (group_id, message id), and
    # keeping it is what lets a deleted-then-re-added group get its full
    # history back (see delete_group, which also archives into this buffer).
    return imported


# ---------------------------------------------------------------------------
# Conversation threads
# ---------------------------------------------------------------------------


def _slugify(text: str) -> str:
    import re
    slug = re.sub(r"[^a-z0-9]+", "_", (text or "").lower()).strip("_")
    return slug[:40] or "group"


async def get_or_create_open_thread(group_id: int, project_name: str | None):
    """The core Thread Manager rule: reuse the group's open thread if one
    exists; otherwise create a new one. Never creates a new thread per
    message — only when none is currently open (spec section 5)."""
    rows = await query(
        "SELECT * FROM conversation_threads WHERE group_id = %s AND status = 'open' "
        "ORDER BY created_at DESC LIMIT 1",
        (group_id,),
    )
    if rows:
        return rows[0]

    slug = _slugify(project_name or f"group{group_id}")
    seq = await query(
        "SELECT COUNT(*) AS c FROM conversation_threads WHERE group_id = %s", (group_id,)
    )
    n = seq[0]["c"] + 1
    thread_key = f"thread_{slug}_{n:03d}"
    # Extremely unlikely, but guarantee uniqueness even under a race.
    while (await query("SELECT id FROM conversation_threads WHERE thread_key = %s", (thread_key,))):
        thread_key = f"thread_{slug}_{n:03d}_{secrets.token_hex(2)}"

    thread_id = await execute(
        "INSERT INTO conversation_threads (thread_key, group_id, project_name, status) "
        "VALUES (%s, %s, %s, 'open')",
        (thread_key, group_id, project_name),
    )
    return {
        "id": thread_id, "thread_key": thread_key, "group_id": group_id,
        "project_name": project_name, "status": "open", "ai_paused": 0,
    }


async def set_thread_status(thread_id: int, status: str):
    await execute("UPDATE conversation_threads SET status = %s WHERE id = %s", (status, thread_id))


async def set_thread_ai_paused(thread_id: int, paused: bool):
    """Human handoff (spec 12): pause/resume AI replies for one
    conversation thread specifically, independent of the group's overall
    ai_mode — so a POC can take over one client's thread without
    disabling the AI for the whole group."""
    await execute("UPDATE conversation_threads SET ai_paused = %s WHERE id = %s", (1 if paused else 0, thread_id))


async def get_thread(thread_id: int):
    rows = await query("SELECT * FROM conversation_threads WHERE id = %s", (thread_id,))
    return rows[0] if rows else None


async def list_threads_for_group(group_id: int):
    return await query(
        "SELECT * FROM conversation_threads WHERE group_id = %s ORDER BY created_at DESC", (group_id,)
    )


async def human_replied_in_thread_since(thread_id: int, since) -> bool:
    """True if a real person (account holder typing directly, or a POC's
    manual reply) sent an outbound message in this thread after `since`.
    Used by the delayed group auto-reply to back off if a human already
    took over while the AI's reply was waiting to go out."""
    rows = await query(
        "SELECT id FROM group_messages WHERE thread_id = %s AND direction = 'out' "
        "AND sender_role = 'internal' AND created_at > %s LIMIT 1",
        (thread_id, since),
    )
    return bool(rows)


# ---------------------------------------------------------------------------
# Messages (idempotent on whatsapp_message_id)
# ---------------------------------------------------------------------------


async def message_already_processed(group_id: int, whatsapp_message_id: str | None) -> bool:
    """Duplicate-webhook protection: WhatsApp/Baileys can redeliver the
    same event. If we've already stored this exact WhatsApp message id
    for this group, skip reprocessing it entirely."""
    if not whatsapp_message_id:
        return False
    rows = await query(
        "SELECT id FROM group_messages WHERE group_id = %s AND whatsapp_message_id = %s",
        (group_id, whatsapp_message_id),
    )
    return bool(rows)


async def save_group_message(
    thread_id: int, group_id: int, whatsapp_message_id: str | None,
    sender_jid: str | None, sender_name: str | None, sender_role: str,
    direction: str, body: str, reply_to_whatsapp_id: str | None = None,
    is_client_visible: bool = True, processing_status: str = "received",
    delivery_status: str = "n_a", created_at: datetime | None = None,
) -> int | None:
    """Inserts a message row. Relies on the UNIQUE(group_id,
    whatsapp_message_id) key as a second line of defence against
    duplicates (INSERT IGNORE-style via ON DUPLICATE KEY no-op), so even
    a race between two webhook deliveries can't double-insert.

    created_at is only ever passed explicitly by the history-import path
    (see import_group_history) so imported messages keep their real
    WhatsApp timestamp instead of "now" — live messages leave this None
    and get the column's own CURRENT_TIMESTAMP default."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            if created_at is not None:
                await cur.execute(
                    """
                    INSERT IGNORE INTO group_messages
                        (thread_id, group_id, whatsapp_message_id, sender_jid, sender_name,
                         sender_role, direction, body, reply_to_whatsapp_id, is_client_visible,
                         processing_status, delivery_status, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        thread_id, group_id, whatsapp_message_id, sender_jid, sender_name,
                        sender_role, direction, body, reply_to_whatsapp_id, 1 if is_client_visible else 0,
                        processing_status, delivery_status, created_at,
                    ),
                )
            else:
                await cur.execute(
                    """
                    INSERT IGNORE INTO group_messages
                        (thread_id, group_id, whatsapp_message_id, sender_jid, sender_name,
                         sender_role, direction, body, reply_to_whatsapp_id, is_client_visible,
                         processing_status, delivery_status)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        thread_id, group_id, whatsapp_message_id, sender_jid, sender_name,
                        sender_role, direction, body, reply_to_whatsapp_id, 1 if is_client_visible else 0,
                        processing_status, delivery_status,
                    ),
                )
            if cur.rowcount == 0:
                # Already existed (duplicate whatsapp_message_id) — nothing inserted.
                return None
            return cur.lastrowid
    return None


async def update_message_status(message_id: int, *, processing_status: str | None = None,
                                 delivery_status: str | None = None):
    sets, params = [], []
    if processing_status:
        sets.append("processing_status = %s"); params.append(processing_status)
    if delivery_status:
        sets.append("delivery_status = %s"); params.append(delivery_status)
    if not sets:
        return
    params.append(message_id)
    await execute(f"UPDATE group_messages SET {', '.join(sets)} WHERE id = %s", tuple(params))


async def load_thread_history(thread_id: int, limit: int = 30):
    """Most recent `limit` messages, oldest-first — this is the bounded
    context-window strategy (spec 6): recent messages only, capped, so we
    never ship unbounded history to the LLM."""
    rows = await query(
        "SELECT sender_name, sender_role, direction, body, created_at FROM group_messages "
        "WHERE thread_id = %s ORDER BY id DESC LIMIT %s",
        (thread_id, limit),
    )
    rows.reverse()
    return rows


async def list_messages_for_thread(thread_id: int):
    return await query(
        "SELECT * FROM group_messages WHERE thread_id = %s ORDER BY id ASC", (thread_id,)
    )


async def list_recent_messages(group_id: int | None = None, limit: int = 100, failed_only: bool = False):
    conditions = []
    params = []
    if group_id is not None:
        conditions.append("group_id = %s"); params.append(group_id)
    if failed_only:
        conditions.append("(processing_status = 'failed' OR delivery_status = 'failed')")
    where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
    params.append(limit)
    return await query(
        f"SELECT * FROM group_messages {where} ORDER BY id DESC LIMIT %s", tuple(params)
    )


# ---------------------------------------------------------------------------
# Project context
# ---------------------------------------------------------------------------


async def get_project_context(group_id: int):
    rows = await query("SELECT * FROM project_context WHERE group_id = %s", (group_id,))
    return rows[0] if rows else None


async def upsert_project_context(group_id: int, description: str | None = None,
                                  scope: str | None = None, status: str | None = None,
                                  communication_instructions: str | None = None):
    existing = await get_project_context(group_id)
    if existing:
        sets, params = [], []
        for col, val in (
            ("description", description), ("scope", scope), ("status", status),
            ("communication_instructions", communication_instructions),
        ):
            if val is not None:
                sets.append(f"{col} = %s"); params.append(val)
        if sets:
            params.append(group_id)
            await execute(f"UPDATE project_context SET {', '.join(sets)} WHERE group_id = %s", tuple(params))
    else:
        await execute(
            """
            INSERT INTO project_context (group_id, description, scope, status, communication_instructions, verified_updates)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (group_id, description, scope, status, communication_instructions, json.dumps([])),
        )


async def add_verified_update(group_id: int, text: str, added_by: str | None = None):
    """Appends a timestamped, human-supplied project update. The AI is
    only ever given updates added this way — never anything it inferred
    itself — so it can't treat its own prior guesses as verified fact."""
    ctx = await get_project_context(group_id)
    updates = json.loads(ctx["verified_updates"]) if ctx and ctx.get("verified_updates") else []
    updates.append({"text": text, "added_by": added_by, "at": datetime.utcnow().isoformat()})
    if ctx:
        await execute(
            "UPDATE project_context SET verified_updates = %s WHERE group_id = %s",
            (json.dumps(updates), group_id),
        )
    else:
        await execute(
            "INSERT INTO project_context (group_id, verified_updates) VALUES (%s, %s)",
            (group_id, json.dumps(updates)),
        )


# ---------------------------------------------------------------------------
# Human-approval workflow (Mode B)
# ---------------------------------------------------------------------------


async def create_pending_approval(thread_id: int, group_id: int, client_message_id: int | None,
                                   draft_reply: str) -> int:
    return await execute(
        """
        INSERT INTO pending_approvals (thread_id, group_id, client_message_id, draft_reply, status)
        VALUES (%s, %s, %s, %s, 'pending')
        """,
        (thread_id, group_id, client_message_id, draft_reply),
    )


async def get_pending_approval(approval_id: int):
    rows = await query("SELECT * FROM pending_approvals WHERE id = %s", (approval_id,))
    return rows[0] if rows else None


async def list_pending_approvals(group_id: int | None = None):
    if group_id is not None:
        return await query(
            "SELECT * FROM pending_approvals WHERE status = 'pending' AND group_id = %s ORDER BY created_at ASC",
            (group_id,),
        )
    return await query(
        "SELECT * FROM pending_approvals WHERE status = 'pending' ORDER BY created_at ASC"
    )


async def decide_approval(approval_id: int, status: str, decided_by: int | None):
    await execute(
        "UPDATE pending_approvals SET status = %s, decided_at = NOW(), decided_by = %s WHERE id = %s",
        (status, decided_by, approval_id),
    )


# ---------------------------------------------------------------------------
# POC notifications
# ---------------------------------------------------------------------------


async def create_notification(thread_id: int, poc_id: int | None, client_message_id: int | None,
                               ai_reply_id: int | None = None):
    return await execute(
        """
        INSERT INTO poc_notifications (poc_id, thread_id, client_message_id, ai_reply_id, status)
        VALUES (%s, %s, %s, %s, 'unread')
        """,
        (poc_id, thread_id, client_message_id, ai_reply_id),
    )


async def list_notifications(poc_id: int, unread_only: bool = False):
    where = "WHERE poc_id = %s" + (" AND status = 'unread'" if unread_only else "")
    return await query(
        f"SELECT * FROM poc_notifications {where} ORDER BY created_at DESC", (poc_id,)
    )


async def mark_notification_read(notification_id: int):
    await execute("UPDATE poc_notifications SET status = 'read' WHERE id = %s", (notification_id,))


# ---------------------------------------------------------------------------
# Dashboard overview stats
# ---------------------------------------------------------------------------


async def overview_stats():
    groups = await query("SELECT COUNT(*) AS c FROM client_groups WHERE active = 1")
    open_threads = await query("SELECT COUNT(*) AS c FROM conversation_threads WHERE status = 'open'")
    pending = await query("SELECT COUNT(*) AS c FROM pending_approvals WHERE status = 'pending'")
    failed = await query(
        "SELECT COUNT(*) AS c FROM group_messages WHERE processing_status = 'failed' OR delivery_status = 'failed'"
    )
    recent = await query(
        """
        SELECT gm.*, g.group_name, g.project_name FROM group_messages gm
        JOIN client_groups g ON g.id = gm.group_id
        WHERE gm.direction = 'in' ORDER BY gm.id DESC LIMIT 10
        """
    )
    return {
        "active_groups": groups[0]["c"],
        "open_conversations": open_threads[0]["c"],
        "pending_approvals": pending[0]["c"],
        "failed_messages": failed[0]["c"],
        "recent_client_messages": recent,
    }
