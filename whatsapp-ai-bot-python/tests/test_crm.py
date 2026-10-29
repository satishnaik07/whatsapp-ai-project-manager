"""
CRM / department-routing tests against a REAL MySQL or MariaDB database.

They need a server on DB_HOST:DB_PORT (default 127.0.0.1:3307, override with
TEST_DB_PORT / DB_HOST / DB_USER / DB_PASSWORD) and always use a database
whose name ends in `_test` (default `whatsapp_ai_bot_test`, dropped and
recreated per run). If no server is reachable these tests are SKIPPED, so the
rest of the suite still runs anywhere.

The AI department classifier and reply writer are replaced by deterministic
stubs, so these tests verify OUR routing logic (thresholds, thread memory,
POC availability, fallback, notifications, sending account, permissions),
not the language model's accuracy.
"""
import asyncio
import os
from unittest.mock import AsyncMock

import aiomysql
import pytest
import pytest_asyncio

from server import bridge_client, crm_schema, db, group_db, group_routes, main
from server.repositories import crm
from server.routes import conversations, projects, tasks, updates, users
from server.routes import dashboard as dashboard_routes
from server.services import department_classifier, message_router

DB_NAME = os.environ["DB_NAME"]
assert DB_NAME.endswith("_test"), "refusing to run CRM tests against a non-test database"


def _server_up() -> bool:
    async def probe():
        c = await aiomysql.connect(host=db.DB_HOST, port=db.DB_PORT, user=db.DB_USER, password=db.DB_PASSWORD, connect_timeout=3)
        c.close()
    try:
        asyncio.run(probe())
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _server_up(), reason="no MySQL/MariaDB reachable for CRM tests")


@pytest.fixture(scope="session")
def schema():
    async def setup():
        c = await aiomysql.connect(host=db.DB_HOST, port=db.DB_PORT, user=db.DB_USER, password=db.DB_PASSWORD)
        async with c.cursor() as cur:
            await cur.execute(f"DROP DATABASE IF EXISTS `{DB_NAME}`")
        c.close()
        db._pool = None
        await db.init_db()
        await group_db.init_group_tables()
        await crm_schema.init_crm_tables()
        db._pool.close()
        await db._pool.wait_closed()
        db._pool = None
    asyncio.run(setup())


@pytest_asyncio.fixture
async def dbc(schema):
    db._pool = None
    pool = await db.get_pool()
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SET FOREIGN_KEY_CHECKS = 0")
            await cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")
            for (t,) in await cur.fetchall():
                await cur.execute(f"TRUNCATE TABLE `{t}`")
            await cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    yield
    pool.close()
    await pool.wait_closed()
    db._pool = None


# ----------------------------------------------------------------- seed data
CLIENT1 = "911111111111@s.whatsapp.net"
CLIENT2 = "922222222222@s.whatsapp.net"
INTERNAL = "933333333333@s.whatsapp.net"


async def _user(username, role, name=None):
    uid = await crm.create_user(username, "x", name or username.title(), role)
    return {"id": uid, "username": username, "name": name or username.title(), "role": role}


async def seed(tech_connected=True, ai_mode="auto"):
    S = {}
    for a, st in (("acc_main", "connected"), ("acc_tech", "connected" if tech_connected else "disconnected"),
                  ("acc_design", "connected"), ("acc_seo", "connected")):
        await db.execute("INSERT INTO accounts (id, status) VALUES (%s,%s)", (a, st))
    S["admin"] = await _user("admin", "admin")
    S["satish"] = await _user("satish", "main_manager")
    S["rahul"] = await _user("rahul", "department_poc")
    S["priya"] = await _user("priya", "department_poc")
    S["akash"] = await _user("akash", "department_poc")
    S["amit"] = await _user("amit", "department_member")
    S["neha"] = await _user("neha", "department_member")
    for u, a in (("satish", "acc_main"), ("rahul", "acc_tech"), ("priya", "acc_design"), ("akash", "acc_seo")):
        await crm.map_account_user(a, S[u]["id"])
    S["tech"] = await crm.create_department("Tech", "Website/software development, bugs, APIs, login, dashboards")
    S["design"] = await crm.create_department("Design", "UI/UX, logos, banners, homepage visuals")
    S["seo"] = await crm.create_department("SEO", "Google ranking, keywords, backlinks")
    gid = await group_db.create_group("acc_main", "120363@g.us", "ABC Website Development", "ABC Website",
                                      S["satish"]["id"], ai_mode)
    S["group_id"] = gid
    await group_db.upsert_member(gid, CLIENT1, "Client One", "client")
    await group_db.upsert_member(gid, CLIENT2, "Client Two", "client")
    await group_db.upsert_member(gid, INTERNAL, "Internal Dev", "internal")
    S["client_id"] = await crm.create_client("ABC Company", "ABC Company")
    S["project_id"] = await crm.create_project(S["client_id"], "ABC Website", status="active",
                                               main_manager_id=S["satish"]["id"], group_id=gid)
    await projects._assign_department(S["project_id"], S["tech"], S["rahul"]["id"], [S["amit"]["id"]], S["admin"])
    await projects._assign_department(S["project_id"], S["design"], S["priya"]["id"], [S["neha"]["id"]], S["admin"])
    await projects._assign_department(S["project_id"], S["seo"], S["akash"]["id"], [], S["admin"])
    return S


class StubClassifier:
    """Deterministic stand-in for the LLM. Records what the router passed in."""
    def __init__(self):
        self.calls = []
        self.fail = False

    async def __call__(self, message, departments, history_text, current_department=None, routing_status=None):
        self.calls.append({"message": message, "current_department": current_department,
                           "routing_status": routing_status, "history": history_text})
        if self.fail:
            raise RuntimeError("DeepSeek unavailable")
        m = message.lower()
        if any(w in m for w in ("login", "api", "dashboard")):
            return _r("Tech", 0.95)
        if any(w in m for w in ("design", "banner", "logo")):
            return _r("Design", 0.93)
        if "google ranking" in m or "seo" in m:
            return _r("SEO", 0.92)
        if "payment ke regarding" in m:
            return _r(None, 0.3)
        if "wo bhi" in m and current_department:        # continuation, no keyword
            return _r(current_department, 0.9)
        return _r(None, 0.2)


def _r(dept, conf, needs_manager=False, cross=False):
    return {"department": dept, "confidence": conf, "reason": "stub", "needs_manager": needs_manager, "cross_department": cross}


@pytest.fixture
def clf(monkeypatch):
    c = StubClassifier()
    monkeypatch.setattr(department_classifier, "classify", c)
    return c


async def send_client_message(S, body, sender=CLIENT1, wa_id=None, thread=None):
    """Store a client message the way the webhook would, then route it."""
    group = await group_db.get_group(S["group_id"])
    thread = await group_db.get_or_create_open_thread(group["id"], group["project_name"])
    mid = await group_db.save_group_message(thread["id"], group["id"], wa_id or f"WA{os.urandom(4).hex()}", sender,
                                            "Client One", "client", "in", body)
    thread = await group_db.get_thread(thread["id"])
    decision = await message_router.route_client_message(group, thread, mid, body, "Client One")
    return decision, await group_db.get_thread(thread["id"])


async def notified_users(S):
    """Who was told about a client MESSAGE (setup notifications such as 'you were assigned' are not counted)."""
    rows = await db.query("SELECT DISTINCT poc_id FROM poc_notifications WHERE kind IN "
                          "('client_message','manager_fallback','escalation','assignment','routing_failure')")
    return {r["poc_id"] for r in rows}


# ------------------------------------------------------ 1-3: department routing
@pytest.mark.asyncio
async def test_1_tech_message_routes_to_tech_poc_only(dbc, clf):
    S = await seed()
    d, thread = await send_client_message(S, "Login API is not working.")
    assert d.department_name == "Tech"
    assert d.poc_user_id == S["rahul"]["id"] and d.account_id == "acc_tech"
    assert thread["current_department_id"] == S["tech"] and thread["assigned_poc_id"] == S["rahul"]["id"]
    assert await notified_users(S) == {S["rahul"]["id"]}          # nobody else is notified


@pytest.mark.asyncio
async def test_2_design_message_routes_to_design_poc_only(dbc, clf):
    S = await seed()
    d, _ = await send_client_message(S, "Homepage banner design change karna hai.")
    assert (d.department_name, d.poc_user_id, d.account_id) == ("Design", S["priya"]["id"], "acc_design")
    assert await notified_users(S) == {S["priya"]["id"]}


@pytest.mark.asyncio
async def test_3_seo_message_routes_to_seo_poc_only(dbc, clf):
    S = await seed()
    d, _ = await send_client_message(S, "Google ranking improve nahi ho rahi.")
    assert (d.department_name, d.poc_user_id) == ("SEO", S["akash"]["id"])
    assert await notified_users(S) == {S["akash"]["id"]}


