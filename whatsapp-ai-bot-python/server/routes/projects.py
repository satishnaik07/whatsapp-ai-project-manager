import re
import secrets

from fastapi import APIRouter, Depends, HTTPException

from server import permissions
from server.auth import hash_password

from server import group_db
from server.repositories import crm
from server.routes.deps import dept_scope, get_current_poc, load_project, member_only, need, require_admin
from server.services import audit, notification_service, poc_router

router = APIRouter()

PROJECT_FIELDS = ("name", "description", "status", "priority", "start_date", "target_date",
                  "main_manager_id", "routing_threshold", "client_id")


async def _need_user(user_id, label: str) -> dict:
    u = await crm.get_user(int(user_id)) if user_id is not None else None
    if not u or not u["active"]:
        raise HTTPException(status_code=404, detail=f"{label} not found or inactive")
    return u


async def _assign_department(project_id: int, dept_id: int, poc_user_id, member_ids, actor, poc_account_id=None):
    dept = await crm.get_department(int(dept_id))
    if not dept:
        raise HTTPException(status_code=404, detail="Department not found")
    project = await crm.get_project(project_id)
    if not crm.department_usable_by(dept, project["main_manager_id"]):
        raise HTTPException(status_code=400, detail="This department belongs to another Main Manager")
    await crm.add_project_department(project_id, int(dept_id))
    await audit.record(actor["id"], "department_assigned", "project", project_id, f"department={dept_id}")
    if poc_user_id:
        await _need_user(poc_user_id, "POC")
        prev = next((d for d in await crm.list_project_departments(project_id) if d["department_id"] == int(dept_id)), None)
        await crm.set_department_poc(project_id, int(dept_id), int(poc_user_id))
        if prev and prev["poc_user_id"] and prev["poc_user_id"] != int(poc_user_id):
            await audit.record(actor["id"], "poc_changed", "project", project_id, before={"poc_user_id": prev["poc_user_id"]},
                               after={"poc_user_id": int(poc_user_id)}, project_id=project_id, department_id=int(dept_id))
            await notification_service.notify(prev["poc_user_id"], "assignment_changed", "You are no longer the POC", project_id, title="POC changed")
        await notification_service.notify(int(poc_user_id), "assigned", "You are now the POC", project_id, title="You were assigned as POC",
                                          entity_type="project", entity_id=project_id)
        await crm.add_department_member(project_id, int(dept_id), int(poc_user_id))  # POC is also a member
        await _ensure_user_in_department(int(poc_user_id), int(dept_id))
        await audit.record(actor["id"], "poc_assigned", "project", project_id, f"department={dept_id} poc={poc_user_id}")
        if poc_account_id:
            require_admin(actor)
            await crm.map_account_user(poc_account_id, int(poc_user_id))
            await audit.record(actor["id"], "whatsapp_account_linked", "user", poc_user_id, poc_account_id)
    for m in member_ids or []:
        await _need_user(m, "Member")
        await crm.add_department_member(project_id, int(dept_id), int(m))
        await _ensure_user_in_department(int(m), int(dept_id))


async def _ensure_user_in_department(user_id: int, dept_id: int):
    from server.db import execute
    await execute("INSERT IGNORE INTO user_departments (user_id, department_id) VALUES (%s,%s)", (user_id, dept_id))


