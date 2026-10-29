"""Everything the AI replier should know before it answers a client in a project group.

Sources (all read fresh on every reply):
  * project context typed on the dashboard (description, scope, communication rules, status)
  * VERIFIED updates (who confirmed what, when, and whether the client was already told)
  * tasks the Main Manager / POCs gave to team members (who, status, due date, latest member note)
  * POC / Main Manager progress updates and member notes
  * what the client was ALREADY told (our earlier messages and posted updates), so the AI answers with what is
    NEW since then instead of repeating itself or saying "I will check with the team"

`format_knowledge` is a pure function (no database) so the prompt logic can be unit-tested on its own.
"""
from datetime import datetime

from server import group_db
from server.repositories import crm
from server.services.verified_updates import legacy_entries

MAX_TASKS = 25
MAX_TEAM_UPDATES = 15
MAX_VERIFIED = 20


def _dt(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("T", " ")[:19])
        except ValueError:
            return None
    return None


def _stamp(value) -> str:
    d = _dt(value)
    return d.strftime("%d %b %H:%M") if d else "earlier"


def _is_new(value, last_told: datetime | None) -> bool:
    d = _dt(value)
    return bool(d and last_told and d > last_told)


def last_told_at(history: list[dict]) -> datetime | None:
    """When we (AI, POC, manager or a posted update) last wrote to the client in this thread."""
    stamps = [_dt(m.get("created_at")) for m in history if m.get("direction") == "out"]
    stamps = [s for s in stamps if s]
    return max(stamps) if stamps else None


async def load(group: dict, project_id: int | None, department_id: int | None = None) -> dict:
    """Raw data for one project (or just the group when it has no CRM project)."""
    project = await crm.get_project(project_id) if project_id else None
    if project is None and not project_id:
        project = await crm.get_project_by_group(group["id"])
        project_id = project["id"] if project else None
    ctx = await group_db.get_project_context(group["id"])
    if project_id:
        verified = await crm.list_verified_updates(project_id=project_id, group_id=group["id"],
                                                   include_group_level=True, limit=MAX_VERIFIED)
    else:
        verified = await crm.list_verified_updates(group_id=group["id"], limit=MAX_VERIFIED)
    verified += legacy_entries(ctx)
    tasks, team_updates, notes = [], [], {}
    if project_id:
        tasks = [t for t in await crm.list_tasks(project_id=project_id) if t["status"] != "cancelled"][:MAX_TASKS]
        team_updates = await crm.list_updates(project_id=project_id, limit=MAX_TEAM_UPDATES * 3)
        for u in team_updates:   # latest member note per task
            if u.get("task_id") and u["task_id"] not in notes:
                notes[u["task_id"]] = u
    return {"project": project, "context": ctx or {}, "verified": verified, "tasks": tasks,
            "team_updates": team_updates, "task_notes": notes, "department_id": department_id}


def _task_line(t: dict, note: dict | None, last_told, dept_id) -> str:
    bits = [f"{t['title']} [{t['status']}]"]
    if t.get("department_name"):
        bits.append(f"dept {t['department_name']}")
    if t.get("assignee_name"):
        bits.append(f"assigned to {t['assignee_name']}")
    else:
        bits.append("not assigned to anyone yet")
    if t.get("due_date"):
        bits.append(f"due {str(t['due_date'])[:10]}" + (" OVERDUE" if t.get("overdue") else ""))
    if t.get("priority") in ("high", "urgent"):
        bits.append(f"priority {t['priority']}")
    if t.get("description"):
        bits.append(f"details: {str(t['description'])[:140]}")
    if note:
        text = note.get("in_progress") or note.get("summary") or note.get("completed") or note.get("next_steps") or ""
        who = note.get("author_name") or "team member"
        bits.append(f"latest member note ({_stamp(note.get('created_at'))}, from {who}, not yet reviewed): {str(text)[:160]}")
    flags = []
    if dept_id and t.get("department_id") == dept_id:
        flags.append("THIS DEPARTMENT")
    if _is_new(t.get("updated_at"), last_told):
        flags.append("CHANGED SINCE YOU LAST WROTE TO THE CLIENT")
    return "- " + "; ".join(bits) + (f"  <{', '.join(flags)}>" if flags else "")