# --------------------------------------------------- 4: unknown -> Main Manager
@pytest.mark.asyncio
async def test_4_unknown_goes_to_main_manager_and_asks_clarification(dbc, clf):
    S = await seed()
    d, thread = await send_client_message(S, "Payment ke regarding baat karni hai.")
    assert d.poc_user_id == S["satish"]["id"] and d.account_id == "acc_main"
    assert thread["routing_status"] == "needs_clarification" and d.ask_clarification
    assert d.ai_context["ask_clarification"] is True
    assert await notified_users(S) == {S["satish"]["id"]}
    d2, thread2 = await send_client_message(S, "Website development ke regarding. Login page.")   # client clarifies
    assert d2.department_name == "Tech" and d2.poc_user_id == S["rahul"]["id"]
    assert thread2["routing_status"] == "routed"


@pytest.mark.asyncio
async def test_threshold_is_configurable_per_project(dbc, clf):
    S = await seed()
    await crm.update_project(S["project_id"], {"routing_threshold": 0.99})   # stub says 0.95 -> now too low
    d, _ = await send_client_message(S, "Login API is not working.")
    assert d.poc_user_id == S["satish"]["id"] and d.fallback == "low_confidence"


# ------------------------------------------- 5/6: thread memory and topic switch
@pytest.mark.asyncio
async def test_5_context_continuation_keeps_department(dbc, clf):
    S = await seed()
    await send_client_message(S, "Login API issue hai.")
    d, thread = await send_client_message(S, "Aur wo bhi check karna.")        # no department keyword at all
    assert clf.calls[-1]["current_department"] == "Tech"                        # router passed thread memory
    assert d.department_name == "Tech" and thread["assigned_poc_id"] == S["rahul"]["id"]


@pytest.mark.asyncio
async def test_6_department_switch_keeps_history(dbc, clf):
    S = await seed()
    _, t1 = await send_client_message(S, "Login issue hai.")
    d, t2 = await send_client_message(S, "Homepage design bhi change karna hai.")
    assert t1["id"] == t2["id"]                                                 # same thread, not a new one
    assert d.department_name == "Design" and t2["assigned_poc_id"] == S["priya"]["id"]
    events = [e["event"] for e in await crm.list_routing(t2["id"])]
    assert "department_switch" in events
    msgs = await db.query("SELECT COUNT(*) c FROM group_messages WHERE thread_id=%s", (t2["id"],))
    assert msgs[0]["c"] == 2                                                    # nothing was lost


# --------------------------------------------- 7: POC unavailable -> manager
@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["account_disconnected", "no_account_linked"])
async def test_7_poc_unavailable_falls_back_to_main_manager(dbc, clf, variant):
    S = await seed(tech_connected=(variant != "account_disconnected"))
    if variant == "no_account_linked":
        await crm.unmap_account("acc_tech")
    d, thread = await send_client_message(S, "Login API is not working.")
    assert d.poc_user_id == S["satish"]["id"] and d.account_id == "acc_main"
    assert d.routing_status == "manager_fallback" and d.fallback.startswith("poc_unavailable")
    assert d.department_name == "Tech"                                          # department still recorded
    events = [e["event"] for e in await crm.list_routing(thread["id"])]
    assert "poc_unavailable" in events                                          # failure is logged, not silent
    audit_rows = await db.query("SELECT action FROM audit_logs WHERE action='routing_failure'")
    assert audit_rows
    assert await notified_users(S) == {S["satish"]["id"]}


@pytest.mark.asyncio
async def test_no_main_manager_is_logged_and_admins_notified(dbc, clf):
    S = await seed()
    await crm.update_project(S["project_id"], {"main_manager_id": None})
    await group_db.update_group(S["group_id"], assigned_poc_id=None)
    d, _ = await send_client_message(S, "Payment ke regarding baat karni hai.")
    assert d.poc_user_id is None and d.account_id == "acc_main"
    assert await db.query("SELECT 1 FROM audit_logs WHERE action='routing_failure'")
    assert await notified_users(S) == {S["admin"]["id"]}


# ------------------------------------------------------ classifier failure
@pytest.mark.asyncio
async def test_classifier_failure_keeps_current_department_else_manager(dbc, clf):
    S = await seed()
    d0, _ = await send_client_message(S, "Login API is not working.")
    clf.fail = True
    d1, _ = await send_client_message(S, "Any news?")
    assert d1.department_name == "Tech" and d1.poc_user_id == S["rahul"]["id"]
    S2 = await seed_second_project_without_thread_memory(S)
    d2, _ = await send_client_message(S2, "Hello there")
    assert d2.poc_user_id == S["satish"]["id"]
    assert await db.query("SELECT 1 FROM conversation_routing WHERE event='classification_failed'")


async def seed_second_project_without_thread_memory(S):
    gid = await group_db.create_group("acc_main", "999@g.us", "Other", "Other", S["satish"]["id"], "auto")
    cid = await crm.create_client("Other Co")
    pid = await crm.create_project(cid, "Other", main_manager_id=S["satish"]["id"], group_id=gid)
    await projects._assign_department(pid, S["tech"], S["rahul"]["id"], None, S["admin"])
    await group_db.upsert_member(gid, CLIENT1, "Client One", "client")
    return {**S, "group_id": gid, "project_id": pid}


# ------------------------------------------------------------- escalation
@pytest.mark.asyncio
async def test_pricing_escalates_to_manager_and_forces_approval(dbc, clf):
    S = await seed()
    d, thread = await send_client_message(S, "Login API fix ke liye extra charges kitna paisa lagega? price batao")
    assert d.escalation == "pricing" and d.force_approval and d.poc_user_id == S["satish"]["id"]
    assert await db.query("SELECT 1 FROM escalations WHERE reason='pricing' AND status='open'")
    assert await notified_users(S) == {S["satish"]["id"], S["rahul"]["id"]}     # manager + the relevant POC only


# ---------------------------------------------------- 9: multiple clients
@pytest.mark.asyncio
async def test_9_multiple_clients_and_internal_sender_roles(dbc, clf):
    S = await seed()
    assert await group_db.resolve_sender_role(S["group_id"], CLIENT1) == "client"
    assert await group_db.resolve_sender_role(S["group_id"], CLIENT2) == "client"
    assert await group_db.resolve_sender_role(S["group_id"], INTERNAL) == "internal"
    assert await group_db.resolve_sender_role(S["group_id"], "900000000000@s.whatsapp.net") is None
    d, _ = await send_client_message(S, "Login API is not working.", sender=CLIENT2)
    assert d.ai_context["client"] == "ABC Company" and d.ai_context["project"] == "ABC Website"


# ---------------------------------------- full webhook pipeline (11-14)
@pytest.fixture
def pipeline(monkeypatch, clf):
    sent = []

    async def fake_send(account_id, jid, text):
        sent.append((account_id, jid, text))
        return True
    monkeypatch.setattr(bridge_client, "send_message", fake_send)
    monkeypatch.setattr(main.sio, "emit", AsyncMock(return_value=None))
    monkeypatch.setattr(main, "GROUP_AI_REPLY_DELAY_SECONDS", 0)
    replier = AsyncMock(return_value="Got it. I'll have the team check it.")
    monkeypatch.setattr(main.group_ai_replier, "build_reply", replier)
    return {"sent": sent, "replier": replier}


async def webhook(S, body, sender=CLIENT1):
    group = await group_db.get_group(S["group_id"])
    await main._handle_group_message(group, {
        "accountId": "acc_main", "jid": group["whatsapp_group_id"], "isGroup": True, "fromMe": False, "body": body,
        "messageId": f"WA{os.urandom(4).hex()}", "senderJid": sender, "senderName": "Client One"})
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()
               and "_send_group_ai_reply_after_delay" in repr(t.get_coro())]
    if pending:
        await asyncio.gather(*pending)


@pytest.mark.asyncio
async def test_auto_mode_sends_once_through_the_responsible_pocs_account(dbc, pipeline):
    S = await seed(ai_mode="auto")
    await webhook(S, "Login API is not working.")
    assert pipeline["sent"] == [("acc_tech", "120363@g.us", "Got it. I'll have the team check it.")]   # exactly ONE account
    ctx = pipeline["replier"].await_args.kwargs["routing_ctx"]
    assert ctx["department"] == "Tech"


@pytest.mark.asyncio
async def test_11_approval_mode_holds_draft_until_approved(dbc, pipeline):
    S = await seed(ai_mode="approval")
    await webhook(S, "Login API is not working.")
    assert pipeline["sent"] == []                                               # nothing sent yet
    pending = await group_db.list_pending_approvals(S["group_id"])
    assert len(pending) == 1 and pending[0]["status"] == "pending"
    await group_routes.approve(pending[0]["id"], None, current=S["rahul"])
    assert [a for a, *_ in pipeline["sent"]] == ["acc_tech"]                    # then sent via the Tech POC's account


