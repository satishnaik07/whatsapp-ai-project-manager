from fastapi import APIRouter, Depends, HTTPException

from server import permissions
from server.repositories import crm
from server.routes.deps import dept_scope, get_current_poc, is_member_role, load_project, need, poc_of_department
from server.services import audit, notification_service

router = APIRouter()
PRIORITIES = ("low", "medium", "high", "urgent")


async def _can_manage_dept(user, project_id, department_id) -> bool:
    return await crm.user_can_manage_project(user, project_id) or await poc_of_department(user, project_id, department_id)


@router.post("/projects/{project_id}/tasks")
async def create_task(project_id: int, payload: dict, current=Depends(get_current_poc)):
    project = await load_project(project_id, current)
    dept_id = payload.get("department_id")
    if not await _can_manage_dept(current, project_id, dept_id):
        raise HTTPException(status_code=403, detail="Only the Main Manager or the department POC can create tasks")
    if payload.get("priority", "medium") not in PRIORITIES or payload.get("status", "todo") not in crm.TASK_STATUSES:
        raise HTTPException(status_code=400, detail="invalid priority or status")
    if payload.get("assigned_user_id"):
        who = int(payload["assigned_user_id"])
        if not await crm.get_user(who):
            raise HTTPException(status_code=404, detail="Assignee not found")
        team = {m["user_id"] for d in await crm.list_project_departments(project_id)
                if d["department_id"] == dept_id for m in d["members"]}
        if who not in team:
            raise HTTPException(status_code=400, detail="Assignee is not a member of this department in this project")
    tid = await crm.create_task(project_id, need(payload.get("title"), "title"), dept_id, payload.get("assigned_user_id"),
                                payload.get("description"), payload.get("priority", "medium"), payload.get("status", "todo"),
                                payload.get("due_date"), current["id"])
    await audit.record(current["id"], "task_created", "task", tid, before={}, after={"title": payload["title"], "department_id": dept_id,
                       "assigned_user_id": payload.get("assigned_user_id")}, project_id=project_id, department_id=dept_id)
    if payload.get("assigned_user_id") and int(payload["assigned_user_id"]) != current["id"]:
        await notification_service.notify(int(payload["assigned_user_id"]), "task_assigned",
                                          f"New task: {payload['title']}", project_id)
    return await crm.get_task(tid)


@router.patch("/tasks/{task_id}")
async def update_task(task_id: int, payload: dict, current=Depends(get_current_poc)):
    task = await crm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = await load_project(task["project_id"], current)
    is_lead = await _can_manage_dept(current, task["project_id"], task["department_id"])
    is_assignee = task["assigned_user_id"] == current["id"]
    if not (is_lead or is_assignee):
        raise HTTPException(status_code=403, detail="Not authorized to change this task")
    fields = dict(payload)
    if not is_lead:  # assignee: progress + comments only
        fields = {k: v for k, v in payload.items() if k in ("status", "description")}
    if fields.get("status") and fields["status"] not in crm.TASK_STATUSES:
        raise HTTPException(status_code=400, detail="invalid status")
    if fields.get("priority") and fields["priority"] not in PRIORITIES:
        raise HTTPException(status_code=400, detail="invalid priority")
    old_v, new_v = audit.changes(task, {k: v for k, v in fields.items() if k in ("title", "description", "priority", "status", "due_date", "assigned_user_id", "department_id")})
    await crm.update_task(task_id, fields)
    new = await crm.get_task(task_id)
    if new_v:
        await audit.record(current["id"], "task_updated", "task", task_id, before=old_v, after=new_v, project_id=task["project_id"], department_id=task["department_id"])
    if "assigned_user_id" in new_v and new_v["assigned_user_id"] and int(new_v["assigned_user_id"]) != current["id"]:
        await notification_service.notify(int(new_v["assigned_user_id"]), "task_assigned", f"Task assigned: {task['title']}", task["project_id"],
                                          title="Task assigned to you", entity_type="task", entity_id=task_id)
    if fields.get("status") and fields["status"] != task["status"]:
        # reporting chain: member -> department POC, POC -> Main Manager
        to = await crm.update_recipient(project, task["department_id"], current)
        if to:
            await notification_service.notify(to, "task_update",
                                              f"{current['name'] or current['username']}: '{task['title']}' is now {fields['status']}", task["project_id"])
    return new


@router.get("/tasks")
async def list_tasks(project_id: int | None = None, department_id: int | None = None, status: str | None = None,
                     mine: bool = False, overdue: bool = False, current=Depends(get_current_poc)):
    if project_id is not None:
        await load_project(project_id, current)
    pids = None if (current["role"] == "admin" and project_id is None) else await crm.accessible_project_ids(current)
    rows = await crm.list_tasks(project_id=project_id, department_id=department_id, status=status,
                                assigned_user_id=current["id"] if mine else None, project_ids=pids)
    out = []
    scopes: dict = {}
    for t in rows:
        if t["project_id"] not in scopes:
            scopes[t["project_id"]] = await dept_scope(current, t["project_id"])
        sc = scopes[t["project_id"]]
        if is_member_role(current) and t["assigned_user_id"] != current["id"] and not await crm.user_can_manage_project(current, t["project_id"]):
            continue          # members see only the tasks assigned to them
        if sc is not None and t["department_id"] not in sc and t["assigned_user_id"] != current["id"]:
            continue
        if overdue and not t["overdue"]:
            continue
        out.append(t)
    return out


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int, current=Depends(get_current_poc)):
    """Soft delete. Only the Main Manager (or admin) of the project; a POC can cancel a task instead."""
    task = await crm.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    permissions.require(current, "task.delete")
    await load_project(task["project_id"], current, manage=True)
    await crm.soft_delete("task", task_id)
    await audit.record(current["id"], "task_deleted", "task", task_id, before={"title": task["title"], "status": task["status"]}, after={"deleted": True},
                       project_id=task["project_id"], department_id=task["department_id"])
    if task["assigned_user_id"] and task["assigned_user_id"] != current["id"]:
        await notification_service.notify(task["assigned_user_id"], "task_removed", f"Task removed: {task['title']}", task["project_id"], title="A task was removed")
    return {"ok": True}