@router.post("/projects")
async def create_project(payload: dict, current=Depends(get_current_poc)):
    """Project wizard in one call: client, group, manager, departments, POCs,
    members, AI mode, activation. Every part is optional except name + client
    + Main Manager; the same pieces also have their own endpoints below."""
    require_admin(current)   # the admin creates projects and hands them to a Main Manager
    name = need(payload.get("name"), "name")

    client_id = payload.get("client_id")
    if client_id:
        if not await crm.get_client(int(client_id)):
            raise HTTPException(status_code=404, detail="Client not found")
    elif payload.get("client"):
        client_id = await crm.create_client(need(payload["client"].get("name"), "client.name"),
                                            payload["client"].get("company_name"), payload["client"].get("phone"),
                                            payload["client"].get("email"), payload["client"].get("notes"),
                                            main_manager_id=payload.get("main_manager_id"))
        await audit.record(current["id"], "client_created", "client", client_id)
    else:
        raise HTTPException(status_code=400, detail="client_id or client{} is required")

    manager_id = payload.get("main_manager_id")
    if not manager_id and client_id:
        manager_id = (await crm.get_client(int(client_id)) or {}).get("main_manager_id")
    if not manager_id:
        raise HTTPException(status_code=400, detail="No Main Manager configured: main_manager_id is required")
    mgr_user = await _need_user(manager_id, "Main Manager")
    if mgr_user["role"] not in ("main_manager", "admin"):
        raise HTTPException(status_code=400, detail="main_manager_id must be a Main Manager user")

    group_id = payload.get("group_id")
    if group_id:
        g = await group_db.get_group(int(group_id))
        if not g:
            raise HTTPException(status_code=404, detail="Group not found")
        await _same_client_or_409(int(group_id), int(client_id))
    elif payload.get("group"):
        from server.group_routes import create_group as legacy_create_group
        gp = dict(payload["group"])
        gp.setdefault("project_name", name)
        gp.setdefault("ai_mode", payload.get("ai_mode", "approval"))
        gp["assigned_poc_id"] = manager_id  # keeps the legacy access check working
        # Authorization was already enforced above (admin / main manager).
        group = await legacy_create_group(gp, current={**current, "role": "admin"})
        group_id = group["id"]
        await audit.record(current["id"], "group_configured", "group", group_id)
    # (a project without a group is allowed; it just cannot receive messages yet)

    status = "active" if payload.get("activate") else payload.get("status", "planning")
    pid = await crm.create_project(int(client_id), name, payload.get("description"), status, payload.get("priority", "medium"),
                                   payload.get("start_date"), payload.get("target_date"), int(manager_id),
                                   int(group_id) if group_id else None)
    await audit.record(current["id"], "project_created", "project", pid, before={}, after={"name": name, "client_id": int(client_id), "main_manager_id": int(manager_id)}, project_id=pid)
    await notification_service.notify_event("project_created", f"New project: {name}", actor_id=current["id"], project_id=pid,
                                            manager_id=int(manager_id), entity_type="project", entity_id=pid)
    if group_id:
        await group_db.update_group(int(group_id), assigned_poc_id=int(manager_id))
        if payload.get("ai_mode"):
            await group_db.update_group(int(group_id), ai_mode=payload["ai_mode"])
    for d in payload.get("departments", []):
        await _assign_department(pid, d["department_id"], d.get("poc_user_id"), d.get("member_ids"), current, d.get("poc_account_id"))
    return await crm.get_project(pid)


@router.get("/projects")
async def list_projects(current=Depends(get_current_poc)):
    return await crm.list_projects(current)