@pytest.mark.asyncio
async def test_12_manual_mode_never_auto_sends_but_still_notifies_poc(dbc, pipeline):
    S = await seed(ai_mode="manual")
    await webhook(S, "Login API is not working.")
    assert pipeline["sent"] == [] and not pipeline["replier"].await_count
    assert not await group_db.list_pending_approvals(S["group_id"])
    assert await notified_users(S) == {S["rahul"]["id"]}


@pytest.mark.asyncio
async def test_13_paused_ai_sends_nothing(dbc, pipeline):
    S = await seed(ai_mode="auto")
    thread = await group_db.get_or_create_open_thread(S["group_id"], "ABC Website")
    await group_db.set_thread_ai_paused(thread["id"], True)
    await webhook(S, "Login API is not working.")
    assert pipeline["sent"] == [] and not pipeline["replier"].await_count
    assert not await group_db.list_pending_approvals(S["group_id"])


@pytest.mark.asyncio
async def test_14_internal_message_is_not_a_client_request(dbc, pipeline, clf):
    S = await seed(ai_mode="auto")
    await webhook(S, "Login API fixed on staging.", sender=INTERNAL)
    assert pipeline["sent"] == [] and not clf.calls
    assert await db.query("SELECT COUNT(*) c FROM conversation_routing") == [{"c": 0}]
    assert await notified_users(S) == set()


@pytest.mark.asyncio
async def test_send_falls_back_to_group_account_when_poc_account_cannot_send(dbc, pipeline, monkeypatch):
    S = await seed(ai_mode="auto")
    calls = []

    async def flaky(account_id, jid, text):
        calls.append(account_id)
        return account_id == "acc_main"                    # the Tech POC's account is not in the group, say
    monkeypatch.setattr(bridge_client, "send_message", flaky)
    await webhook(S, "Login API is not working.")
    assert calls == ["acc_tech", "acc_main"]


# --------------------------------------------- 15: client-visible updates
@pytest.mark.asyncio
async def test_15_only_client_visible_updates_are_posted_to_whatsapp(dbc, monkeypatch):
    S = await seed()
    sent = []

    async def fake_send(account_id, jid, text):
        sent.append((account_id, text))
        return True
    monkeypatch.setattr(bridge_client, "send_message", fake_send)
    internal = await updates.create_update(S["project_id"], {"department_id": S["tech"], "blocked": "API credentials missing from client",
                                                              "visibility": "internal"}, current=S["rahul"])
    assert internal["sent_to_client"] is False and sent == []
    client = await updates.create_update(S["project_id"], {"department_id": S["tech"], "completed": "Login API",
                                                            "in_progress": "Dashboard API", "next_steps": "Integration testing",
                                                            "visibility": "client"}, current=S["rahul"])
    assert client["sent_to_client"] is True and len(sent) == 1 and sent[0][0] == "acc_tech"
    assert "TECH UPDATE" in sent[0][1] and "Login API" in sent[0][1] and "credentials" not in sent[0][1]
    # the manager was told about both, the author was not
    notes = await db.query("SELECT poc_id, kind FROM poc_notifications WHERE kind='project_update'")
    assert {n["poc_id"] for n in notes} == {S["satish"]["id"]} and len(notes) == 2
    # a member may post internal updates but not client-visible ones
    with pytest.raises(Exception) as e:
        await updates.create_update(S["project_id"], {"department_id": S["tech"], "completed": "x", "visibility": "client"}, current=S["amit"])
    assert getattr(e.value, "status_code", None) == 403


# ----------------------------------------------- 10: migration of existing data
@pytest.mark.asyncio
async def test_10_legacy_groups_are_migrated_without_losing_history(dbc):
    await db.execute("INSERT INTO accounts (id, status) VALUES ('legacy_acc','connected')")
    poc = await group_db.create_poc_user("oldpoc", "hash", "Old POC", "poc")
    gid = await group_db.create_group("legacy_acc", "555@g.us", "Legacy Group", "Legacy Site", poc)
    thread = await group_db.get_or_create_open_thread(gid, "Legacy Site")
    for i in range(3):
        await group_db.save_group_message(thread["id"], gid, f"L{i}", CLIENT1, "Client", "client", "in", f"old message {i}")
    assert not await db.query("SELECT 1 FROM projects WHERE group_id=%s", (gid,))
    for _ in range(2):                                                          # running twice must not duplicate
        await crm_schema.migrate_legacy_groups()
    proj = await db.query("SELECT * FROM projects WHERE group_id=%s", (gid,))
    assert len(proj) == 1 and proj[0]["main_manager_id"] == poc and proj[0]["name"] == "Legacy Site"
    assert (await db.query("SELECT COUNT(*) c FROM clients"))[0]["c"] == 1
    assert (await db.query("SELECT COUNT(*) c FROM group_messages WHERE group_id=%s", (gid,)))[0]["c"] == 3
    assert (await db.query("SELECT role FROM poc_users WHERE id=%s", (poc,)))[0]["role"] == "main_manager"
    assert (await group_db.get_group(gid))["assigned_poc_id"] == poc            # legacy link untouched
    assert await group_db.poc_can_access_group("main_manager", poc, gid)       # and the old POC keeps access


# ------------------------------------------------ project APIs, RBAC, dashboards
@pytest.mark.asyncio
async def test_project_wizard_one_poc_per_department_and_rbac(dbc):
    S = await seed()
    created = await projects.create_project({
        "name": "Second Project", "client": {"name": "New Client"}, "main_manager_id": S["satish"]["id"],
        "group_id": await group_db.create_group("acc_main", "777@g.us", "G2", "Second", None, "approval"),
        "departments": [{"department_id": S["tech"], "poc_user_id": S["rahul"]["id"], "member_ids": [S["amit"]["id"]]}],
        "activate": True}, current=S["admin"])
    assert created["status"] == "active" and created["main_manager_id"] == S["satish"]["id"]
    # one primary POC per (project, department): assigning another replaces
    await projects.assign_poc(created["id"], S["tech"], {"user_id": S["amit"]["id"]}, current=S["satish"])
    rows = await crm.list_project_departments(created["id"])
    assert [r["poc_user_id"] for r in rows] == [S["amit"]["id"]]
    # no Main Manager -> clear 400
    with pytest.raises(Exception) as e:
        await projects.create_project({"name": "X", "client_id": S["client_id"]}, current=S["admin"])
    assert e.value.status_code == 400 and "Main Manager" in e.value.detail
    # a POC cannot manage a project or see departments they do not belong to
    with pytest.raises(Exception) as e:
        await projects.update_project(S["project_id"], {"status": "on_hold"}, current=S["rahul"])
    assert e.value.status_code == 403
    await tasks.create_task(S["project_id"], {"title": "Design hero", "department_id": S["design"]}, current=S["priya"])
    await tasks.create_task(S["project_id"], {"title": "Fix login API", "department_id": S["tech"], "assigned_user_id": S["amit"]["id"]}, current=S["rahul"])
    detail = await projects.project_details(S["project_id"], current=S["rahul"])
    assert [t["title"] for t in detail["tasks"]] == ["Fix login API"]
    assert [d["name"] for d in detail["departments"]] == ["Tech"] and detail["escalations"] == []
    outsider = await _user("outsider", "department_poc")
    with pytest.raises(Exception) as e:
        await projects.project_details(S["project_id"], current=outsider)
    assert e.value.status_code == 403


@pytest.mark.asyncio
async def test_tasks_progress_and_manager_dashboard(dbc):
    S = await seed()
    t1 = await tasks.create_task(S["project_id"], {"title": "Login API", "department_id": S["tech"], "assigned_user_id": S["amit"]["id"],
                                                    "due_date": "2000-01-01"}, current=S["rahul"])
    await tasks.create_task(S["project_id"], {"title": "Dashboard API", "department_id": S["tech"]}, current=S["rahul"])
    await tasks.update_task(t1["id"], {"status": "completed"}, current=S["amit"])           # assignee may update status
    await tasks.update_task(t1["id"], {"title": "renamed"}, current=S["amit"])   # assignee: only status/description apply
    d = await dashboard_routes.manager_dashboard(current=S["satish"])
    tech = next(x for x in d["department_progress"] if x["name"] == "Tech")
    assert tech["progress_pct"] == 50 and d["pending_tasks"] == 1 and d["overdue_tasks"] == 0
    assert d["total_projects"] == 1 and d["active_projects"] == 1
    assert any(p["user_id"] == S["rahul"]["id"] and p["available"] for p in d["poc_availability"])
    with pytest.raises(Exception) as e:
        await dashboard_routes.manager_dashboard(current=S["rahul"])
    assert e.value.status_code == 403
    dd = await dashboard_routes.department_dashboard(current=S["rahul"])
    assert {x["name"] for x in dd["departments"]} == {"Tech"}
    assert (await crm.get_task(t1["id"]))["title"] == "Login API"
    note = await db.query("SELECT message FROM poc_notifications WHERE kind='task_update'")
    assert note and "completed" in note[0]["message"]


