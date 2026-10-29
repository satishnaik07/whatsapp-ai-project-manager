"""
Message routing for client messages in a configured project group.

    client message -> project -> thread memory -> (escalation rules)
      -> AI department classifier -> department -> POC -> availability
      -> (Main Manager fallback) -> sending account -> notifications

Only ONE responsible user is chosen per message. Low confidence or unknown
department goes to the Main Manager. Returns None when the group has no CRM
project (legacy group): the caller then keeps the original single-POC flow.
"""
import logging
import os
import re
from dataclasses import dataclass, field

from server import group_db
from server.repositories import crm
from server.services import audit, department_classifier, notification_service, poc_router

log = logging.getLogger("router")

DEFAULT_THRESHOLD = float(os.getenv("DEPARTMENT_CLASSIFICATION_CONFIDENCE_THRESHOLD", "0.75"))
POC_FALLBACK_ENABLED = os.getenv("POC_OFFLINE_FALLBACK_ENABLED", "true").lower() != "false"

ESCALATION_RULES = {
    "pricing": r"\b(price|pricing|quotation|quote|budget|discount|refund|extra charges?|kitna paisa|kitne paise)\b",
    "contract_legal": r"\b(contract|agreement|legal|lawyer|nda|sue)\b",
    "angry_client": r"\b(worst|useless|fed up|unacceptable|disappointed|frustrated|pathetic|fraud|cheated|bakwas|bekar|bekaar)\b",
    "manager_requested": r"(speak|talk|baat)\s+(to|with)?\s*(the\s+)?manager|manager\s+(se|ko)\b|connect me to (the )?manager",
}


def detect_escalation(text: str) -> str | None:
    t = (text or "").lower()
    for reason, pattern in ESCALATION_RULES.items():
        if re.search(pattern, t):
            return reason
    return None


@dataclass
class RoutingDecision:
    project_id: int
    department_id: int | None = None
    department_name: str | None = None
    poc_user_id: int | None = None          # the ONE responsible user
    account_id: str | None = None           # account that should send the reply
    confidence: float | None = None
    reason: str = ""
    routing_status: str = "routed"          # routed | manager_fallback | needs_clarification
    fallback: str | None = None             # why the Main Manager is responsible
    escalation: str | None = None
    force_approval: bool = False
    ask_clarification: bool = False
    notify_also: int | None = None          # e.g. the matched POC on an escalation
    ai_context: dict = field(default_factory=dict)


def _find_department(depts: list[dict], name: str | None) -> dict | None:
    if not name:
        return None
    n = name.strip().lower()
    for d in depts:
        if str(d.get("label", d["name"])).strip().lower() == n:
            return d
    return None


async def _ai_context(project: dict, dept: dict | None, decision: RoutingDecision) -> dict:
    tasks = await crm.relevant_tasks_for_ai(project["id"], dept["department_id"] if dept else None)
    return {
        "client": project.get("client_name"),
        "project": project.get("name"),
        "project_id": project.get("id"),
        "department_id": dept["department_id"] if dept else None,
        "department": dept["name"] if dept else None,
        "department_description": dept.get("description") if dept else None,
        "tasks": [{"title": t["title"], "status": t["status"]} for t in tasks],
        "ask_clarification": decision.ask_clarification,
        "manager_handling": decision.routing_status == "manager_fallback",
    }