@router.get("/projects/{project_id}")
async def project_details(project_id: int, current=Depends(get_current_poc)):
    project = await load_project(project_id, current)
    depts = await crm.list_project_departments(project_id)
    for d in depts:
        d["poc_problem"] = poc_router.poc_problem(d)
    tasks = await crm.list_tasks(project_id=project_id)
    progress = {}
    for t in tasks:
        if t["status"] == "cancelled":
            continue
        k = t["department_id"]
        a = progress.setdefault(k, {"department_id": k, "name": t["department_name"] or "Unassigned", "total": 0, "completed": 0})
        a["total"] += 1
        a["completed"] += 1 if t["status"] == "completed" else 0
    for a in progress.values():
        a["progress_pct"] = round(100 * a["completed"] / a["total"]) if a["total"] else 0
    group_members = await group_db.list_members(project["group_id"]) if project.get("group_id") else []
    conversations = await crm.list_project_threads(project["group_id"]) if project.get("group_id") else []
    updates = await crm.list_updates(project_id=project_id, limit=20)
    escalations = await crm.list_escalations([project_id])
    scope = await dept_scope(current, project_id)
    if await member_only(current, project_id):
        # A member sees only the project name and their own tasks and notes.
        mine = [t for t in tasks if t["assigned_user_id"] == current["id"]]
        return {"project": {k: project.get(k) for k in ("id", "name", "client_name", "status", "priority", "target_date")},
                "departments": [{"department_id": d["department_id"], "name": d["name"], "poc_name": d["poc_name"], "members": []}
                                for d in depts if d["department_id"] in scope],
                "tasks": mine, "progress": [], "conversations": [], "client_members": [], "group_members": [],
                "escalations": [], "activity_log": [],
                "updates": await crm.list_updates(project_id=project_id, limit=20, author_id=current["id"])}
    if scope is not None:  # POC: only their own department(s)
        depts = [d for d in depts if d["department_id"] in scope]
        tasks = [t for t in tasks if t["department_id"] in scope or t["assigned_user_id"] == current["id"]]
        progress = {k: v for k, v in progress.items() if k in scope}
        updates = [u for u in updates if u["department_id"] in scope]
        conversations = [c for c in conversations if c["current_department_id"] in scope or c["assigned_poc_id"] == current["id"]]
        escalations = []
    return {
        "project": project,
        "departments": depts,
        "tasks": tasks,
        "progress": list(progress.values()),
        "updates": updates,
        "conversations": conversations,
        "client_members": [m for m in group_members if m["role"] == "client"],
        "group_members": group_members,
        "escalations": escalations,
        "activity_log": await crm.list_audit(30, project_id=project_id) if scope is None else [],
    }


@router.patch("/projects/{project_id}")
async def update_project(project_id: int, payload: dict, current=Depends(get_current_poc)):
    permissions.require(current, "project.edit")
    before = await load_project(project_id, current, manage=True)
    fields = {k: payload[k] for k in PROJECT_FIELDS if k in payload}
    if "name" in fields and not str(fields["name"]).strip():
        raise HTTPException(status_code=400, detail="name cannot be empty")
    if current["role"] != "admin":     # a Main Manager runs the project but cannot move it to another client or manager
        for k in ("client_id", "main_manager_id"):
            fields.pop(k, None)
    if "main_manager_id" in fields:
        await _need_user(fields["main_manager_id"], "Main Manager")
    old, new = audit.changes(before, fields)
    await crm.update_project(project_id, fields)
    p = await crm.get_project(project_id)
    if p.get("group_id"):
        if "main_manager_id" in fields:
            await group_db.update_group(p["group_id"], assigned_poc_id=fields["main_manager_id"])
        for k in ("ai_mode", "ai_enabled"):
            if k in payload:
                await group_db.update_group(p["group_id"], **{k: payload[k]})
    if new:
        await audit.record(current["id"], "project_updated", "project", project_id, before=old, after=new, project_id=project_id)
        await notification_service.notify_event("project_updated", f"Project edited: {before['name']}", ", ".join(new), actor_id=current["id"],
                                                project_id=project_id, entity_type="project", entity_id=project_id)
    return p


@router.delete("/projects/{project_id}")
async def delete_project(project_id: int, current=Depends(get_current_poc)):
    """Soft delete. Messages, tasks, updates and audit remain; the project stops receiving routed messages."""
    permissions.require(current, "project.delete")
    project = await crm.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    await crm.soft_delete("project", project_id)
    await audit.record(current["id"], "project_deleted", "project", project_id, before={"name": project["name"], "status": project["status"]},
                       after={"deleted": True}, project_id=project_id)
    await notification_service.notify_event("project_deleted", f"Project removed: {project['name']}", actor_id=current["id"],
                                            manager_id=project["main_manager_id"], entity_type="project", entity_id=project_id)
    return {"ok": True}


@router.post("/projects/{project_id}/restore")
async def restore_project(project_id: int, current=Depends(get_current_poc)):
    permissions.require(current, "project.restore")
    project = await crm.get_project(project_id, include_deleted=True)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not await crm.get_client(project["client_id"]):
        raise HTTPException(status_code=409, detail="Restore the client first")
    await crm.restore("project", project_id)
    await audit.record(current["id"], "project_restored", "project", project_id, project_id=project_id)
    return await crm.get_project(project_id)


