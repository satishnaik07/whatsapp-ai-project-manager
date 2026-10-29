"""
Role-permission matrix. Add, edit and delete are SEPARATE permissions: being allowed to create a record
never implies edit or delete rights. Routes call require()/allowed(); the frontend asks GET /api/permissions
so it only shows buttons the backend would accept (the backend still enforces every call).

"own" scopes are enforced by the routes (a Main Manager only for their projects/departments/users, a POC only
for their department, a member only for tasks assigned to them).
"""
from fastapi import HTTPException

A, M, P, B = "admin", "main_manager", "department_poc", "department_member"   # B = member

MATRIX: dict[str, set[str]] = {
    # clients and projects are onboarded by the admin; managers can edit the projects they run
    "client.view": {A, M, P}, "client.create": {A}, "client.edit": {A}, "client.delete": {A}, "client.restore": {A},
    "project.view": {A, M, P, B}, "project.create": {A}, "project.edit": {A, M}, "project.delete": {A}, "project.restore": {A},
    # departments belong to the Main Manager who made them
    "department.view": {A, M, P, B}, "department.create": {A, M}, "department.edit": {A, M}, "department.delete": {A}, "department.restore": {A},
    # people
    "user.view": {A, M}, "user.create": {A}, "user.edit": {A, M}, "user.delete": {A}, "user.restore": {A},
    "role.assign": {A},
    # whatsapp groups and group <-> project/department mapping
    "group.map": {A, M}, "group.unmap": {A, M}, "group.view": {A, M},
    "poc.assign": {A, M}, "member.assign": {A, M},
    "whatsapp.link_own": {A, M, P},            # members never link WhatsApp
    # work
    "task.view": {A, M, P, B}, "task.create": {A, M, P}, "task.edit": {A, M, P, B}, "task.delete": {A, M},
    "update.create": {A, M, P, B}, "update.send_to_client": {A, M, P},
    "conversation.view": {A, M, P}, "conversation.reassign": {A, M},
    "approval.decide": {A, M, P},
    "audit.view": {A}, "notification.view": {A, M, P, B},
}


def canon(role: str) -> str:
    return P if role == "poc" else role      # legacy 'poc' behaves as department_poc


def allowed(user: dict, action: str) -> bool:
    return canon(user["role"]) in MATRIX.get(action, set())


def require(user: dict, action: str):
    if not allowed(user, action):
        raise HTTPException(status_code=403, detail=f"Your role is not allowed to do this ({action})")


def for_role(role: str) -> list[str]:
    return sorted(a for a, roles in MATRIX.items() if canon(role) in roles)