def _team_update_line(u: dict, last_told) -> str:
    parts = []
    for label, key in (("completed", "completed"), ("in progress", "in_progress"), ("blocked", "blocked"),
                       ("next", "next_steps"), ("note", "summary")):
        if u.get(key):
            parts.append(f"{label}: {str(u[key])[:200]}")
    role = "member note" if u.get("task_id") else "team update"
    who = u.get("author_name") or "team"
    dept = f", {u['department_name']}" if u.get("department_name") else ""
    told = "client was told" if u.get("visibility") in ("client", "both") and u.get("sent_to_client") else "client NOT told yet"
    new = "  <NEW since you last wrote to the client>" if _is_new(u.get("created_at"), last_told) else ""
    return f"- {_stamp(u.get('created_at'))} {role} by {who}{dept} ({told}): " + " | ".join(parts) + new


def _verified_line(v: dict, last_told) -> str:
    who = v.get("author_name") or "team"
    role = f" ({str(v['author_role']).replace('_', ' ')})" if v.get("author_role") else ""
    told = "client was told" if v.get("sent_to_client") else "client NOT told yet"
    new = "  <NEW since you last wrote to the client>" if (not v.get("legacy") and _is_new(v.get("created_at"), last_told)) else ""
    proj = f" [{v['project_name']}]" if v.get("project_name") else ""
    return f"- {_stamp(v.get('created_at'))} confirmed by {who}{role}{proj} ({told}): {v['text']}{new}"


def format_knowledge(data: dict, history: list[dict], now: datetime | None = None) -> str:
    now = now or datetime.now()
    last_told = last_told_at(history)
    dept_id = data.get("department_id")
    lines = [f"\nCURRENT DATE/TIME: {now.strftime('%A %d %b %Y, %H:%M')}"]
    lines.append("LAST TIME YOU / THE TEAM WROTE TO THE CLIENT IN THIS CHAT: "
                 + (_stamp(last_told) if last_told else "never (nothing has been sent to the client yet)"))

    v = data.get("verified") or []
    lines.append("\nVERIFIED UPDATES (confirmed by the Main Manager / POC, newest first). These are confirmed facts you may state:")
    lines += [_verified_line(x, last_told) for x in v] or ["(none recorded yet)"]

    tasks = data.get("tasks") or []
    lines.append("\nTASKS GIVEN TO THE TEAM (from the CRM):")
    lines += [_task_line(t, data.get("task_notes", {}).get(t["id"]), last_told, dept_id) for t in tasks] or ["(no tasks recorded)"]

    tu = (data.get("team_updates") or [])[:MAX_TEAM_UPDATES]
    lines.append("\nPROGRESS UPDATES FROM POCs, MANAGERS AND TEAM MEMBERS (newest first):")
    lines += [_team_update_line(u, last_told) for u in tu] or ["(no progress updates recorded)"]

    lines.append("""
HOW TO USE THIS (important):
- Facts you may state: VERIFIED UPDATES; a task whose status is [completed]; items under "completed" in a POC or manager update. A member note is a report, not a confirmation: you may say the work is going on, not that it is finished.
- Status in progress / todo / review is also usable: say the team is working on it, or it is being tested. For a blocked task do not say "blocked": say it is being looked into, unless a verified update gives the client-safe reason. Do not invent dates, percentages or details that are not written above.
- Before you answer "any update?", look at what the client was ALREADY told (your earlier messages and every item marked "client was told"). Give the NEW information first: items marked NEW, CHANGED, or "client NOT told yet". Do not repeat what was already said.
- If nothing is new since the last message to the client, say so honestly in fresh words and name what is still being worked on (from the tasks above), for example "No change since yesterday, the team is still on the payment page." Never copy an earlier message word for word.
- Say "I'll check with the team" ONLY when nothing above covers the question. If tasks are listed and being worked on, answer from them.
- Talk about the team, not individuals: do not name team members, departments, the CRM, tasks or dashboards, and never reveal internal chatter, blame or problems the client was not told about. Use what is relevant to this client's question and ignore the rest.
- If the client asks about something specific, answer only about that. Keep the reply short.""")
    return "\n".join(lines) + "\n"


async def build(group: dict, history: list[dict], project_id: int | None, department_id: int | None = None) -> tuple[dict, str]:
    data = await load(group, project_id, department_id)
    return data, format_knowledge(data, history)