@pytest.mark.asyncio
async def test_manual_reassignment_and_routing_history(dbc, clf):
    S = await seed()
    _, thread = await send_client_message(S, "Login API is not working.")
    await conversations.reassign(thread["id"], {"department_id": S["design"]}, current=S["satish"])
    t = await group_db.get_thread(thread["id"])
    assert t["current_department_id"] == S["design"] and t["assigned_poc_id"] == S["priya"]["id"]
    hist = await conversations.routing_history(thread["id"], current=S["satish"])
    events = [e["event"] for e in hist["events"]]
    assert events[0] == "message_received" and "manual_reassign" in events
    with pytest.raises(Exception) as e:                                         # POCs cannot reassign
        await conversations.reassign(thread["id"], {"department_id": S["tech"]}, current=S["rahul"])
    assert e.value.status_code == 403


# ------------------------------------------------ classifier output parsing
def test_classifier_parse_is_tolerant_and_clamps():
    p = department_classifier._parse
    ok = p('Sure: {"department": "Tech", "confidence": 0.94, "reason": "login bug", "needs_manager": false}')
    assert ok["department"] == "Tech" and ok["confidence"] == 0.94 and ok["needs_manager"] is False
    assert p('{"department": "null", "confidence": 7}')["department"] is None
    assert p('{"department": "Tech", "confidence": 7}')["confidence"] == 1.0
    assert p('{"department": "Tech", "confidence": "abc"}')["confidence"] == 0.0
    with pytest.raises(ValueError):
        p("no json here")


# =====================================================================
# Role-based flow (v11): admin -> Main Manager -> POC -> member
# =====================================================================
from server import auth as auth_mod, group_routes
from server.routes import clients as clients_routes, departments as dept_routes, my_whatsapp, tasks as tasks_routes
from server.services import whatsapp_link


async def expect_status(coro, code):
    with pytest.raises(Exception) as e:
        await coro
    assert getattr(e.value, "status_code", None) == code, f"expected {code}, got {e.value!r}"


@pytest.mark.asyncio
async def test_only_admin_creates_clients_and_projects_manager_sees_them(dbc):
    S = await seed()
    await expect_status(clients_routes.create_client({"name": "X"}, current=S["satish"]), 403)
    await expect_status(projects.create_project({"name": "P", "client": {"name": "C"}}, current=S["satish"]), 403)
    client = await clients_routes.create_client({"name": "New Co", "main_manager_id": S["satish"]["id"]}, current=S["admin"])
    assert client["main_manager_id"] == S["satish"]["id"]
    # manager is taken from the client when the project does not name one
    proj = await projects.create_project({"name": "Portal", "client_id": client["id"]}, current=S["admin"])
    assert proj["main_manager_id"] == S["satish"]["id"]
    mine = await clients_routes.list_clients(current=S["satish"])
    assert "New Co" in [c["name"] for c in mine]
    other = await _user("other_mgr", "main_manager")
    assert "New Co" not in [c["name"] for c in await clients_routes.list_clients(current=other)]
    await expect_status(clients_routes.get_client(client["id"], current=other), 403)


@pytest.mark.asyncio
async def test_departments_belong_to_the_manager_who_created_them(dbc):
    S = await seed()
    mgr_b = await _user("mgr_b", "main_manager")
    a = await dept_routes.create_department({"name": "Sales", "description": "pricing"}, current=S["satish"])
    b = await dept_routes.create_department({"name": "Sales", "description": "mine"}, current=mgr_b)   # same name, other manager: fine
    assert a["id"] != b["id"] and a["owner_user_id"] == S["satish"]["id"]
    await expect_status(dept_routes.create_department({"name": "sales"}, current=S["satish"]), 409)
    names_b = [d["name"] for d in await dept_routes.list_departments(current=mgr_b)]
    assert names_b.count("Sales") == 1 and "Tech" in names_b                                         # own + shared, not Satish's Sales
    await expect_status(dept_routes.create_department({"name": "X"}, current=S["rahul"]), 403)        # POC cannot
    await expect_status(dept_routes.update_department(a["id"], {"name": "Hacked"}, current=mgr_b), 403)
    await expect_status(projects.add_department(S["project_id"], {"department_id": b["id"]}, current=S["satish"]), 400)  # not Satish's
    assert [d["name"] for d in await dept_routes.list_departments(current=S["rahul"])] == ["Tech"]    # POC: only own department


@pytest.mark.asyncio
async def test_main_manager_builds_team_and_creates_logins(dbc, monkeypatch):
    S = await seed()
    sales = await dept_routes.create_department({"name": "Sales"}, current=S["satish"])
    ex = await dept_routes.create_department({"name": "Execution"}, current=S["satish"])
    for jid, name in (("919800000001@s.whatsapp.net", "Sam Seller"), ("919800000002@s.whatsapp.net", "Eva Exec"), ("919800000003@s.whatsapp.net", "Mo Member")):
        await group_db.upsert_member(S["group_id"], jid, name, "internal", jid.split("@")[0])
    out = await projects.add_team(S["project_id"], {"people": [
        {"jid": "919800000001@s.whatsapp.net", "name": "Sam Seller", "phone": "919800000001", "department_id": sales["id"], "is_poc": True},
        {"jid": "919800000002@s.whatsapp.net", "name": "Eva Exec", "phone": "919800000002", "department_id": ex["id"], "is_poc": True, "username": "eva", "password": "evapass1234"},
        {"jid": "919800000003@s.whatsapp.net", "name": "Mo Member", "phone": "919800000003", "department_id": sales["id"], "is_poc": False}]},
        current=S["satish"])
    r = {x["name"]: x for x in out["results"]}
    assert all(x["created"] for x in r.values()) and r["Eva Exec"]["username"] == "eva" and r["Eva Exec"]["password"] == "evapass1234"
    assert r["Sam Seller"]["role"] == "POC" and r["Mo Member"]["role"] == "Member" and len(r["Sam Seller"]["password"]) >= 8
    row = await db.query("SELECT * FROM poc_users WHERE username='eva'")
    assert row[0]["role"] == "department_poc" and row[0]["created_by"] == S["satish"]["id"] and auth_mod.verify_password("evapass1234", row[0]["password_hash"])
    pocs = {d["name"]: d["poc_name"] for d in await crm.list_project_departments(S["project_id"])}
    assert pocs["Sales"] == "Sam Seller" and pocs["Execution"] == "Eva Exec"
    mem = await db.query("SELECT role FROM group_members WHERE group_id=%s AND member_jid='919800000003@s.whatsapp.net'", (S["group_id"],))
    assert mem[0]["role"] == "internal"
    rost = {x["name"]: x for x in await projects.roster(S["project_id"], current=S["satish"])}
    assert rost["Mo Member"]["department"] == "Sales" and rost["Mo Member"]["poc_name"] == "Sam Seller" and not rost["Mo Member"]["is_poc"]
    assert rost["Client One"]["user_id"] is None
    # same number again: login reused, nothing overwritten, no password re-issued
    again = await projects.add_team(S["project_id"], {"people": [{"jid": "919800000003@s.whatsapp.net", "name": "Mo Member", "phone": "+91 98000 00003",
                                                                  "department_id": ex["id"], "is_poc": False}]}, current=S["satish"])
    assert again["results"][0]["created"] is False and again["results"][0]["password"] is None
    # guards
    await expect_status(projects.add_team(S["project_id"], {"people": [
        {"name": "A", "phone": "919811111111", "department_id": sales["id"], "is_poc": True},
        {"name": "B", "phone": "919822222222", "department_id": sales["id"], "is_poc": True}]}, current=S["satish"]), 400)   # one POC per department
    await expect_status(projects.add_team(S["project_id"], {"people": [{"name": "Z", "phone": "12", "department_id": sales["id"]}]}, current=S["satish"]), 400)
    await expect_status(projects.add_team(S["project_id"], {"people": [{"name": "Z", "phone": "919833333333", "department_id": sales["id"]}]}, current=S["rahul"]), 403)
    # a manager can reset a password of a login they created, but not someone else's
    sam = await crm.find_user_by_username(r["Sam Seller"]["username"])
    await users.update_user(sam["id"], {"password": "newpassword1", "role": "admin"}, current=S["satish"])      # role change silently ignored
    assert (await crm.get_user(sam["id"]))["role"] == "department_poc"
    await expect_status(users.update_user(S["rahul"]["id"], {"password": "newpassword1"}, current=S["satish"]), 403)
    assert {u["username"] for u in await users.list_users(current=S["satish"])} >= {"eva", "rahul"}
    mgr_b = await _user("mgr_b2", "main_manager")
    assert "eva" not in {u["username"] for u in await users.list_users(current=mgr_b)}