@router.post("/projects/{project_id}/departments")
async def add_department(project_id: int, payload: dict, current=Depends(get_current_poc)):
    await load_project(project_id, current, manage=True)
    await _assign_department(project_id, need(payload.get("department_id"), "department_id"),
                             payload.get("poc_user_id"), payload.get("member_ids"), current, payload.get("poc_account_id"))
    return await crm.list_project_departments(project_id)


@router.delete("/projects/{project_id}/departments/{dept_id}")
async def remove_department(project_id: int, dept_id: int, current=Depends(get_current_poc)):
    await load_project(project_id, current, manage=True)
    await crm.remove_project_department(project_id, dept_id)
    await audit.record(current["id"], "department_removed", "project", project_id, f"department={dept_id}")
    return {"ok": True}


@router.post("/projects/{project_id}/departments/{dept_id}/poc")
async def assign_poc(project_id: int, dept_id: int, payload: dict, current=Depends(get_current_poc)):
    await load_project(project_id, current, manage=True)
    await _assign_department(project_id, dept_id, need(payload.get("user_id"), "user_id"), None, current, payload.get("account_id"))
    return await crm.list_project_departments(project_id)


@router.post("/projects/{project_id}/departments/{dept_id}/members")
async def add_member(project_id: int, dept_id: int, payload: dict, current=Depends(get_current_poc)):
    await load_project(project_id, current, manage=True)
    await _assign_department(project_id, dept_id, None, [need(payload.get("user_id"), "user_id")], current)
    return await crm.list_project_departments(project_id)


@router.delete("/projects/{project_id}/departments/{dept_id}/members/{user_id}")
async def remove_member(project_id: int, dept_id: int, user_id: int, current=Depends(get_current_poc)):
    await load_project(project_id, current, manage=True)
    await crm.remove_department_member(project_id, dept_id, user_id)
    return {"ok": True}


# ------------------------------------------------ group + team (Main Manager)
async def _same_client_or_409(group_id: int, client_id: int):
    """One WhatsApp group may serve many projects, but only of ONE client: otherwise a client would see
    (and the AI would read) another client's conversation."""
    for p in await crm.list_projects_by_group(group_id):
        if p["client_id"] != client_id:
            raise HTTPException(status_code=409, detail=f"This WhatsApp group already belongs to client '{p['client_name']}'. "
                                                        "One group cannot serve two different clients.")


@router.post("/projects/{project_id}/group")
async def attach_group(project_id: int, payload: dict, current=Depends(get_current_poc)):
    """Main Manager links a WhatsApp group (seen through THEIR linked account) to one of their projects."""
    project = await load_project(project_id, current, manage=True)
    if project.get("group_id"):
        raise HTTPException(status_code=409, detail="This project already has a WhatsApp group (unlink it first to change it)")
    manager_id = project["main_manager_id"]
    own = await crm.get_user_account(manager_id) if manager_id else None
    account_id = payload.get("account_id") if current["role"] == "admin" and payload.get("account_id") else (own or {}).get("account_id")
    if not account_id:
        raise HTTPException(status_code=400, detail="Link your WhatsApp account first (My WhatsApp)")
    jid = need(payload.get("whatsapp_group_id"), "whatsapp_group_id")
    existing = await group_db.get_group_by_whatsapp_id(account_id, jid)
    if existing:
        # The physical group is already configured (for another project of this client): reuse it. The unique rule
        # (account, group) stays: there is one config row per real group; many projects may point at it.
        await _same_client_or_409(existing["id"], project["client_id"])
        have = {m["member_jid"] for m in await group_db.list_members(existing["id"])}
        for m in payload.get("members", []):
            if m.get("jid") and m["jid"] not in have and m.get("role") in ("client", "internal"):
                await group_db.upsert_member(existing["id"], m["jid"], m.get("name"), m["role"], m.get("phone"))
        group = existing
        reused = True
    else:
        from server.group_routes import create_group as legacy_create_group
        gp = {"account_id": account_id, "whatsapp_group_id": jid, "group_name": payload.get("group_name"), "project_name": project["name"],
              "members": payload.get("members", []), "ai_mode": payload.get("ai_mode", "approval"), "assigned_poc_id": manager_id}
        group = await legacy_create_group(gp, current={**current, "role": "admin"})   # authorization checked above
        reused = False
    await crm.update_project(project_id, {"group_id": group["id"]})
    await audit.record(current["id"], "group_mapped", "project", project_id, f"group={group.get('group_name')} reused={reused}",
                       before={"group_id": None}, after={"group_id": group["id"]}, project_id=project_id)
    await notification_service.notify_event("group_mapped", f"WhatsApp group mapped to {project['name']}", group.get("group_name") or "",
                                            actor_id=current["id"], project_id=project_id, entity_type="project", entity_id=project_id)
    out = await crm.get_project(project_id)
    out["group_reused"] = reused
    return out


