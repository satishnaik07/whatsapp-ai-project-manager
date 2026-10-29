"""Shared auth / permission helpers for the CRM routes."""
from fastapi import HTTPException

from server.auth import get_current_poc  # noqa: F401  (re-exported dependency)
from server.repositories import crm


def require_admin(user: dict):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")


def require_manager_role(user: dict):
    if user["role"] not in crm.MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="Only admins and main managers can do this")


async def load_project(project_id: int, user: dict, manage: bool = False) -> dict:
    project = await crm.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    ok = await (crm.user_can_manage_project(user, project_id) if manage else crm.user_can_access_project(user, project_id))
    if not ok:
        raise HTTPException(status_code=403, detail="Not authorized for this project")
    return project


async def poc_of_department(user: dict, project_id: int, department_id: int | None) -> bool:
    """True if the user is this department's POC in this project."""
    if department_id is None:
        return False
    rows = await crm.list_project_departments(project_id)
    return any(r["department_id"] == department_id and r["poc_user_id"] == user["id"] for r in rows)


def need(value, name: str):
    if value in (None, ""):
        raise HTTPException(status_code=400, detail=f"{name} is required")
    return value


async def dept_scope(user: dict, project_id: int) -> set[int] | None:
    """None = may see the whole project (admin / its Main Manager).
    Otherwise the set of department ids the user belongs to in it. A POC or
    member must not see other departments' tasks, updates or conversations."""
    if user["role"] == "admin" or await crm.user_can_manage_project(user, project_id):
        return None
    return set(await crm.user_department_ids_in_project(user["id"], project_id))


def is_member_role(user: dict) -> bool:
    return user["role"] == "department_member"


async def member_only(user: dict, project_id: int) -> bool:
    """A department member sees nothing but their own tasks and notes."""
    return is_member_role(user) and not await crm.user_can_manage_project(user, project_id)