@pytest.mark.asyncio
async def test_reporting_chain_and_member_only_sees_own_tasks(dbc):
    S = await seed()
    t_mine = await tasks.create_task(S["project_id"], {"title": "Fix login API", "department_id": S["tech"], "assigned_user_id": S["amit"]["id"]}, current=S["rahul"])
    await tasks.create_task(S["project_id"], {"title": "Other task", "department_id": S["tech"], "assigned_user_id": S["rahul"]["id"]}, current=S["rahul"])
    await tasks.create_task(S["project_id"], {"title": "Design hero", "department_id": S["design"], "assigned_user_id": S["neha"]["id"]}, current=S["priya"])
    await expect_status(tasks.create_task(S["project_id"], {"title": "x", "department_id": S["tech"], "assigned_user_id": S["neha"]["id"]}, current=S["rahul"]), 400)  # not in Tech
    await expect_status(tasks.create_task(S["project_id"], {"title": "x", "department_id": S["tech"]}, current=S["amit"]), 403)                                    # members cannot create
    assert [t["title"] for t in await tasks.list_tasks(current=S["amit"])] == ["Fix login API"]
    d = await projects.project_details(S["project_id"], current=S["amit"])
    assert [t["title"] for t in d["tasks"]] == ["Fix login API"] and d["conversations"] == [] and d["group_members"] == [] and d["activity_log"] == []
    assert all(x["members"] == [] for x in d["departments"])
    await expect_status(projects.attach_group(S["project_id"], {}, current=S["amit"]), 403)
    await expect_status(conversations.project_conversations(S["project_id"], current=S["amit"]) if False else
                        conversations.routing_history(1, current=S["amit"]), 404)          # no thread yet: cannot probe
    # member changes status -> POC (not the manager) is told
    await tasks.update_task(t_mine["id"], {"status": "in_progress"}, current=S["amit"])
    notes = await db.query("SELECT poc_id, kind FROM poc_notifications WHERE kind='task_update'")
    assert [n["poc_id"] for n in notes] == [S["rahul"]["id"]]
    # member adds a note on the task -> POC sees it; it is internal and is never sent to WhatsApp
    sent = []

    async def fake_send(a, j, t):
        sent.append(t); return True
    import server.bridge_client as bc
    orig = bc.send_message; bc.send_message = fake_send
    try:
        r = await updates.create_update(S["project_id"], {"task_id": t_mine["id"], "in_progress": "API half done", "visibility": "internal"}, current=S["amit"])
        assert r["sent_to_client"] is False and sent == []
        await expect_status(updates.create_update(S["project_id"], {"task_id": t_mine["id"], "completed": "x", "visibility": "client"}, current=S["amit"]), 403)
        await expect_status(updates.create_update(S["project_id"], {"task_id": (await db.query("SELECT id FROM tasks WHERE title='Design hero'"))[0]["id"], "summary": "x"}, current=S["amit"]), 403)
        notes = await db.query("SELECT poc_id FROM poc_notifications WHERE kind='project_update'")
        assert [n["poc_id"] for n in notes] == [S["rahul"]["id"]]
        # POC reports to the manager; a client-visible POC update is the only thing that reaches the group, via the POC's account
        await updates.create_update(S["project_id"], {"department_id": S["tech"], "completed": "Login API", "visibility": "client"}, current=S["rahul"])
        assert len(sent) == 1 and "TECH UPDATE" in sent[0]
    finally:
        bc.send_message = orig
    assert S["satish"]["id"] in {n["poc_id"] for n in await db.query("SELECT poc_id FROM poc_notifications WHERE kind='project_update'")}
    assert [u["author_name"] for u in await updates.list_updates(project_id=S["project_id"], current=S["amit"])] == ["Amit"]
    assert (await updates.list_updates(project_id=S["project_id"], current=S["amit"]))[0]["task_title"] == "Fix login API"
    dd = await dashboard_routes.department_dashboard(current=S["amit"])
    assert [t["title"] for t in dd["tasks"]] == ["Fix login API"]


@pytest.mark.asyncio
async def test_my_whatsapp_linking_maps_account_to_the_user(dbc, monkeypatch):
    S = await seed()
    started = []

    async def start(account_id): started.append(account_id)
    async def remove(account_id): return None
    monkeypatch.setattr(whatsapp_link.bridge_client, "start_session", start)
    monkeypatch.setattr(whatsapp_link.bridge_client, "remove_session", remove)
    await expect_status(my_whatsapp.connect(current=S["amit"]), 403)                    # members never link WhatsApp
    poc = await _user("newpoc", "department_poc")
    assert (await my_whatsapp.my_whatsapp(current=poc)) == {"account": None, "pending": None}
    aid = (await my_whatsapp.connect(current=poc))["account_id"]
    assert started == [aid]
    whatsapp_link.on_qr(aid, "data:image/png;base64,QR")
    assert (await my_whatsapp.my_whatsapp(current=poc))["pending"] == {"account_id": aid, "qr": "data:image/png;base64,QR"}
    await db.upsert_account_connected(aid, "919999999999", "New POC")                    # what /webhook/status does
    await whatsapp_link.on_connected(aid)
    st = await my_whatsapp.my_whatsapp(current=poc)
    assert st["pending"] is None and st["account"]["account_id"] == aid and st["account"]["status"] == "connected"
    await expect_status(my_whatsapp.connect(current=poc), 409)
    await whatsapp_link.on_connected("some-other-account")                                  # unrelated accounts are ignored
    assert (await my_whatsapp.disconnect(current=poc))["removed"] is True
    assert (await my_whatsapp.my_whatsapp(current=poc))["account"] is None


@pytest.mark.asyncio
async def test_manager_can_only_browse_groups_of_own_account(dbc, monkeypatch):
    S = await seed()
    monkeypatch.setattr(group_routes.bridge_client, "list_groups", AsyncMock(return_value=[{"jid": "1@g.us", "name": "G"}]))
    assert await group_routes.discover_groups("acc_main", current=S["satish"])           # satish owns acc_main
    await expect_status(group_routes.discover_groups("acc_tech", current=S["satish"]), 403)
    assert await group_routes.discover_groups("acc_tech", current=S["admin"])


@pytest.mark.asyncio
async def test_manager_attaches_group_through_own_account(dbc, monkeypatch):
    S = await seed()
    cid = await crm.create_client("Fresh Co", main_manager_id=S["satish"]["id"])
    pid = await crm.create_project(cid, "Fresh Project", main_manager_id=S["satish"]["id"])
    monkeypatch.setattr(group_routes.bridge_client, "sync_group_history", AsyncMock(return_value={}), raising=False)
    proj = await projects.attach_group(pid, {"whatsapp_group_id": "555@g.us", "group_name": "Fresh Group",
                                            "members": [{"jid": CLIENT1, "name": "Client One", "role": "client"}], "ai_mode": "approval"},
                                       current=S["satish"])
    assert proj["group_id"] and proj["ai_mode"] == "approval" and proj["group_account_id"] == "acc_main"   # the manager's own account
    await expect_status(projects.attach_group(pid, {"whatsapp_group_id": "556@g.us"}, current=S["satish"]), 409)
    nowa = await _user("nowa", "main_manager")
    cid2 = await crm.create_client("No WA Co", main_manager_id=nowa["id"])
    pid2 = await crm.create_project(cid2, "P2", main_manager_id=nowa["id"])
    await expect_status(projects.attach_group(pid2, {"whatsapp_group_id": "557@g.us"}, current=nowa), 400)  # must link WhatsApp first


@pytest.mark.asyncio
async def test_members_without_a_whatsapp_name_use_their_number(dbc):
    """WhatsApp often gives no display name. Adding such a person must not fail with 'name is required'."""
    S = await seed()
    await group_db.upsert_member(S["group_id"], "919877700001@s.whatsapp.net", None, "internal", "919877700001")
    roster = {m["phone"]: m for m in await projects.roster(S["project_id"], current=S["satish"])}
    assert roster["919877700001"]["name"] == "919877700001"                      # shown by number, not blank
    out = await projects.add_team(S["project_id"], {"people": [
        {"jid": "919877700001@s.whatsapp.net", "name": "", "phone": "919877700001", "department_id": S["tech"], "is_poc": False}]},
        current=S["satish"])
    assert out["results"][0]["name"] == "919877700001" and out["results"][0]["created"] is True
    assert (await crm.find_user_by_phone("919877700001"))["name"] == "919877700001"


