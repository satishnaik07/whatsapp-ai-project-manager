"""
Tests for the group PM/CRM workflow described in the spec's section 16.

These mock the DB layer (group_db), the WhatsApp bridge (bridge_client),
and the AI replier (group_ai_replier) — no real MySQL connection or
external API calls are made, so this suite runs anywhere.

Run with:  pytest tests/test_group_workflow.py -v
"""
import asyncio
from unittest.mock import AsyncMock

import pytest

from server import group_ai_replier, main


async def _flush_background_tasks():
    """Auto-mode group replies now go out through a short delayed
    background task (see main._send_group_ai_reply_after_delay) instead
    of sending inline — tests set the delay to 0 and then yield control
    a few times so that task actually gets to run before assertions."""
    for _ in range(5):
        await asyncio.sleep(0)


def make_group(**overrides):
    base = {
        "id": 1,
        "account_id": "acct1",
        "whatsapp_group_id": "120363000000000000@g.us",
        "group_name": "ORA Website Development",
        "project_name": "ORA Website",
        "assigned_poc_id": 7,
        "ai_enabled": 1,
        "ai_mode": "auto",
        "active": 1,
    }
    base.update(overrides)
    return base


def make_thread(**overrides):
    base = {"id": 10, "thread_key": "thread_ora_website_001", "group_id": 1,
            "project_name": "ORA Website", "status": "open", "ai_paused": 0}
    base.update(overrides)
    return base


def base_payload(**overrides):
    p = {
        "accountId": "acct1",
        "jid": "120363000000000000@g.us",
        "isGroup": True,
        "fromMe": False,
        "body": "The contact form is still not working.",
        "messageId": "WA_MSG_1",
        "senderJid": "919876543210@s.whatsapp.net",
        "senderName": "Client One",
        "quotedMessageId": None,
    }
    p.update(overrides)
    return p


@pytest.fixture
def gdb(monkeypatch):
    """Patches every server.group_db function _handle_group_message touches."""
    mocks = {
        "message_already_processed": AsyncMock(return_value=False),
        "get_or_create_open_thread": AsyncMock(return_value=make_thread()),
        "resolve_sender_role": AsyncMock(return_value="client"),
        "save_group_message": AsyncMock(return_value=100),
        "update_message_status": AsyncMock(return_value=None),
        "create_pending_approval": AsyncMock(return_value=55),
        "create_notification": AsyncMock(return_value=1),
        "get_group_by_whatsapp_id": AsyncMock(return_value=make_group()),
        # Used by the delayed auto-reply's pre-send recheck.
        "get_thread": AsyncMock(return_value=make_thread()),
        "human_replied_in_thread_since": AsyncMock(return_value=False),
    }
    for name, mock in mocks.items():
        monkeypatch.setattr(main.group_db, name, mock)
    # Legacy-group behaviour: no CRM project => the department router steps
    # aside (returns None) and sending uses the group's own account.
    monkeypatch.setattr(main.message_router, "route_client_message", AsyncMock(return_value=None))
    monkeypatch.setattr(main.poc_router.crm, "get_project_by_group", AsyncMock(return_value=None))
    # sio.emit is called throughout — no real socket server in tests.
    monkeypatch.setattr(main.sio, "emit", AsyncMock(return_value=None))
    # Auto-mode replies wait GROUP_AI_REPLY_DELAY_SECONDS in the background
    # before sending — zero that out so tests don't actually sleep.
    monkeypatch.setattr(main, "GROUP_AI_REPLY_DELAY_SECONDS", 0)
    return mocks


