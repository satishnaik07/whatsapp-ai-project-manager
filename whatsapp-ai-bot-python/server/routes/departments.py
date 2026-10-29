from fastapi import APIRouter, Depends, HTTPException

from server import permissions
from server.repositories import crm
from server.routes.deps import get_current_poc
from server.services import audit, notification_service

router = APIRouter()


def _can_create(user):
    if user["role"] not in ("admin", "main_manager"):
        raise HTTPException(status_code=403, detail="Only the Main Manager (or admin) can create departments")


@router.post("/departments")
async def create_department(payload: dict, current=Depends(get_current_poc)):
    """A Main Manager's departments belong to them (another manager can also have a 'Tech').
    Admin-created ones are shared (owner NULL) unless owner_user_id is given."""
    _can_create(current)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    owner = current["id"] if current["role"] == "main_manager" else payload.get("owner_user_id")
    if await crm.department_name_taken(name, owner):
        raise HTTPException(status_code=409, detail="Department already exists")
    did = await crm.create_department(name, payload.get("description"), owner)
    await audit.record(current["id"], "department_created", "department", did, before={}, after={"name": name, "owner_user_id": owner}, department_id=did)
    await notification_service.notify_event("department_created", f"New department: {name}", actor_id=current["id"], manager_id=owner,
                                            entity_type="department", entity_id=did)
    return await crm.get_department(did)


@router.get("/departments")
async def list_departments(current=Depends(get_current_poc)):
    return await crm.list_departments(viewer=current)


@router.patch("/departments/{dept_id}")
async def update_department(dept_id: int, payload: dict, current=Depends(get_current_poc)):
    dept = await crm.get_department(dept_id)
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    if current["role"] != "admin" and dept["owner_user_id"] != current["id"]:
        raise HTTPException(status_code=403, detail="You can only change your own departments")
    if payload.get("name") and await crm.department_name_taken(payload["name"].strip(), dept["owner_user_id"], dept_id):
        raise HTTPException(status_code=409, detail="Department already exists")
    fields = {k: payload[k] for k in ("name", "description", "active") if k in payload}
    old, new = audit.changes(dept, fields)
    await crm.update_department(dept_id, fields)
    if new:
        await audit.record(current["id"], "department_updated", "department", dept_id, before=old, after=new, department_id=dept_id)
        await notification_service.notify_event("department_updated", f"Department edited: {dept['name']}", actor_id=current["id"],
                                                manager_id=dept["owner_user_id"], entity_type="department", entity_id=dept_id)
    return await crm.get_department(dept_id)


@router.delete("/departments/{dept_id}")
async def delete_department(dept_id: int, current=Depends(get_current_poc)):
    """Soft delete: hidden from pickers, but projects, tasks and history that used it keep their link."""
    permissions.require(current, "department.delete")
    dept = await crm.get_department(dept_id)
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    await crm.soft_delete("department", dept_id)
    await audit.record(current["id"], "department_deleted", "department", dept_id, before={"name": dept["name"]}, after={"deleted": True}, department_id=dept_id)
    return {"ok": True}


@router.post("/departments/{dept_id}/restore")
async def restore_department(dept_id: int, current=Depends(get_current_poc)):
    permissions.require(current, "department.restore")
    if not await crm.get_department(dept_id, include_deleted=True):
        raise HTTPException(status_code=404, detail="Department not found")
    await crm.restore("department", dept_id)
    await audit.record(current["id"], "department_restored", "department", dept_id, department_id=dept_id)
    return await crm.get_department(dept_id)