# =====================================================================
# v12: one WhatsApp group, many projects/departments; RBAC; soft delete; audit; notifications
# =====================================================================
import json as _json

from server import permissions
from server.routes import admin_data, notifications as notif_routes
from server.services import notification_service as ns, audit as audit_svc


async def second_project(S, name="Product catalogue", with_tech_poc=None):
    """A second project of the SAME client, mapped to the SAME WhatsApp group (the case that used to fail)."""
    pid = await crm.create_project(S["client_id"], name, status="active", main_manager_id=S["satish"]["id"])
    return pid


@pytest.mark.asyncio
async def test_same_group_can_serve_two_projects_of_one_client(dbc, monkeypatch):
    """REPRODUCES THE REPORTED BUG: 'This WhatsApp group is already configured' when a second project used the same group."""
    S = await seed()
    p2 = await second_project(S)
    # before v12: projects.group_id was UNIQUE and attach/create refused a group that was already configured
    out = await projects.attach_group(p2, {"whatsapp_group_id": "120363@g.us", "group_name": "ABC Website Development"}, current=S["satish"])
    assert out["group_id"] == S["group_id"] and out["group_reused"] is True
    assert len(await crm.list_projects_by_group(S["group_id"])) == 2
    assert (await db.query("SELECT COUNT(*) c FROM client_groups"))[0]["c"] == 1          # still ONE config row per real group
    # the (account, group) uniqueness is still enforced where it belongs
    with pytest.raises(Exception):
        await db.execute("INSERT INTO client_groups (account_id, whatsapp_group_id, group_name) VALUES ('acc_main','120363@g.us','dup')")
    # but a group cannot be shared by two DIFFERENT clients (conversation leakage)
    other_client = await crm.create_client("Other Client", main_manager_id=S["satish"]["id"])
    p3 = await crm.create_project(other_client, "Other", main_manager_id=S["satish"]["id"])
    await expect_status(projects.attach_group(p3, {"whatsapp_group_id": "120363@g.us"}, current=S["satish"]), 409)
    # the mapping view shows both projects on the group
    maps = await projects.group_mappings(current=S["admin"])
    assert [p["project"] for p in maps[0]["projects"]] == ["ABC Website", "Product catalogue"]
    # unlink: group, members and history stay
    await projects.unlink_group(p2, current=S["satish"])
    assert (await crm.get_project(p2))["group_id"] is None and await group_db.get_group(S["group_id"])


@pytest.mark.asyncio
async def test_two_projects_one_group_route_to_the_right_project_and_poc(dbc, clf, monkeypatch):
    S = await seed()
    p2 = await second_project(S)
    await projects.attach_group(p2, {"whatsapp_group_id": "120363@g.us"}, current=S["satish"])
    tech2 = await dept_routes.create_department({"name": "Tech2"}, current=S["satish"])      # same kind of department, other project
    boss2 = await _user("boss2", "department_poc")
    await crm.map_account_user("acc_design", boss2["id"]) if False else None
    await projects._assign_department(p2, S["tech"], S["priya"]["id"], None, S["admin"])     # Priya is the Tech POC of project 2
    seen = {}

    class Multi(StubClassifier):
        async def __call__(self, message, departments, history_text, current_department=None, routing_status=None):
            seen["labels"] = [d["name"] for d in departments]
            seen["current"] = current_department
            return _r("Product catalogue / Tech", 0.93) if "catalogue" in message.lower() else _r("ABC Website / Tech", 0.95)
    monkeypatch.setattr(department_classifier, "classify", Multi())
    d1, t1 = await send_client_message(S, "Login API is not working on the website")
    assert d1.project_id == S["project_id"] and d1.poc_user_id == S["rahul"]["id"]
    d2, t2 = await send_client_message(S, "catalogue page crashes")
    assert d2.project_id == p2 and d2.poc_user_id == S["priya"]["id"] and t2["current_project_id"] == p2
    assert "ABC Website / Tech" in seen["labels"] and "Product catalogue / Tech" in seen["labels"]
    assert seen["current"] == "ABC Website / Tech"                                   # thread memory names project AND department
    assert t1["id"] == t2["id"]                                                      # same conversation, history kept
    cl = await db.query("SELECT project_id, department_id, urgency, category FROM message_classifications ORDER BY id")
    assert [c["project_id"] for c in cl] == [S["project_id"], p2]
    ass = await crm.list_assignments(t2["id"])
    assert [(a["project_name"], a["poc_name"], a["kind"]) for a in ass] == [("ABC Website", "Rahul", "primary"), ("Product catalogue", "Priya", "primary")]
    # the AI context carries the right project, and conversations are filtered per project
    assert d2.ai_context["project"] == "Product catalogue"


@pytest.mark.asyncio
async def test_ai_cannot_invent_a_department_and_clarification_goes_to_manager(dbc, monkeypatch):
    S = await seed()
    answers = [dict(_r("Legal", 0.99)), dict(_r("Tech", 0.9), needs_clarification=True), dict(_r("Tech", 0.9), urgency="urgent", intent="outage")]

    async def fake(message, departments, history_text, current_department=None, routing_status=None):
        return answers.pop(0)
    monkeypatch.setattr(department_classifier, "classify", fake)
    d, _ = await send_client_message(S, "Something about Legal?")
    assert d.department_id is None and d.poc_user_id == S["satish"]["id"]             # 'Legal' is not a department of this project
    d, _ = await send_client_message(S, "ambiguous")
    assert d.poc_user_id == S["satish"]["id"] and d.routing_status == "needs_clarification"      # needs_clarification wins over confidence
    d, _ = await send_client_message(S, "site is down!")
    assert d.poc_user_id == S["rahul"]["id"]
    title = (await db.query("SELECT title FROM poc_notifications WHERE kind='client_message' ORDER BY id DESC LIMIT 1"))[0]["title"]
    assert title.startswith("URGENT") and "Tech" in title
    cl = (await db.query("SELECT urgency, intent FROM message_classifications ORDER BY id DESC LIMIT 1"))[0]
    assert (cl["urgency"], cl["intent"]) == ("urgent", "outage")


@pytest.mark.asyncio
async def test_cross_department_message_has_primary_and_secondary_assignment(dbc, monkeypatch):
    S = await seed()

    async def fake(message, departments, history_text, current_department=None, routing_status=None):
        return dict(_r("Tech", 0.92, cross=True), also_departments=["Design", "Nonexistent"])
    monkeypatch.setattr(department_classifier, "classify", fake)
    d, thread = await send_client_message(S, "The dashboard is broken and the layout looks wrong too")
    assert d.poc_user_id == S["rahul"]["id"] and d.force_approval is True               # one primary responder, human approval
    ass = [(a["department_name"], a["kind"]) for a in await crm.list_assignments(thread["id"])]
    assert ass == [("Tech", "primary"), ("Design", "secondary")]                         # invented department ignored
    assert await notified_users(S) == {S["rahul"]["id"], S["priya"]["id"]}               # Akash (SEO) is not involved


@pytest.mark.asyncio
async def test_duplicate_webhook_is_idempotent(dbc, pipeline):
    S = await seed(ai_mode="auto")
    group = await group_db.get_group(S["group_id"])
    payload = {"accountId": "acc_main", "jid": group["whatsapp_group_id"], "isGroup": True, "fromMe": False, "body": "Login API is not working.",
               "messageId": "SAME-ID-1", "senderJid": CLIENT1, "senderName": "Client One"}
    for _ in range(3):                                                                    # provider retries the same event
        await main._handle_group_message(group, dict(payload))
        for t in [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and "_send_group_ai_reply_after_delay" in repr(t.get_coro())]:
            await t
    assert (await db.query("SELECT COUNT(*) c FROM group_messages WHERE direction='in'"))[0]["c"] == 1
    assert len(pipeline["sent"]) == 1                                                     # one reply, not three
    assert (await db.query("SELECT COUNT(*) c FROM poc_notifications WHERE kind='client_message'"))[0]["c"] == 1


@pytest.mark.asyncio
async def test_failed_whatsapp_delivery_is_recorded_and_notified(dbc, pipeline, monkeypatch):
    S = await seed(ai_mode="auto")

    async def failing(account_id, jid, text):
        return False
    monkeypatch.setattr(bridge_client, "send_message", failing)
    await webhook(S, "Login API is not working.")
    out = await db.query("SELECT delivery_status FROM group_messages WHERE direction='out'")
    assert out[0]["delivery_status"] == "failed"
    n = await db.query("SELECT poc_id, title FROM poc_notifications WHERE kind='send_failed'")
    assert {x["poc_id"] for x in n} == {S["rahul"]["id"], S["satish"]["id"]} and n[0]["title"] == "Message NOT delivered"


