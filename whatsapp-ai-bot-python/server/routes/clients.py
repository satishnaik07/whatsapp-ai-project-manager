from fastapi import APIRouter, Depends, HTTPException

from server import permissions
from server.repositories import crm
from server.routes.deps import get_current_poc, need
from server.services import audit, notification_service

router = APIRouter()
EDITABLE = ("name", "company_name", "phone", "email", "notes", "status", "main_manager_id")


async def _check_manager(mgr):
    if mgr:
        u = await crm.get_user(int(mgr))
        if not u or u["role"] not in ("main_manager", "admin"):
            raise HTTPException(status_code=400, detail="main_manager_id must be a Main Manager user")


@router.post("/clients")
async def create_client(payload: dict, current=Depends(get_current_poc)):
    permissions.require(current, "client.create")   # the admin onboards clients and hands them to a Main Manager
    mgr = payload.get("main_manager_id")
    await _check_manager(mgr)
    cid = await crm.create_client(need(payload.get("name"), "name"), payload.get("company_name"), payload.get("phone"),
                                  payload.get("email"), payload.get("notes"), payload.get("status", "active"), mgr)
    row = await crm.get_client(cid)
    await audit.record(current["id"], "client_created", "client", cid, before={}, after={k: row.get(k) for k in EDITABLE})
    await notification_service.notify_event("client_created", f"New client: {row['name']}", actor_id=current["id"], manager_id=mgr,
                                            entity_type="client", entity_id=cid)
    return row


@router.get("/clients")
async def list_clients(q: str | None = None, current=Depends(get_current_poc)):
    permissions.require(current, "client.view")
    if current["role"] == "admin":
        rows = await crm.list_clients()
    else:
        projects = await crm.list_projects(current)
        ids = {p["client_id"] for p in projects}
        if current["role"] == "main_manager":
            ids |= {c["id"] for c in await crm.list_clients() if c.get("main_manager_id") == current["id"]}
        rows = await crm.list_clients(sorted(ids))
    if q:
        ql = q.lower()
        rows = [r for r in rows if ql in f"{r['name']} {r.get('company_name') or ''} {r.get('email') or ''}".lower()]
    return rows


@router.get("/clients/{client_id}")
async def get_client(client_id: int, current=Depends(get_current_poc)):
    permissions.require(current, "client.view")
    client = await crm.get_client(client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    projects = [p for p in await crm.list_projects(current) if p["client_id"] == client_id]
    if current["role"] != "admin" and not projects and client.get("main_manager_id") != current["id"]:
        raise HTTPException(status_code=403, detail="Not authorized for this client")
    client["projects"] = projects
    return client


@router.patch("/clients/{client_id}")
async def update_client(client_id: int, payload: dict, current=Depends(get_current_poc)):
    permissions.require(current, "client.edit")
    before = await crm.get_client(client_id)
    if not before:
        raise HTTPException(status_code=404, detail="Client not found")
    fields = {k: payload[k] for k in EDITABLE if k in payload}
    if "name" in fields and not str(fields["name"]).strip():
        raise HTTPException(status_code=400, detail="name cannot be empty")
    await _check_manager(fields.get("main_manager_id"))
    old, new = audit.changes(before, fields)
    await crm.update_client(client_id, fields)
    if new:
        await audit.record(current["id"], "client_updated", "client", client_id, before=old, after=new)
        await notification_service.notify_event("client_updated", f"Client edited: {before['name']}", ", ".join(new), actor_id=current["id"],
                                                manager_id=fields.get("main_manager_id") or before.get("main_manager_id"),
                                                entity_type="client", entity_id=client_id)
    return await crm.get_client(client_id)


@router.delete("/clients/{client_id}")
async def delete_client(client_id: int, current=Depends(get_current_poc)):
    """Soft delete: the client and its projects disappear from lists; conversations, tasks and audit stay in the database."""
    permissions.require(current, "client.delete")
    client = await crm.get_client(client_id)
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    projects = [p for p in await crm.list_projects(include_deleted=False) if p["client_id"] == client_id]
    for p in projects:
        await crm.soft_delete("project", p["id"])
    await crm.soft_delete("client", client_id)
    await audit.record(current["id"], "client_deleted", "client", client_id, f"{len(projects)} project(s) hidden too",
                       before={"name": client["name"], "status": client["status"]}, after={"deleted": True})
    await notification_service.notify_event("client_deleted", f"Client removed: {client['name']}", actor_id=current["id"],
                                            manager_id=client.get("main_manager_id"), entity_type="client", entity_id=client_id)
    return {"ok": True, "projects_hidden": len(projects)}


@router.post("/clients/{client_id}/restore")
async def restore_client(client_id: int, payload: dict | None = None, current=Depends(get_current_poc)):
    permissions.require(current, "client.restore")
    if not await crm.get_client(client_id, include_deleted=True):
        raise HTTPException(status_code=404, detail="Client not found")
    await crm.restore("client", client_id)
    if (payload or {}).get("with_projects"):
        for p in await crm.list_projects(include_deleted=True):
            if p["client_id"] == client_id and p.get("deleted_at"):
                await crm.restore("project", p["id"])
    await audit.record(current["id"], "client_restored", "client", client_id)
    return await crm.get_client(client_id)
