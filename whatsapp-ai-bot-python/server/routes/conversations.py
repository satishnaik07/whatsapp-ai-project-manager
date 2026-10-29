from fastapi import APIRouter, Depends, HTTPException

from server import group_db
from server.repositories import crm
from server.routes.deps import dept_scope, get_current_poc, load_project, member_only, require_admin
from server.services import audit, notification_service

router = APIRouter()


async def _thread_project(thread_id: int, user: dict, manage: bool = False):
    thread = await group_db.get_thread(thread_id)
    if not thread:
        raise HTTPException(status_code=404, detail="Thread not found")
    project = None
    if thread.get("current_project_id"):
        project = await crm.get_project(thread["current_project_id"])
    if not project:
        project = await crm.get_project_by_group(thread["group_id"])
    if not project:
        raise HTTPException(status_code=404, detail="This thread's group has no CRM project")
    await load_project(project["id"], user, manage=manage)
    return thread, project


@router.get("/projects/{project_id}/conversations")
async def project_conversations(project_id: int, current=Depends(get_current_poc)):
    project = await load_project(project_id, current)
    if not project.get("group_id") or await member_only(current, project_id):
        return []
    rows = await crm.list_project_threads(project["group_id"], project_id)
    scope = await dept_scope(current, project_id)
    if scope is None:
        return rows
    return [r for r in rows if r["current_department_id"] in scope or r["assigned_poc_id"] == current["id"]]


@router.get("/conversations/{thread_id}/routing")
async def routing_history(thread_id: int, current=Depends(get_current_poc)):
    thread, project = await _thread_project(thread_id, current)
    if await member_only(current, project["id"]):
        raise HTTPException(status_code=403, detail="Members cannot see conversations")
    scope = await dept_scope(current, project["id"])
    if scope is not None and thread.get("current_department_id") not in scope and thread.get("assigned_poc_id") != current["id"]:
        raise HTTPException(status_code=403, detail="Not authorized for this conversation")
    return {
        "thread": {k: thread.get(k) for k in ("id", "status", "ai_paused", "current_department_id", "assigned_poc_id",
                                              "routing_status", "routing_confidence")},
        "events": await crm.list_routing(thread_id),
        "assignments": await crm.list_assignments(thread_id),
    }


@router.patch("/conversations/{thread_id}/routing")
async def reassign(thread_id: int, payload: dict, current=Depends(get_current_poc)):
    """Main Manager / admin: change the department and/or POC of a thread."""
    thread, project = await _thread_project(thread_id, current, manage=True)
    depts = await crm.list_project_departments(project["id"])
    dept_id = payload.get("department_id", thread.get("current_department_id"))
    dept = next((d for d in depts if d["department_id"] == dept_id), None)
    if dept_id and not dept:
        raise HTTPException(status_code=400, detail="Department is not assigned to this project")
    poc_id = payload.get("poc_user_id") or (dept["poc_user_id"] if dept else None) or project.get("main_manager_id")
    if not poc_id or not await crm.get_user(int(poc_id)):
        raise HTTPException(status_code=404, detail="POC not found")
    await crm.set_thread_routing(thread_id, dept_id, int(poc_id), "routed", 1.0)
    await crm.log_routing(thread_id, "manual_reassign", project["id"], None, dept_id, int(poc_id), None, 1.0, f"by user {current['id']}")
    if dept_id != thread.get("current_department_id"):
        await audit.record(current["id"], "department_changed", "project", project["id"], f"thread={thread_id} dept={dept_id}")
    if int(poc_id) != thread.get("assigned_poc_id"):
        await audit.record(current["id"], "poc_changed", "project", project["id"], f"thread={thread_id} poc={poc_id}")
        await notification_service.notify(int(poc_id), "reassigned", f"Conversation reassigned to you ({project['name']})", project["id"], thread_id)
    return {"ok": True}


@router.post("/escalations/{escalation_id}/resolve")
async def resolve_escalation(escalation_id: int, current=Depends(get_current_poc)):
    from server.routes.deps import require_manager_role
    require_manager_role(current)
    await crm.resolve_escalation(escalation_id)
    return {"ok": True}


@router.get("/audit")
async def audit_log(limit: int = 100, current=Depends(get_current_poc)):
    require_admin(current)
    return await crm.list_audit(min(limit, 500))