@pytest.mark.asyncio
async def test_own_outgoing_message_never_triggers_a_reply(dbc, pipeline):
    """A message the system/POC sent into the group comes back through the webhook: it must not start a reply loop."""
    S = await seed(ai_mode="auto")
    group = await group_db.get_group(S["group_id"])
    await main._handle_group_message(group, {"accountId": "acc_main", "jid": group["whatsapp_group_id"], "isGroup": True, "fromMe": True,
                                             "body": "Login API fix is in progress", "messageId": "OUT-1", "senderJid": "acc", "senderName": "Me"})
    await main._handle_group_message(group, {"accountId": "acc_main", "jid": group["whatsapp_group_id"], "isGroup": True, "fromMe": False,
                                             "body": "Login API fix is in progress", "messageId": "OUT-2", "senderJid": INTERNAL, "senderName": "Internal Dev"})
    assert pipeline["sent"] == [] and not pipeline["replier"].await_count


# ------------------------------------------------------------- RBAC matrix
@pytest.mark.asyncio
async def test_permission_matrix_separates_add_edit_delete(dbc):
    S = await seed()
    assert permissions.allowed(S["admin"], "client.delete") and not permissions.allowed(S["satish"], "client.delete")
    assert permissions.allowed(S["satish"], "department.create") and permissions.allowed(S["satish"], "department.edit") and not permissions.allowed(S["satish"], "department.delete")
    assert permissions.allowed(S["rahul"], "task.create") and permissions.allowed(S["rahul"], "task.edit") and not permissions.allowed(S["rahul"], "task.delete")
    assert permissions.allowed(S["amit"], "task.edit") and not permissions.allowed(S["amit"], "task.create") and not permissions.allowed(S["amit"], "update.send_to_client")
    assert not permissions.allowed(S["amit"], "whatsapp.link_own") and permissions.allowed(S["rahul"], "whatsapp.link_own")
    mine = await admin_data.my_permissions(current=S["rahul"])
    assert "task.create" in mine["actions"] and "client.create" not in mine["actions"]
    await expect_status(admin_data.matrix(current=S["satish"]), 403)
    assert "client.create" in (await admin_data.matrix(current=S["admin"]))


@pytest.mark.asyncio
async def test_delete_permissions_are_enforced_by_the_backend(dbc):
    S = await seed()
    await expect_status(clients_routes.delete_client(S["client_id"], current=S["satish"]), 403)
    await expect_status(projects.delete_project(S["project_id"], current=S["satish"]), 403)
    await expect_status(dept_routes.delete_department(S["tech"], current=S["satish"]), 403)
    await expect_status(users.delete_user(S["amit"]["id"], current=S["satish"]), 403)
    t = await tasks.create_task(S["project_id"], {"title": "t", "department_id": S["tech"]}, current=S["rahul"])
    await expect_status(tasks_routes.delete_task(t["id"], current=S["rahul"]), 403)       # POC created it but may not delete it
    await expect_status(tasks_routes.delete_task(t["id"], current=S["amit"]), 403)
    await tasks_routes.delete_task(t["id"], current=S["satish"])                          # the project's Main Manager may
    assert await crm.get_task(t["id"]) is None and await crm.get_task(t["id"], include_deleted=True)


@pytest.mark.asyncio
async def test_soft_delete_keeps_history_and_can_be_restored(dbc, clf):
    S = await seed()
    d, thread = await send_client_message(S, "Login API is not working.")
    await db.execute("INSERT INTO accounts (id, status) VALUES ('acc_x','connected')") if False else None
    res = await clients_routes.delete_client(S["client_id"], current=S["admin"])
    assert res["projects_hidden"] == 1
    assert S["client_id"] not in [c["id"] for c in await clients_routes.list_clients(current=S["admin"])]
    assert not await crm.list_projects(S["admin"]) and await crm.get_project(S["project_id"]) is None
    assert (await db.query("SELECT COUNT(*) c FROM group_messages"))[0]["c"] == 1                 # history is untouched
    assert (await db.query("SELECT COUNT(*) c FROM audit_logs WHERE action='client_deleted'"))[0]["c"] == 1
    assert [x["name"] for x in await admin_data.deleted("client", current=S["admin"])] == ["ABC Company"]
    await expect_status(clients_routes.restore_client(S["client_id"], {}, current=S["satish"]), 403)
    await clients_routes.restore_client(S["client_id"], {"with_projects": True}, current=S["admin"])
    assert (await crm.get_project(S["project_id"]))["name"] == "ABC Website"
    # deleted users cannot log in and vanish from lists, but their name stays on history
    await users.delete_user(S["amit"]["id"], current=S["admin"])
    assert (await db.query("SELECT active FROM poc_users WHERE id=%s", (S["amit"]["id"],)))[0]["active"] == 0
    assert S["amit"]["id"] not in [u["id"] for u in await crm.list_users()]
    await expect_status(users.delete_user(S["admin"]["id"], current=S["admin"]), 400)                # cannot delete yourself / last admin
    await users.restore_user(S["amit"]["id"], current=S["admin"])
    assert S["amit"]["id"] in [u["id"] for u in await crm.list_users()]


# --------------------------------------------------- audit trail with before/after
@pytest.mark.asyncio
async def test_audit_records_who_what_before_after_and_hides_secrets(dbc):
    S = await seed()
    await clients_routes.update_client(S["client_id"], {"name": "ABC Renamed", "phone": "123"}, current=S["admin"])
    row = (await db.query("SELECT * FROM audit_logs WHERE action='client_updated'"))[0]
    d = _json.loads(row["detail"])
    assert row["user_id"] == S["admin"]["id"] and d["before"]["name"] == "ABC Company" and d["after"]["name"] == "ABC Renamed"
    await users.update_user(S["amit"]["id"], {"password": "SuperSecret123", "role": "department_poc"}, current=S["admin"])
    raw = " ".join(r["detail"] or "" for r in await db.query("SELECT detail FROM audit_logs"))
    assert "SuperSecret123" not in raw and "password_hash" not in raw.replace('"password"', "")          # no secret is ever stored
    ch = (await db.query("SELECT detail FROM audit_logs WHERE action='role_changed'"))
    assert ch and _json.loads(ch[0]["detail"])["after"]["role"] == "department_poc"
    # who changed the POC
    await projects.assign_poc(S["project_id"], S["tech"], {"user_id": S["priya"]["id"]}, current=S["satish"])
    pc = (await db.query("SELECT user_id, project_id, department_id, detail FROM audit_logs WHERE action='poc_changed'"))[0]
    assert pc["user_id"] == S["satish"]["id"] and pc["project_id"] == S["project_id"] and pc["department_id"] == S["tech"]
    assert _json.loads(pc["detail"])["before"]["poc_user_id"] == S["rahul"]["id"]
    assert audit_svc.redact({"api_key": "k", "token": "t", "x": 1}) == {"api_key": "***", "token": "***", "x": 1}
    gm = await projects.attach_group(await crm.create_project(S["client_id"], "P9", main_manager_id=S["satish"]["id"]),
                                     {"whatsapp_group_id": "120363@g.us"}, current=S["satish"])
    assert await db.query("SELECT 1 FROM audit_logs WHERE action='group_mapped' AND user_id=%s", (S["satish"]["id"],))


# ------------------------------------------------------------- notifications
@pytest.mark.asyncio
async def test_notifications_events_read_state_and_dedupe(dbc):
    S = await seed()
    c = await clients_routes.create_client({"name": "Fresh", "main_manager_id": S["satish"]["id"]}, current=S["admin"])
    assert (await notif_routes.unread_count(current=S["satish"]))["unread"] >= 1
    mine = await notif_routes.list_notifications(current=S["satish"])
    n = next(x for x in mine if x["kind"] == "client_created")
    assert n["title"] == "New client: Fresh" and n["entity_type"] == "client" and n["entity_id"] == str(c["id"]) and n["status"] == "unread"
    assert not [x for x in await notif_routes.list_notifications(current=S["admin"]) if x["kind"] == "client_created"]     # the actor is not notified of their own action
    assert not [x for x in await notif_routes.list_notifications(current=S["rahul"]) if x["kind"] == "client_created"]     # unrelated POC never sees it
    await expect_status(notif_routes.mark_read(n["id"], current=S["rahul"]), 403)
    await notif_routes.mark_read(n["id"], current=S["satish"])
    assert [x for x in await notif_routes.list_notifications(current=S["satish"]) if x["id"] == n["id"]][0]["status"] == "read"
    await notif_routes.read_all(current=S["satish"])
    assert (await notif_routes.unread_count(current=S["satish"]))["unread"] == 0
    assert len(await notif_routes.list_notifications(current=S["satish"], unread_only=True)) == 0 and len(await notif_routes.list_notifications(current=S["satish"])) >= 1  # history kept
    # dedupe: the same keyed notification is stored once
    a = await ns.notify(S["rahul"]["id"], "client_message", "x", title="t", dedupe_key="msg99:client_message")
    b = await ns.notify(S["rahul"]["id"], "client_message", "x", title="t", dedupe_key="msg99:client_message")
    assert a and b is None