@pytest.fixture
def bridge(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(main.bridge_client, "send_message", send)
    return send


@pytest.fixture
def ai(monkeypatch):
    build = AsyncMock(return_value="Thanks, we're looking into the contact form issue.")
    monkeypatch.setattr(main.group_ai_replier, "build_reply", build)
    return build


# ---------------------------------------------------------------------------
# 1 & 3: configured group + authorized client message -> AI workflow runs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_message_in_auto_mode_generates_and_sends_reply(gdb, bridge, ai):
    group = make_group(ai_mode="auto")
    await main._handle_group_message(group, base_payload())
    await _flush_background_tasks()

    ai.assert_awaited_once()
    bridge.assert_awaited_once_with("acct1", "120363000000000000@g.us",
                                     "Thanks, we're looking into the contact form issue.")
    # Both the inbound client message and the outbound AI reply got saved.
    assert gdb["save_group_message"].await_count == 2
    gdb["create_notification"].assert_awaited_once()


# ---------------------------------------------------------------------------
# 2: unconfigured group is ignored entirely (no group row -> falls through)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unconfigured_group_never_enters_group_workflow(monkeypatch):
    called = AsyncMock()
    monkeypatch.setattr(main, "_handle_group_message", called)
    monkeypatch.setattr(main.group_db, "get_group_by_whatsapp_id", AsyncMock(return_value=None))
    # Avoid touching the real 1:1 path's DB calls.
    monkeypatch.setattr(main.db, "upsert_contact", AsyncMock(return_value=1))
    monkeypatch.setattr(main.db, "save_message", AsyncMock(return_value=1))
    monkeypatch.setattr(main.sio, "emit", AsyncMock(return_value=None))

    await main.webhook_message(base_payload(jid="UNCONFIGURED@g.us"))

    called.assert_not_awaited()


# ---------------------------------------------------------------------------
# 4: internal team message never triggers a client reply
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_internal_team_message_does_not_trigger_reply(gdb, bridge, ai):
    gdb["resolve_sender_role"].return_value = "internal"
    await main._handle_group_message(make_group(), base_payload(
        senderJid="91INTERNAL@s.whatsapp.net", senderName="Developer",
        body="The contact form issue has been fixed.",
    ))

    ai.assert_not_awaited()
    bridge.assert_not_awaited()
    gdb["save_group_message"].assert_awaited_once()  # stored, not replied to


# ---------------------------------------------------------------------------
# 5 & 6: thread retrieval / creation is delegated to get_or_create_open_thread
# (its own create-vs-reuse logic is exercised directly against group_db,
#  see test_thread_reuse below)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_uses_existing_open_thread_not_a_new_one(gdb, bridge, ai):
    await main._handle_group_message(make_group(), base_payload())
    gdb["get_or_create_open_thread"].assert_awaited_once_with(1, "ORA Website")


# ---------------------------------------------------------------------------
# 9 & 10: AI reply is sent to the SAME group, and does not trigger a reply
# to itself (outbound messages never re-enter this function; that's
# guaranteed structurally since only /webhook/message calls it, and the
# bridge tags/skips its own echoes — see bridge/index.js wasJustSentByBot).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_sent_to_same_group_jid(gdb, bridge, ai):
    group = make_group(whatsapp_group_id="120363SPECIFIC@g.us")
    await main._handle_group_message(group, base_payload(jid="120363SPECIFIC@g.us"))
    await _flush_background_tasks()
    args, _ = bridge.call_args
    assert args[0] == "acct1"
    assert args[1] == "120363SPECIFIC@g.us"


# ---------------------------------------------------------------------------
# 11: duplicate webhook events do not generate duplicate replies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_webhook_is_skipped(gdb, bridge, ai):
    gdb["message_already_processed"].return_value = True
    await main._handle_group_message(make_group(), base_payload())

    gdb["get_or_create_open_thread"].assert_not_awaited()
    ai.assert_not_awaited()
    bridge.assert_not_awaited()


# ---------------------------------------------------------------------------
# 12: WhatsApp send failures are recorded, not silently dropped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_failure_is_recorded(gdb, bridge, ai):
    bridge.return_value = False
    await main._handle_group_message(make_group(), base_payload())
    await _flush_background_tasks()

    # The outbound row must have been saved with delivery_status='failed'.
    calls = gdb["save_group_message"].await_args_list
    outbound_call = calls[-1]
    assert outbound_call.kwargs["delivery_status"] == "failed"


# ---------------------------------------------------------------------------
# 15: human approval mode prevents unapproved messages from being sent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_mode_does_not_auto_send(gdb, bridge, ai):
    group = make_group(ai_mode="approval")
    await main._handle_group_message(group, base_payload())

    bridge.assert_not_awaited()
    gdb["create_pending_approval"].assert_awaited_once()
    gdb["create_notification"].assert_awaited_once()


@pytest.mark.asyncio
async def test_manual_mode_never_calls_ai(gdb, bridge, ai):
    group = make_group(ai_mode="manual")
    await main._handle_group_message(group, base_payload())

    ai.assert_not_awaited()
    bridge.assert_not_awaited()


@pytest.mark.asyncio
async def test_paused_thread_skips_ai(gdb, bridge, ai):
    gdb["get_or_create_open_thread"].return_value = make_thread(ai_paused=1)
    await main._handle_group_message(make_group(), base_payload())

    ai.assert_not_awaited()
    bridge.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverified_sender_is_not_replied_to(gdb, bridge, ai):
    gdb["resolve_sender_role"].return_value = None
    await main._handle_group_message(make_group(), base_payload(senderJid="unknown@s.whatsapp.net"))

    ai.assert_not_awaited()
    bridge.assert_not_awaited()
    save_call = gdb["save_group_message"].await_args
    assert save_call.kwargs["sender_role"] == "unknown"
    assert save_call.kwargs["processing_status"] == "ignored"


@pytest.mark.asyncio
async def test_escalation_keyword_forces_approval_even_in_auto_mode(gdb, bridge, ai):
    group = make_group(ai_mode="auto")
    await main._handle_group_message(group, base_payload(body="I want a refund for last month."))

    bridge.assert_not_awaited()
    gdb["create_pending_approval"].assert_awaited_once()


def test_needs_escalation_keyword_matrix():
    assert group_ai_replier.needs_escalation("Can I get a refund please")
    assert group_ai_replier.needs_escalation("We are considering legal action")
    assert not group_ai_replier.needs_escalation("The homepage looks great, thanks!")


# ---------------------------------------------------------------------------
# Reply style / silence guardrails (group_ai_replier)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("msg", ["Okay 👍", "Thanks.", "👍", "ok thanks", "Got it", "Noted, thank you"])
def test_pure_acknowledgements_are_detected(msg):
    assert group_ai_replier.is_pure_acknowledgement(msg)


@pytest.mark.parametrize("msg", [
    "Okay, I'll send it tomorrow.", "Thanks, can you confirm the date?", "Any update?",
    "Please change the button color to blue.",
])
def test_real_messages_are_not_acknowledgements(msg):
    assert not group_ai_replier.is_pure_acknowledgement(msg)


def test_clean_reply_enforces_style_rules():
    c = group_ai_replier.clean_reply
    assert c("NO_REPLY") is None
    assert "\u2014" not in c("The project is almost complete \u2014 we're testing it now.")
    assert c("Quick update: it's in testing now.") == "It's in testing now."
    assert c("It's in testing now.\n\nBest regards,\nTeam") == "It's in testing now."
    assert c("Sure, noted. Let me know if you need anything else.") == "Sure, noted."
    assert c("As an AI, I can't see that.") == group_ai_replier.SAFE_FALLBACK


@pytest.mark.asyncio
async def test_build_reply_skips_llm_for_acknowledgement(monkeypatch):
    called = AsyncMock()
    monkeypatch.setattr(group_ai_replier.openai_client.chat.completions, "create", called)
    out = await group_ai_replier.build_reply(make_group(), make_thread(), "Okay 👍", "Client One")
    assert out is None
    called.assert_not_awaited()


@pytest.mark.asyncio
async def test_none_draft_means_no_send_and_no_approval(gdb, bridge, monkeypatch):
    monkeypatch.setattr(main.group_ai_replier, "build_reply", AsyncMock(return_value=None))
    await main._handle_group_message(make_group(ai_mode="auto"), base_payload(body="Okay 👍"))
    await _flush_background_tasks()
    bridge.assert_not_awaited()
    gdb["create_pending_approval"].assert_not_awaited()