async def route_client_message(group: dict, thread: dict, client_message_id: int | None,
                               body: str, sender_name: str | None = None) -> RoutingDecision | None:
    projects = await crm.list_projects_by_group(group["id"])
    if not projects:
        return None  # legacy group, keep old behaviour
    multi = len(projects) > 1
    # a group may serve several projects: the thread remembers which one the conversation is about
    project = next((p for p in projects if p["id"] == thread.get("current_project_id")), projects[0])
    tid = thread["id"]
    # candidate (project, department) pairs. With one project this is just that project's departments.
    depts = []
    for pr in projects:
        for r in await crm.list_project_departments(pr["id"]):
            row = dict(r)
            row["_project"] = pr
            row["label"] = f"{pr['name']} / {r['name']}" if multi else r["name"]
            depts.append(row)
    pid = project["id"]

    def _threshold(pr):
        return float(pr["routing_threshold"]) if pr.get("routing_threshold") is not None else DEFAULT_THRESHOLD
    threshold = _threshold(project)
    log.info("[ROUTER] incoming group=%s thread=%s projects=%s sender=%s", group["id"], tid, [p["id"] for p in projects], sender_name)
    await crm.log_routing(tid, "message_received", pid, client_message_id)

    d = RoutingDecision(project_id=pid)
    manager_id = project.get("main_manager_id") or group.get("assigned_poc_id")
    esc = detect_escalation(body)
    classified, force_clarify, secondary = None, False, []

    # ---- classify (thread memory: current department is passed in) ----------
    cur = next((x for x in depts if x["department_id"] == thread.get("current_department_id") and x["_project"]["id"] == pid), None)
    chosen = None
    if depts:
        history = await group_db.load_thread_history(tid, limit=12)
        from server.group_ai_replier import _format_history  # local import: keep router import-light
        try:
            classified = await department_classifier.classify(
                body, [{"name": x["label"], "description": x.get("description")} for x in depts], _format_history(history),
                cur["label"] if cur else None, thread.get("routing_status"))
            d.confidence = classified["confidence"]
            d.reason = classified["reason"]
            chosen = _find_department(depts, classified["department"])      # validated against real assignments: the AI cannot invent a department
            if chosen:
                project, pid = chosen["_project"], chosen["_project"]["id"]
                threshold = _threshold(project)
                manager_id = project.get("main_manager_id") or group.get("assigned_poc_id")
                d.project_id = pid
            if classified["needs_manager"] and not esc:
                esc = "classifier_needs_manager"
            force_clarify = bool(classified.get("needs_clarification"))
            if classified.get("cross_department"):
                d.force_approval = True      # two teams involved: a human approves so replies do not conflict
                secondary = [x for x in (_find_department(depts, n) for n in classified.get("also_departments", []))
                             if x and chosen and x is not chosen and x["_project"]["id"] == pid]
            await crm.log_routing(tid, "classified", pid, client_message_id,
                                  chosen["department_id"] if chosen else None, None, None, d.confidence, d.reason)
        except Exception as err:  # noqa: BLE001
            log.error("[ROUTER] classification failed group=%s thread=%s: %s", group["id"], tid, err)
            await crm.log_routing(tid, "classification_failed", pid, client_message_id, reason=str(err))
            await audit.record(None, "classification_failed", "thread", tid, str(err)[:300])
            if cur:  # keep the thread's current department rather than losing context
                chosen, d.confidence, d.reason = cur, thread.get("routing_confidence"), "classifier unavailable, kept current department"
                d.confidence = float(d.confidence) if d.confidence is not None else threshold
                project, pid = cur["_project"], cur["_project"]["id"]
    else:
        d.reason = "no departments configured for this project"

    # ---- decide the responsible user ------------------------------------
    if chosen and (d.confidence or 0) >= threshold and not force_clarify:
        d.department_id, d.department_name = chosen["department_id"], chosen["name"]
        d.project_id = pid
        problem = poc_router.poc_problem(chosen)
        if problem is None:
            d.poc_user_id = chosen["poc_user_id"]
            d.account_id = chosen["poc_account_id"]
        elif POC_FALLBACK_ENABLED:
            d.fallback = f"poc_unavailable:{problem}"
            d.routing_status = "manager_fallback"
            await crm.log_routing(tid, "poc_unavailable", pid, client_message_id, d.department_id,
                                  chosen.get("poc_user_id"), None, d.confidence, problem)
            await audit.record(None, "routing_failure", "thread", tid, f"{chosen['name']} POC unavailable: {problem}")
            log.warning("[ROUTER] POC unavailable department=%s problem=%s -> Main Manager", chosen["name"], problem)
        else:
            d.poc_user_id = chosen.get("poc_user_id")
    else:
        d.fallback = "unknown_department" if not chosen else "low_confidence"
        d.routing_status = "needs_clarification"
        d.ask_clarification = True
        chosen = None if not chosen else chosen
        if chosen:  # remember the best guess but do not route to its POC
            d.department_id, d.department_name = chosen["department_id"], chosen["name"]

    if esc:
        # Sensitive topics are handled by the Main Manager; the department POC
        # (if one was matched) is still told, but is not the responsible user.
        d.escalation = esc
        d.force_approval = True
        if d.poc_user_id and d.poc_user_id != manager_id:
            d.notify_also = d.poc_user_id
        d.poc_user_id = None
        d.fallback = d.fallback or f"escalation:{esc}"
        d.routing_status = "manager_fallback"
        d.ask_clarification = False

    if d.poc_user_id is None:  # Main Manager is responsible
        if manager_id:
            d.poc_user_id = manager_id
            d.account_id = await poc_router.user_connected_account(manager_id) or group["account_id"]
        else:
            log.error("[ROUTER] no Main Manager configured for project=%s", pid)
            await audit.record(None, "routing_failure", "project", pid, "No Main Manager configured")
            await notification_service.notify_admins("routing_failure", f"No Main Manager configured for project {project['name']}", pid, tid)
            d.account_id = group["account_id"]
    if not d.account_id:
        d.account_id = group["account_id"]

    # ---- persist (thread memory + routing history) ---------------------
    prev_dept = thread.get("current_department_id")
    await crm.set_thread_routing(tid, d.department_id, d.poc_user_id, d.routing_status, d.confidence, pid)
    if prev_dept and d.department_id and prev_dept != d.department_id:
        await crm.log_routing(tid, "department_switch", pid, client_message_id, d.department_id, d.poc_user_id, None, d.confidence,
                              f"from department {prev_dept}")
    await crm.log_routing(tid, "routed" if d.routing_status == "routed" else d.routing_status, pid, client_message_id,
                          d.department_id, d.poc_user_id, d.account_id, d.confidence, d.fallback or d.reason)
    if esc:
        await crm.create_escalation(tid, pid, client_message_id, esc, (body or "")[:300])
        await audit.record(None, "manager_escalation", "thread", tid, esc, project_id=pid)

    # ---- structured AI result + assignment records (one row per responsible team) -------------
    urgency = (classified or {}).get("urgency", "normal")
    requires_approval = bool(d.force_approval or (group.get("ai_mode") or "approval") == "approval")
    if classified is not None and client_message_id is not None:
        await crm.save_classification(client_message_id, tid, pid, d.department_id, {**classified, "confidence": d.confidence,
                                      "needs_clarification": force_clarify or d.ask_clarification}, requires_approval)
    if client_message_id is not None:
        await crm.add_assignment(client_message_id, tid, pid, d.department_id, d.poc_user_id,
                                 "primary" if d.routing_status == "routed" else "manager")
        for sec in secondary:       # also involves another department: it is told, but the primary responds
            if poc_router.poc_problem(sec) is None:
                await crm.add_assignment(client_message_id, tid, pid, sec["department_id"], sec["poc_user_id"], "secondary")

    # ---- targeted notifications (never everyone) ------------------------
    kind = "client_message" if d.routing_status == "routed" else "manager_fallback"
    label = d.department_name or "unrouted"
    text = f"[{project['name']}] {label}: {(body or '')[:120]}"
    targets = {d.poc_user_id: kind}
    if esc and manager_id:
        targets[manager_id] = "escalation"
    if d.notify_also:
        targets[d.notify_also] = "escalation"
    urgent = urgency in ("high", "urgent")
    title = f"{'URGENT: ' if urgent else ''}{'New client message' if d.routing_status == 'routed' else 'Needs manager attention'} ({label})"
    for sec in secondary:
        if sec["poc_user_id"] and sec["poc_user_id"] not in targets and poc_router.poc_problem(sec) is None:
            targets[sec["poc_user_id"]] = "assignment"
    for uid, k in targets.items():
        await notification_service.notify(uid, k, text, pid, tid, client_message_id, title=title, entity_type="thread", entity_id=tid,
                                          dedupe_key=f"msg{client_message_id}:{k}" if client_message_id is not None else None)

    await audit.record(None, "message_routed", "thread", tid,
                       f"dept={d.department_name} poc={d.poc_user_id} account={d.account_id} conf={d.confidence} status={d.routing_status}")
    log.info("[ROUTER] group=%s thread=%s department=%s confidence=%s poc=%s account=%s status=%s fallback=%s",
             group["id"], tid, d.department_name, d.confidence, d.poc_user_id, d.account_id, d.routing_status, d.fallback)

    dept_row = next((x for x in depts if x["department_id"] == d.department_id and x["_project"]["id"] == pid), None)
    d.ai_context = await _ai_context(project, dept_row, d)
    return d