@router.delete("/projects/{project_id}/group")
async def unlink_group(project_id: int, current=Depends(get_current_poc)):
    """Removes the group-to-project mapping. The group, its members and ALL its history stay (other projects may use it)."""
    project = await load_project(project_id, current, manage=True)
    if not project.get("group_id"):
        raise HTTPException(status_code=404, detail="This project has no WhatsApp group")
    await crm.update_project(project_id, {"group_id": None})
    await audit.record(current["id"], "group_unmapped", "project", project_id, before={"group_id": project["group_id"]},
                       after={"group_id": None}, project_id=project_id)
    await notification_service.notify_event("group_unmapped", f"WhatsApp group unlinked from {project['name']}", actor_id=current["id"],
                                            project_id=project_id, entity_type="project", entity_id=project_id)
    return {"ok": True}


@router.get("/group-mappings")
async def group_mappings(current=Depends(get_current_poc)):
    """Which projects and departments (with POC and members) each WhatsApp group serves."""
    if current["role"] not in crm.MANAGER_ROLES:
        raise HTTPException(status_code=403, detail="Only admins and Main Managers can view group mappings")
    out: dict = {}
    for p in await crm.list_projects(current):
        if not p.get("group_id"):
            continue
        g = out.setdefault(p["group_id"], {"group_id": p["group_id"], "group_name": p["group_name"], "whatsapp_group_id": p["whatsapp_group_id"], "projects": []})
        g["projects"].append({"project_id": p["id"], "project": p["name"], "client": p["client_name"],
                              "departments": [{"department": d["name"], "poc": d["poc_name"], "members": [m["name"] for m in d["members"]]}
                                              for d in await crm.list_project_departments(p["id"])]})
    return list(out.values())


@router.get("/projects/{project_id}/roster")
async def roster(project_id: int, current=Depends(get_current_poc)):
    """The group's people with who they are in this project (client / team / unassigned)."""
    project = await load_project(project_id, current, manage=True)
    if not project.get("group_id"):
        return []
    depts = await crm.list_project_departments(project_id)
    by_user: dict = {}
    for d in depts:
        for m in d["members"]:
            by_user.setdefault(m["user_id"], {"department_id": d["department_id"], "department": d["name"], "is_poc": False, "poc_name": d["poc_name"]})
        if d["poc_user_id"]:
            by_user[d["poc_user_id"]] = {"department_id": d["department_id"], "department": d["name"], "is_poc": True, "poc_name": d["poc_name"]}
    out = []
    for m in await group_db.list_members(project["group_id"]):
        phone = m.get("member_phone") or m["member_jid"].split("@")[0]
        user = await crm.find_user_by_phone(phone)
        a = by_user.get(user["id"]) if user else None
        out.append({"jid": m["member_jid"], "name": m.get("member_name") or phone, "phone": phone, "role": m["role"],
                    "user_id": user["id"] if user else None, "username": user["username"] if user else None,
                    "department_id": a["department_id"] if a else None, "department": a["department"] if a else None,
                    "is_poc": bool(a and a["is_poc"]), "poc_name": a["poc_name"] if a else None})
    return out


def _slug(name: str, phone: str) -> str:
    base = re.sub(r"[^a-z0-9]", "", (name or "").lower())[:14] or "user"
    return base