# ----------------------------------------------- migration from the v11 schema
@pytest.mark.asyncio
async def test_migration_from_v11_keeps_data_and_is_repeatable(dbc):
    S = await seed()
    await db.execute("ALTER TABLE projects ADD UNIQUE KEY `group_id` (`group_id`)")              # put the v11 shape back: one group : one project
    with pytest.raises(Exception):
        await db.execute("INSERT INTO projects (client_id, name, group_id) VALUES (%s,'dup',%s)", (S["client_id"], S["group_id"]))
    for _ in range(2):
        await crm_schema.init_crm_tables()
    await db.execute("INSERT INTO projects (client_id, name, group_id) VALUES (%s,'second',%s)", (S["client_id"], S["group_id"]))     # now allowed
    assert (await db.query("SELECT COUNT(*) c FROM projects WHERE group_id=%s", (S["group_id"],)))[0]["c"] == 2
    assert [d["name"] for d in await crm.list_project_departments(S["project_id"])] == ["Design", "SEO", "Tech"]      # assignments intact
    # one department can serve several groups (projects), each project keeps its own POC
    assert not await db.query("SELECT 1 FROM information_schema.statistics WHERE table_schema=DATABASE() AND table_name='project_departments' AND non_unique=0 AND index_name<>'PRIMARY'")


# ===================================================== v13: verified updates + AI context
from types import SimpleNamespace

from server import group_ai_replier
from server.routes import verified_updates as vu_routes


async def _sent_capture(monkeypatch):
    sent = []

    async def fake_send(account_id, jid, text):
        sent.append((account_id, text))
        return True
    monkeypatch.setattr(bridge_client, "send_message", fake_send)
    return sent


@pytest.mark.asyncio
async def test_verified_update_is_listed_with_author_for_everyone_on_the_project(dbc):
    S = await seed()
    r = await vu_routes.add_project_verified(S["project_id"], {"text": "Login API deployed", "visibility": "internal"}, current=S["satish"])
    assert r["sent_to_client"] is False and r["items"][0]["author_name"] == "Satish"
    for who in ("rahul", "amit", "admin"):   # POC, member and admin all see it, with the author
        v = await vu_routes.list_project_verified(S["project_id"], current=S[who])
        assert [i["text"] for i in v["items"]] == ["Login API deployed"] and v["items"][0]["author_name"] == "Satish"
    assert (await vu_routes.list_project_verified(S["project_id"], current=S["amit"]))["can_add"] is False
    assert (await vu_routes.list_project_verified(S["project_id"], current=S["rahul"]))["can_add"] is True


@pytest.mark.asyncio
async def test_member_cannot_add_verified_update_but_poc_and_admin_can(dbc):
    S = await seed()
    await expect_status(vu_routes.add_project_verified(S["project_id"], {"text": "x"}, current=S["amit"]), 403)
    await vu_routes.add_project_verified(S["project_id"], {"text": "from poc"}, current=S["rahul"])
    await vu_routes.add_project_verified(S["project_id"], {"text": "from admin"}, current=S["admin"])
    await expect_status(vu_routes.add_project_verified(S["project_id"], {"text": "  "}, current=S["rahul"]), 400)
    await expect_status(vu_routes.add_project_verified(S["project_id"], {"text": "x", "visibility": "nope"}, current=S["rahul"]), 400)


@pytest.mark.asyncio
async def test_verified_update_both_is_saved_and_sent_to_the_group(dbc, monkeypatch):
    S = await seed()
    sent = await _sent_capture(monkeypatch)
    r = await vu_routes.add_project_verified(S["project_id"], {"text": "Payment page live on staging", "visibility": "both"}, current=S["rahul"])
    assert r["sent_to_client"] is True and len(sent) == 1 and "Payment page live on staging" in sent[0][1]
    assert r["items"][0]["visibility"] == "both" and r["items"][0]["sent_to_client"] == 1
    # internal only: saved, nothing sent
    await vu_routes.add_project_verified(S["project_id"], {"text": "Internal fact", "visibility": "internal"}, current=S["rahul"])
    assert len(sent) == 1
    # the manager is told about the POC's update, the author is not
    notes = await db.query("SELECT poc_id FROM poc_notifications WHERE kind='verified_update'")
    assert S["satish"]["id"] in {n["poc_id"] for n in notes} and S["rahul"]["id"] not in {n["poc_id"] for n in notes}


@pytest.mark.asyncio
async def test_group_dashboard_verified_update_shows_in_the_project_too(dbc, monkeypatch):
    S = await seed()
    sent = await _sent_capture(monkeypatch)
    r = await group_routes.add_update(S["group_id"], {"text": "Contact form fixed", "visibility": "both"}, current=S["satish"])
    assert r["sent_to_client"] is True and len(sent) == 1
    g = await group_routes.list_group_verified(S["group_id"], current=S["satish"])
    assert [i["text"] for i in g["items"]] == ["Contact form fixed"] and g["items"][0]["project_name"] == "ABC Website"
    p = await vu_routes.list_project_verified(S["project_id"], current=S["rahul"])
    assert [i["text"] for i in p["items"]] == ["Contact form fixed"]
    await expect_status(group_routes.add_update(S["group_id"], {"text": "x"}, current=S["amit"]), 403)


@pytest.mark.asyncio
async def test_remove_verified_update_rules(dbc):
    S = await seed()
    r = await vu_routes.add_project_verified(S["project_id"], {"text": "mine"}, current=S["rahul"])
    uid = r["items"][0]["id"]
    await expect_status(vu_routes.delete_verified(uid, current=S["priya"]), 403)      # another POC may not remove it
    await vu_routes.delete_verified(uid, current=S["satish"])                          # the Main Manager may
    assert (await vu_routes.list_project_verified(S["project_id"], current=S["rahul"]))["items"] == []


@pytest.mark.asyncio
async def test_post_update_both_goes_to_crm_and_whatsapp(dbc, monkeypatch):
    S = await seed()
    sent = await _sent_capture(monkeypatch)
    r = await updates.create_update(S["project_id"], {"department_id": S["tech"], "completed": "Login API", "visibility": "both"}, current=S["rahul"])
    assert r["sent_to_client"] is True and len(sent) == 1
    row = (await updates.list_updates(project_id=S["project_id"], current=S["rahul"]))[0]
    assert row["visibility"] == "both" and row["sent_to_client"] == 1
    await expect_status(updates.create_update(S["project_id"], {"department_id": S["tech"], "completed": "x", "visibility": "both"}, current=S["amit"]), 403)


@pytest.mark.asyncio
async def test_ai_prompt_contains_context_tasks_updates_and_what_the_client_was_told(dbc, monkeypatch):
    S = await seed()
    await group_db.upsert_project_context(S["group_id"], description="ABC e-commerce site", scope="No mobile app",
                                          status="In development", communication_instructions="Client is non-technical")
    await tasks.create_task(S["project_id"], {"title": "Payment page", "department_id": S["tech"], "assigned_user_id": S["amit"]["id"]}, current=S["rahul"])
    await updates.create_update(S["project_id"], {"task_id": (await db.query("SELECT id FROM tasks WHERE title='Payment page'"))[0]["id"],
                                                  "in_progress": "gateway keys added", "visibility": "internal"}, current=S["amit"])
    await vu_routes.add_project_verified(S["project_id"], {"text": "Login API deployed", "visibility": "internal"}, current=S["rahul"])
    thread = await group_db.get_or_create_open_thread(S["group_id"], "ABC Website")
    await group_db.save_group_message(thread_id=thread["id"], group_id=S["group_id"], whatsapp_message_id="m1", sender_jid=CLIENT1,
                                      sender_name="Client One", sender_role="client", direction="in", body="Any update on the site?")
    group = await group_db.get_group(S["group_id"])
    captured = {}

    async def fake_create(**kw):
        captured["system"] = kw["messages"][0]["content"]
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Payment page is in progress."))])
    monkeypatch.setattr(group_ai_replier.openai_client.chat.completions, "create", fake_create)
    out = await group_ai_replier.build_reply(group, thread, "Any update?", "Client One",
                                             routing_ctx={"project_id": S["project_id"], "department_id": S["tech"], "client": "ABC Company"})
    assert out == "Payment page is in progress."
    p = captured["system"]
    for needle in ("ABC e-commerce site", "No mobile app", "In development", "Client is non-technical",
                   "Login API deployed", "confirmed by Rahul", "Payment page", "assigned to Amit", "gateway keys added",
                   "Any update on the site?"):
        assert needle in p, needle
    assert "client NOT told yet" in p
