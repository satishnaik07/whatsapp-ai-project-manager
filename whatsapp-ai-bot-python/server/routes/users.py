from fastapi import APIRouter, Depends, HTTPException

from server import permissions
from server.auth import hash_password
from server.repositories import crm
from server.routes.deps import get_current_poc, require_admin, require_manager_role
from server.services import audit, notification_service

router = APIRouter()


@router.post("/users")
async def create_user(payload: dict, current=Depends(get_current_poc)):
    require_admin(current)
    username, password = payload.get("username"), payload.get("password")
    role = payload.get("role", "department_poc")
    if role not in crm.ROLES:
        raise HTTPException(status_code=400, detail=f"role must be one of {', '.join(crm.ROLES)}")
    if not username or not password or len(password) < 8:
        raise HTTPException(status_code=400, detail="username and a password (8+ chars) are required")
    try:
        uid = await crm.create_user(username, hash_password(password), payload.get("name") or username,
                                    role, payload.get("email"), payload.get("phone"))
    except Exception as err:  # noqa: BLE001
        if "Duplicate" in str(err) or "1062" in str(err):
            raise HTTPException(status_code=409, detail="Username already exists")
        raise
    if payload.get("department_ids"):
        await crm.set_user_departments(uid, [int(x) for x in payload["department_ids"]])
    if payload.get("account_id"):
        await crm.map_account_user(payload["account_id"], uid)
        await audit.record(current["id"], "whatsapp_account_linked", "user", uid, payload["account_id"])
    await audit.record(current["id"], "user_created", "user", uid, role)
    return await crm.get_user(uid)


@router.get("/users")
async def list_users(current=Depends(get_current_poc)):
    require_manager_role(current)
    return await crm.list_users(current)


@router.patch("/users/{user_id}")
async def update_user(user_id: int, payload: dict, current=Depends(get_current_poc)):
    """Admin: any user. Main Manager: only logins they created (name, phone,
    email, active, password, departments), never roles or WhatsApp links."""
    target = await crm.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if current["role"] != "admin":
        row = await crm.find_user_by_username(target["username"])
        if current["role"] != "main_manager" or not row or row.get("created_by") != current["id"]:
            raise HTTPException(status_code=403, detail="You can only manage logins you created")
        payload = {k: v for k, v in payload.items() if k in ("name", "email", "phone", "active", "password", "department_ids")}
    fields = {k: payload[k] for k in ("name", "email", "phone", "role", "active") if k in payload}
    if "role" in fields and fields["role"] not in crm.ROLES:
        raise HTTPException(status_code=400, detail="invalid role")
    if payload.get("password"):
        if len(payload["password"]) < 8:
            raise HTTPException(status_code=400, detail="password must be 8+ chars")
        fields["password_hash"] = hash_password(payload["password"])
    old, new = audit.changes(target, {k: v for k, v in fields.items() if k != "password_hash"})
    if "password_hash" in fields:
        new["password"] = "***"; old["password"] = "***"
    await crm.update_user(user_id, fields)
    if "role" in new:
        await notification_service.notify(user_id, "role_changed", f"Your role is now {new['role']}", title="Your role was changed")
        await notification_service.notify_event("role_changed", f"Role changed for {target['name']}", f"{old['role']} -> {new['role']}", actor_id=current["id"],
                                                entity_type="user", entity_id=user_id)
    if new:
        await audit.record(current["id"], "role_changed" if "role" in new else "user_updated", "user", user_id, before=old, after=new)
    if "department_ids" in payload:
        await crm.set_user_departments(user_id, [int(x) for x in payload["department_ids"]])
    if "account_id" in payload and current["role"] == "admin":
        if payload["account_id"]:
            await crm.map_account_user(payload["account_id"], user_id)
        else:
            acc = await crm.get_user_account(user_id)
            if acc:
                await crm.unmap_account(acc["account_id"])
        await audit.record(current["id"], "whatsapp_account_linked", "user", user_id, str(payload["account_id"]))
    return await crm.get_user(user_id)


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, current=Depends(get_current_poc)):
    """Soft delete: the login is switched off and hidden, but tasks, updates and audit entries still show who they were."""
    permissions.require(current, "user.delete")
    target = await crm.get_user(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if user_id == current["id"]:
        raise HTTPException(status_code=400, detail="You cannot delete your own account")
    if target["role"] == "admin" and len([u for u in await crm.list_users() if u["role"] == "admin"]) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the last admin")
    await crm.soft_delete("user", user_id)
    acc = await crm.get_user_account(user_id)
    if acc:
        await crm.unmap_account(acc["account_id"])      # their number no longer sends or routes anything
    await audit.record(current["id"], "user_deleted", "user", user_id, before={"name": target["name"], "role": target["role"]}, after={"deleted": True})
    return {"ok": True}


@router.post("/users/{user_id}/restore")
async def restore_user(user_id: int, current=Depends(get_current_poc)):
    permissions.require(current, "user.restore")
    if not await crm.get_user(user_id, include_deleted=True):
        raise HTTPException(status_code=404, detail="User not found")
    await crm.restore("user", user_id)
    await crm.update_user(user_id, {"active": 1})
    await audit.record(current["id"], "user_restored", "user", user_id)
    return await crm.get_user(user_id)