async def _unique_username(base: str, phone: str) -> str:
    cand = base
    for suffix in ("", phone[-3:], phone[-4:], secrets.token_hex(2)):
        cand = base + suffix
        if not await crm.find_user_by_username(cand):
            return cand
    return base + secrets.token_hex(3)


@router.post("/projects/{project_id}/team")
async def add_team(project_id: int, payload: dict, current=Depends(get_current_poc)):
    """Assign people from the WhatsApp group to departments as POC or member and create their logins.
    people: [{jid, name, phone, department_id, is_poc, username?, password?}]
    A number that already has a login is reused (nothing is overwritten). New passwords are returned ONCE."""
    project = await load_project(project_id, current, manage=True)
    people = payload.get("people") or []
    if not people:
        raise HTTPException(status_code=400, detail="people is required")
    manager_id = project["main_manager_id"]
    seen_poc: set = set()
    cache: dict = {}
    for p in people:
        did = int(need(p.get("department_id"), "department_id"))
        if did not in cache:
            cache[did] = await crm.get_department(did)
        if not cache[did]:
            raise HTTPException(status_code=404, detail="Department not found")
        if not crm.department_usable_by(cache[did], manager_id):
            raise HTTPException(status_code=400, detail="This department belongs to another Main Manager")
        if len(crm.digits(p.get("phone") or (p.get("jid") or "").split("@")[0])) < 7:
            raise HTTPException(status_code=400, detail=f"A valid phone number is required for {p.get('name') or 'this person'}")
        # WhatsApp often gives no display name for a member: use the number instead of failing
        p["name"] = (p.get("name") or "").strip() or crm.digits(p.get("phone") or (p.get("jid") or "").split("@")[0])
        if p.get("is_poc"):
            if did in seen_poc:
                raise HTTPException(status_code=400, detail=f"Only one POC per department ({cache[did]['name']})")
            seen_poc.add(did)
        if p.get("password") and len(p["password"]) < 8:
            raise HTTPException(status_code=400, detail="Passwords must be at least 8 characters")

    group = await group_db.get_group(project["group_id"]) if project.get("group_id") else None
    results = []
    for p in people:
        did = int(p["department_id"])
        phone = crm.digits(p.get("phone") or p["jid"].split("@")[0])
        want_poc = bool(p.get("is_poc"))
        user = await crm.find_user_by_phone(phone)
        created, password = False, None
        if user and user["role"] in ("admin", "main_manager"):
            raise HTTPException(status_code=400, detail=f"{p['name']} ({phone}) already has a manager/admin login")
        if not user:
            username = (p.get("username") or "").strip() or await _unique_username(_slug(p["name"], phone), phone)
            if await crm.find_user_by_username(username):
                raise HTTPException(status_code=409, detail=f"Username '{username}' is already taken")
            password = p.get("password") or secrets.token_urlsafe(8)
            uid = await crm.create_user(username, hash_password(password), p["name"],
                                        "department_poc" if want_poc else "department_member", None, phone, current["id"])
            user, created = await crm.get_user(uid), True
        elif want_poc and user["role"] == "department_member":
            await crm.update_user(user["id"], {"role": "department_poc"})
            user["role"] = "department_poc"
        await _ensure_user_in_department(user["id"], did)
        await crm.add_project_department(project_id, did)
        if want_poc:
            await crm.set_department_poc(project_id, did, user["id"])
            await audit.record(current["id"], "poc_assigned", "project", project_id, f"department={did} poc={user['id']}")
        await crm.add_department_member(project_id, did, user["id"])
        if group:
            await group_db.upsert_member(group["id"], p.get("jid") or f"{phone}@s.whatsapp.net", p["name"], "internal", phone)
        await audit.record(current["id"], "team_member_added", "project", project_id, f"user={user['id']} department={did} poc={want_poc}")
        await notification_service.notify(user["id"], "assigned", f"You were added to {project['name']} ({cache[did]['name']})", project_id)
        results.append({"name": p["name"], "phone": phone, "department": cache[did]["name"], "role": "POC" if want_poc else "Member",
                        "username": user["username"], "password": password, "created": created,
                        "note": "new login" if created else "existing login reused"})
    return {"results": results, "login_url": "/pm/"}
