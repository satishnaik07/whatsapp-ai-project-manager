from fastapi import APIRouter, Depends, HTTPException

from server import group_db
from server.repositories import crm
from server.routes.deps import dept_scope, get_current_poc, is_member_role, load_project, poc_of_department
from server.services import audit, notification_service, poc_router

router = APIRouter()


@router.post("/projects/{project_id}/updates")
async def create_update(project_id: int, payload: dict, current=Depends(get_current_poc)):
    """Department progress update. visibility='internal' stays in the CRM; 'client' is posted to the WhatsApp group;
    'both' is saved as a team record in the CRM AND posted to the WhatsApp group."""
    project = await load_project(project_id, current)
    dept_id = payload.get("department_id")
    visibility = payload.get("visibility", "internal")
    if visibility not in ("internal", "client", "both"):
        raise HTTPException(status_code=400, detail="visibility must be 'internal', 'client' or 'both'")
    is_manager = await crm.user_can_manage_project(current, project_id)
    is_poc = await poc_of_department(current, project_id, dept_id)
    in_dept = dept_id is not None and dept_id in await crm.user_department_ids_in_project(current["id"], project_id)
    if payload.get("task_id") and dept_id is None:
        t0 = await crm.get_task(int(payload["task_id"]))
        dept_id = t0["department_id"] if t0 else None
        is_poc = await poc_of_department(current, project_id, dept_id)
        in_dept = dept_id is not None and dept_id in await crm.user_department_ids_in_project(current["id"], project_id)
    if not (is_manager or is_poc or (in_dept and visibility == "internal")):
        raise HTTPException(status_code=403, detail="Not authorized to post this update")
    if not any(payload.get(k) for k in ("completed", "in_progress", "blocked", "next_steps", "summary")):
        raise HTTPException(status_code=400, detail="Add at least one of completed / in_progress / blocked / next_steps / summary")

    task_id = payload.get("task_id")
    if task_id:
        task = await crm.get_task(int(task_id))
        if not task or task["project_id"] != project_id:
            raise HTTPException(status_code=404, detail="Task not found in this project")
        if not is_manager and not is_poc and task["assigned_user_id"] != current["id"]:
            raise HTTPException(status_code=403, detail="You can only add notes to tasks assigned to you")
        dept_id = dept_id or task["department_id"]
    uid = await crm.create_update(project_id, dept_id, current["id"], payload.get("completed"), payload.get("in_progress"),
                                  payload.get("blocked"), payload.get("next_steps"), payload.get("summary"), visibility,
                                  int(task_id) if task_id else None)
    dept = await crm.get_department(dept_id) if dept_id else None
    row = (await crm.list_updates(project_id=project_id, limit=1))[0]
    text = crm.format_update_text(row, dept["name"] if dept else None)
    await audit.record(current["id"], "update_created", "project", project_id, f"update={uid} visibility={visibility}")

    # reporting chain: a member's note goes to their POC, a POC's update goes to the Main Manager
    to = await crm.update_recipient(project, dept_id, current)
    if to:
        await notification_service.notify(to, "project_update", text.replace("\n", " | ")[:480], project_id)

    sent, delivery_error = False, None
    if visibility in ("client", "both"):
        if not project.get("group_id"):
            delivery_error = "Project has no WhatsApp group"
        else:
            group = await group_db.get_group(project["group_id"])
            thread = await group_db.get_or_create_open_thread(group["id"], group.get("project_name"))
            sent, _acc = await poc_router.send_with_fallback(group, {"assigned_poc_id": current["id"]}, text)
            await group_db.save_group_message(
                thread_id=thread["id"], group_id=group["id"], whatsapp_message_id=None, sender_jid=None,
                sender_name=current["name"] or current["username"], sender_role="internal", direction="out", body=text,
                processing_status="processed", delivery_status="sent" if sent else "failed")
            if sent:
                await crm.mark_update_sent(uid)
            else:
                delivery_error = "WhatsApp send failed"
    return {"id": uid, "sent_to_client": sent, "delivery_error": delivery_error, "text": text}


@router.get("/updates")
async def list_updates(project_id: int | None = None, current=Depends(get_current_poc)):
    if project_id is not None:
        await load_project(project_id, current)
    pids = None if (current["role"] == "admin" and project_id is None) else await crm.accessible_project_ids(current)
    rows = await crm.list_updates(project_id=project_id, project_ids=pids, limit=100,
                                  author_id=current["id"] if is_member_role(current) else None)
    out, scopes = [], {}
    for u in rows:
        if u["project_id"] not in scopes:
            scopes[u["project_id"]] = await dept_scope(current, u["project_id"])
        sc = scopes[u["project_id"]]
        if sc is None or u["department_id"] in sc:
            out.append(u)
    return out
