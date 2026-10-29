"""
CRM data access: users, departments, WhatsApp-account mapping, clients,
projects, project departments/members, tasks, updates, routing log,
escalations, audit log, notifications, dashboards and project access checks.

Everything is plain SQL over server.db (same pool as the rest of the app).
`poc_users` is the users table (see server/crm_schema.py for why).
"""
from datetime import date

from server.db import query, execute

ROLES = ("admin", "main_manager", "department_poc", "department_member")
LEGACY_ROLE = "poc"  # pre-CRM role; treated as department_poc
MANAGER_ROLES = ("admin", "main_manager")


def effective_role(role: str) -> str:
    return "department_poc" if role == LEGACY_ROLE else role


def _sets(fields: dict, allowed: tuple):
    cols, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            cols.append(f"`{k}` = %s")
            vals.append(v)
    return cols, vals


# ---------------------------------------------------------------- users
USER_COLS = "id, username, name, email, phone, role, active, created_at"


async def list_users(viewer: dict | None = None):
    """Admin (or viewer=None): everyone. Main Manager: themselves, logins they
    created, and people in their projects. Anyone else: nobody."""
    users = await query(f"SELECT {USER_COLS}, created_by FROM poc_users WHERE deleted_at IS NULL ORDER BY id")
    if viewer is not None and viewer["role"] != "admin":
        if viewer["role"] != "main_manager":
            return []
        pids = await accessible_project_ids(viewer)
        mine = {viewer["id"]}
        if pids:
            ph = ",".join(["%s"] * len(pids))
            for r in await query(f"SELECT poc_user_id AS u FROM project_departments WHERE project_id IN ({ph}) AND poc_user_id IS NOT NULL", tuple(pids)):
                mine.add(r["u"])
            for r in await query(f"SELECT user_id AS u FROM project_department_members WHERE project_id IN ({ph})", tuple(pids)):
                mine.add(r["u"])
        users = [u for u in users if u["id"] in mine or u.get("created_by") == viewer["id"]]
    deps = await query(
        "SELECT ud.user_id, d.id, d.name FROM user_departments ud JOIN departments d ON d.id = ud.department_id"
    )
    maps = await query("SELECT w.user_id, w.account_id, a.status, a.phone_number FROM whatsapp_account_users w JOIN accounts a ON a.id = w.account_id")
    for u in users:
        u["departments"] = [{"id": d["id"], "name": d["name"]} for d in deps if d["user_id"] == u["id"]]
        m = next((x for x in maps if x["user_id"] == u["id"]), None)
        u["whatsapp_account"] = m
    return users


async def get_user(user_id: int, include_deleted: bool = False):
    rows = await query(f"SELECT {USER_COLS} FROM poc_users WHERE id = %s" + ("" if include_deleted else " AND deleted_at IS NULL"), (user_id,))
    return rows[0] if rows else None


async def create_user(username, password_hash, name, role, email=None, phone=None, created_by=None) -> int:
    return await execute(
        "INSERT INTO poc_users (username, password_hash, name, role, email, phone, created_by) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (username, password_hash, name, role, email, phone, created_by),
    )


def digits(value) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


async def find_user_by_phone(phone) -> dict | None:
    d = digits(phone)
    if len(d) < 7:
        return None
    for u in await query(f"SELECT {USER_COLS}, created_by FROM poc_users WHERE phone IS NOT NULL"):
        ud = digits(u["phone"])
        if ud and (ud == d or ud.endswith(d[-10:]) and d.endswith(ud[-10:])):
            return u
    return None


async def find_user_by_username(username: str) -> dict | None:
    rows = await query(f"SELECT {USER_COLS}, created_by FROM poc_users WHERE username = %s", (username,))
    return rows[0] if rows else None


async def update_user(user_id: int, fields: dict):
    cols, vals = _sets(fields, ("name", "email", "phone", "role", "active", "password_hash"))
    if cols:
        await execute(f"UPDATE poc_users SET {', '.join(cols)} WHERE id = %s", (*vals, user_id))


async def set_user_departments(user_id: int, department_ids: list[int]):
    await execute("DELETE FROM user_departments WHERE user_id = %s", (user_id,))
    for d in dict.fromkeys(department_ids):
        await execute("INSERT INTO user_departments (user_id, department_id) VALUES (%s,%s)", (user_id, d))


# ---------------------------------------------------------- departments
async def create_department(name: str, description: str | None, owner_user_id: int | None = None) -> int:
    return await execute("INSERT INTO departments (name, description, owner_user_id) VALUES (%s,%s,%s)",
                         (name, description, owner_user_id))


async def department_name_taken(name: str, owner_user_id: int | None, exclude_id: int | None = None) -> bool:
    rows = await query("SELECT id FROM departments WHERE LOWER(name) = LOWER(%s) AND deleted_at IS NULL AND "
                       "(owner_user_id <=> %s)", (name, owner_user_id))
    return any(r["id"] != exclude_id for r in rows)


async def list_departments(active_only: bool = False, viewer: dict | None = None):
    """Admin / no viewer: all. Main Manager: their own plus shared ones. POC / member: only departments they belong to."""
    sql = "SELECT d.*, u.name AS owner_name FROM departments d LEFT JOIN poc_users u ON u.id = d.owner_user_id WHERE d.deleted_at IS NULL"
    params: list = []
    if active_only:
        sql += " AND d.active = 1"
    if viewer is not None and viewer["role"] != "admin":
        if viewer["role"] == "main_manager":
            sql += " AND (d.owner_user_id IS NULL OR d.owner_user_id = %s)"
            params.append(viewer["id"])
        else:
            sql += " AND d.id IN (SELECT department_id FROM user_departments WHERE user_id = %s)"
            params.append(viewer["id"])
    return await query(sql + " ORDER BY d.name", tuple(params))


async def get_department(dept_id: int, include_deleted: bool = False):
    rows = await query("SELECT * FROM departments WHERE id = %s" + ("" if include_deleted else " AND deleted_at IS NULL"), (dept_id,))
    return rows[0] if rows else None


async def update_department(dept_id: int, fields: dict):
    cols, vals = _sets(fields, ("name", "description", "active"))
    if cols:
        await execute(f"UPDATE departments SET {', '.join(cols)} WHERE id = %s", (*vals, dept_id))


def department_usable_by(dept: dict, manager_id: int | None) -> bool:
    """A project's manager may use shared departments and their own, never another manager's."""
    return dept["owner_user_id"] is None or dept["owner_user_id"] == manager_id


# ---------------------------------------- WhatsApp account <-> user map
async def map_account_user(account_id: str, user_id: int):
    """One account per user and one user per account: any previous mapping
    of either side is replaced."""
    await execute("DELETE FROM whatsapp_account_users WHERE account_id = %s OR user_id = %s", (account_id, user_id))
    await execute("INSERT INTO whatsapp_account_users (account_id, user_id) VALUES (%s,%s)", (account_id, user_id))


async def unmap_account(account_id: str):
    await execute("DELETE FROM whatsapp_account_users WHERE account_id = %s", (account_id,))


async def get_user_account(user_id: int):
    """The user's linked WhatsApp account with live status, or None."""
    rows = await query(
        "SELECT a.id AS account_id, a.status, a.phone_number FROM whatsapp_account_users w "
        "JOIN accounts a ON a.id = w.account_id WHERE w.user_id = %s",
        (user_id,),
    )
    return rows[0] if rows else None


async def list_accounts_with_users():
    return await query(
        """SELECT a.id, a.name, a.phone_number, a.status, w.user_id, u.name AS user_name, u.role AS user_role
           FROM accounts a LEFT JOIN whatsapp_account_users w ON w.account_id = a.id
           LEFT JOIN poc_users u ON u.id = w.user_id ORDER BY a.created_at"""
    )


# -------------------------------------------------------------- clients
async def create_client(name, company_name=None, phone=None, email=None, notes=None, status="active",
                        main_manager_id=None) -> int:
    return await execute(
        "INSERT INTO clients (name, company_name, phone, email, notes, status, main_manager_id) VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (name, company_name, phone, email, notes, status, main_manager_id),
    )


async def list_clients(client_ids: list[int] | None = None):
    rows = await query(
        "SELECT c.*, m.name AS main_manager_name, (SELECT COUNT(*) FROM projects p WHERE p.client_id = c.id AND p.deleted_at IS NULL) AS project_count "
        "FROM clients c LEFT JOIN poc_users m ON m.id = c.main_manager_id WHERE c.deleted_at IS NULL ORDER BY c.name"
    )
    if client_ids is not None:
        rows = [r for r in rows if r["id"] in client_ids]
    return rows


async def get_client(client_id: int, include_deleted: bool = False):
    rows = await query("SELECT c.*, m.name AS main_manager_name FROM clients c "
                       "LEFT JOIN poc_users m ON m.id = c.main_manager_id WHERE c.id = %s" +
                       ("" if include_deleted else " AND c.deleted_at IS NULL"), (client_id,))
    return rows[0] if rows else None


async def update_client(client_id: int, fields: dict):
    cols, vals = _sets(fields, ("name", "company_name", "phone", "email", "notes", "status", "main_manager_id"))
    if cols:
        await execute(f"UPDATE clients SET {', '.join(cols)} WHERE id = %s", (*vals, client_id))


# ------------------------------------------------------------- projects
async def create_project(client_id, name, description=None, status="planning", priority="medium",
                         start_date=None, target_date=None, main_manager_id=None, group_id=None) -> int:
    return await execute(
        """INSERT INTO projects (client_id, group_id, name, description, status, priority, start_date, target_date, main_manager_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (client_id, group_id, name, description, status, priority, start_date, target_date, main_manager_id),
    )


_PROJECT_SELECT = """
    SELECT p.*, c.name AS client_name, c.company_name, m.name AS main_manager_name,
           g.group_name, g.whatsapp_group_id, g.account_id AS group_account_id, g.ai_mode, g.ai_enabled
    FROM projects p JOIN clients c ON c.id = p.client_id
    LEFT JOIN poc_users m ON m.id = p.main_manager_id
    LEFT JOIN client_groups g ON g.id = p.group_id
"""


async def get_project(project_id: int, include_deleted: bool = False):
    rows = await query(_PROJECT_SELECT + " WHERE p.id = %s" + ("" if include_deleted else " AND p.deleted_at IS NULL"), (project_id,))
    return rows[0] if rows else None


async def list_projects_by_group(group_id: int):
    """All live projects served by one WhatsApp group (a group can serve several)."""
    return await query(_PROJECT_SELECT + " WHERE p.group_id = %s AND p.deleted_at IS NULL ORDER BY p.id", (group_id,))


async def get_project_by_group(group_id: int):
    """First project of the group (kept for single-project callers)."""
    rows = await list_projects_by_group(group_id)
    return rows[0] if rows else None


async def list_projects(user: dict | None = None, include_deleted: bool = False):
    rows = await query(_PROJECT_SELECT + ("" if include_deleted else " WHERE p.deleted_at IS NULL") + " ORDER BY p.created_at DESC")
    if user is None or user["role"] == "admin":
        return rows
    allowed = set(await accessible_project_ids(user))
    return [r for r in rows if r["id"] in allowed]


async def update_project(project_id: int, fields: dict):
    cols, vals = _sets(fields, ("name", "description", "status", "priority", "start_date", "target_date",
                                "main_manager_id", "routing_threshold", "group_id", "client_id"))
    if cols:
        await execute(f"UPDATE projects SET {', '.join(cols)} WHERE id = %s", (*vals, project_id))


async def accessible_project_ids(user: dict) -> list[int]:
    role = user["role"]
    if role == "admin":
        return [r["id"] for r in await query("SELECT id FROM projects WHERE deleted_at IS NULL")]
    ids = set()
    for r in await query("SELECT id FROM projects WHERE main_manager_id = %s AND deleted_at IS NULL", (user["id"],)):
        ids.add(r["id"])
    if role != "main_manager":
        for r in await query("SELECT project_id FROM project_departments WHERE poc_user_id = %s", (user["id"],)):
            ids.add(r["project_id"])
        for r in await query("SELECT project_id FROM project_department_members WHERE user_id = %s", (user["id"],)):
            ids.add(r["project_id"])
    if ids:
        live = {r["id"] for r in await query("SELECT id FROM projects WHERE deleted_at IS NULL AND id IN (" + ",".join(["%s"] * len(ids)) + ")", tuple(ids))}
        ids &= live
    return sorted(ids)


async def user_can_access_project(user: dict, project_id: int) -> bool:
    return user["role"] == "admin" or project_id in await accessible_project_ids(user)


async def user_can_manage_project(user: dict, project_id: int) -> bool:
    """Admin, or the project's Main Manager."""
    if user["role"] == "admin":
        return True
    rows = await query("SELECT 1 FROM projects WHERE id = %s AND main_manager_id = %s AND deleted_at IS NULL", (project_id, user["id"]))
    return bool(rows)


async def user_department_ids_in_project(user_id: int, project_id: int) -> list[int]:
    out = set()
    for r in await query("SELECT department_id FROM project_departments WHERE project_id = %s AND poc_user_id = %s", (project_id, user_id)):
        out.add(r["department_id"])
    for r in await query("SELECT department_id FROM project_department_members WHERE project_id = %s AND user_id = %s", (project_id, user_id)):
        out.add(r["department_id"])
    return sorted(out)


# ----------------------------------------------- project departments etc
async def add_project_department(project_id: int, department_id: int, poc_user_id: int | None = None):
    await execute(
        "INSERT INTO project_departments (project_id, department_id, poc_user_id) VALUES (%s,%s,%s) "
        "ON DUPLICATE KEY UPDATE poc_user_id = COALESCE(VALUES(poc_user_id), poc_user_id)",
        (project_id, department_id, poc_user_id),
    )


async def remove_project_department(project_id: int, department_id: int):
    await execute("DELETE FROM project_departments WHERE project_id = %s AND department_id = %s", (project_id, department_id))


async def set_department_poc(project_id: int, department_id: int, user_id: int | None):
    await add_project_department(project_id, department_id)
    await execute(
        "UPDATE project_departments SET poc_user_id = %s WHERE project_id = %s AND department_id = %s",
        (user_id, project_id, department_id),
    )


async def add_department_member(project_id: int, department_id: int, user_id: int):
    await add_project_department(project_id, department_id)
    await execute(
        "INSERT IGNORE INTO project_department_members (project_id, department_id, user_id) VALUES (%s,%s,%s)",
        (project_id, department_id, user_id),
    )


async def remove_department_member(project_id: int, department_id: int, user_id: int):
    await execute(
        "DELETE FROM project_department_members WHERE project_id=%s AND department_id=%s AND user_id=%s",
        (project_id, department_id, user_id),
    )


async def list_project_departments(project_id: int):
    """Departments of a project with POC (+ live WhatsApp status) and members."""
    depts = await query(
        """SELECT pd.department_id, d.name, d.description, pd.poc_user_id, u.name AS poc_name,
                  w.account_id AS poc_account_id, a.status AS poc_account_status
           FROM project_departments pd JOIN departments d ON d.id = pd.department_id AND d.active = 1 AND d.deleted_at IS NULL
           LEFT JOIN poc_users u ON u.id = pd.poc_user_id AND u.active = 1
           LEFT JOIN whatsapp_account_users w ON w.user_id = pd.poc_user_id
           LEFT JOIN accounts a ON a.id = w.account_id
           WHERE pd.project_id = %s ORDER BY d.name""",
        (project_id,),
    )
    members = await query(
        """SELECT m.department_id, u.id AS user_id, u.name FROM project_department_members m
           JOIN poc_users u ON u.id = m.user_id WHERE m.project_id = %s""",
        (project_id,),
    )
    for d in depts:
        d["members"] = [{"user_id": m["user_id"], "name": m["name"]} for m in members if m["department_id"] == d["department_id"]]
    return depts


async def update_recipient(project: dict, department_id, actor: dict):
    """Reporting chain: a member's change goes to their department POC, a POC's
    change goes to the Main Manager. The Main Manager's own changes notify no one."""
    if actor["id"] == project.get("main_manager_id") or actor["role"] == "admin":
        return None
    for d in await list_project_departments(project["id"]):
        if d["department_id"] == department_id:
            if d["poc_user_id"] and d["poc_user_id"] != actor["id"]:
                return d["poc_user_id"]
            break
    return project.get("main_manager_id")


# ---------------------------------------------------------------- tasks
TASK_STATUSES = ("todo", "in_progress", "blocked", "review", "completed", "cancelled")


async def create_task(project_id, title, department_id=None, assigned_user_id=None, description=None,
                      priority="medium", status="todo", due_date=None, created_by=None) -> int:
    return await execute(
        """INSERT INTO tasks (project_id, department_id, assigned_user_id, title, description, priority, status, due_date, created_by)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (project_id, department_id, assigned_user_id, title, description, priority, status, due_date, created_by),
    )


async def get_task(task_id: int, include_deleted: bool = False):
    rows = await query("SELECT * FROM tasks WHERE id = %s" + ("" if include_deleted else " AND deleted_at IS NULL"), (task_id,))
    return rows[0] if rows else None


async def update_task(task_id: int, fields: dict):
    cols, vals = _sets(fields, ("title", "description", "priority", "status", "due_date", "assigned_user_id", "department_id"))
    if cols:
        await execute(f"UPDATE tasks SET {', '.join(cols)} WHERE id = %s", (*vals, task_id))


async def list_tasks(project_id=None, department_id=None, assigned_user_id=None, status=None,
                     project_ids: list[int] | None = None, open_only: bool = False):
    sql = ("SELECT t.*, d.name AS department_name, u.name AS assignee_name, p.name AS project_name, "
           "(t.due_date IS NOT NULL AND t.due_date < CURDATE() AND t.status NOT IN ('completed','cancelled')) AS overdue "
           "FROM tasks t LEFT JOIN departments d ON d.id = t.department_id "
           "LEFT JOIN poc_users u ON u.id = t.assigned_user_id JOIN projects p ON p.id = t.project_id WHERE t.deleted_at IS NULL")
    params: list = []
    for col, val in (("t.project_id", project_id), ("t.department_id", department_id),
                     ("t.assigned_user_id", assigned_user_id), ("t.status", status)):
        if val is not None:
            sql += f" AND {col} = %s"
            params.append(val)
    if open_only:
        sql += " AND t.status NOT IN ('completed','cancelled')"
    if project_ids is not None:
        if not project_ids:
            return []
        sql += " AND t.project_id IN (" + ",".join(["%s"] * len(project_ids)) + ")"
        params.extend(project_ids)
    return await query(sql + " ORDER BY t.created_at DESC", tuple(params))


async def relevant_tasks_for_ai(project_id: int, department_id: int | None, limit: int = 8):
    sql = "SELECT title, status, due_date FROM tasks WHERE project_id = %s AND status NOT IN ('cancelled')"
    params = [project_id]
    if department_id:
        sql += " AND department_id = %s"
        params.append(department_id)
    return await query(sql + " ORDER BY updated_at DESC LIMIT %s", (*params, limit))


# -------------------------------------------------------------- updates
async def create_update(project_id, department_id, author_id, completed=None, in_progress=None, blocked=None,
                        next_steps=None, summary=None, visibility="internal", task_id=None) -> int:
    return await execute(
        """INSERT INTO project_updates (project_id, department_id, author_id, completed, in_progress, blocked, next_steps, summary, visibility, task_id)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (project_id, department_id, author_id, completed, in_progress, blocked, next_steps, summary, visibility, task_id),
    )


async def mark_update_sent(update_id: int):
    await execute("UPDATE project_updates SET sent_to_client = 1 WHERE id = %s", (update_id,))


async def list_updates(project_id=None, department_id=None, project_ids=None, limit=50, author_id=None):
    sql = ("SELECT up.*, d.name AS department_name, u.name AS author_name, p.name AS project_name, t.title AS task_title "
           "FROM project_updates up "
           "LEFT JOIN departments d ON d.id = up.department_id LEFT JOIN poc_users u ON u.id = up.author_id "
           "LEFT JOIN tasks t ON t.id = up.task_id JOIN projects p ON p.id = up.project_id WHERE 1=1")
    params: list = []
    if author_id is not None:
        sql += " AND up.author_id = %s"; params.append(author_id)
    if project_id is not None:
        sql += " AND up.project_id = %s"; params.append(project_id)
    if department_id is not None:
        sql += " AND up.department_id = %s"; params.append(department_id)
    if project_ids is not None:
        if not project_ids:
            return []
        sql += " AND up.project_id IN (" + ",".join(["%s"] * len(project_ids)) + ")"
        params.extend(project_ids)
    return await query(sql + " ORDER BY up.id DESC LIMIT %s", (*params, limit))


# ------------------------------------------------------- verified updates
VERIFIED_VISIBILITIES = ("internal", "client", "both")


async def add_verified_update(text: str, *, group_id=None, project_id=None, author: dict | None = None,
                              visibility: str = "internal") -> int:
    """A fact a manager / POC / admin confirmed. Stored with who added it and when."""
    return await execute(
        """INSERT INTO verified_updates (group_id, project_id, author_id, author_name, author_role, text, visibility)
           VALUES (%s,%s,%s,%s,%s,%s,%s)""",
        (group_id, project_id, (author or {}).get("id"),
         ((author or {}).get("name") or (author or {}).get("username")), effective_role((author or {}).get("role") or "") or None,
         text, visibility),
    )


async def mark_verified_sent(update_id: int):
    await execute("UPDATE verified_updates SET sent_to_client = 1 WHERE id = %s", (update_id,))


async def get_verified_update(update_id: int):
    rows = await query("SELECT * FROM verified_updates WHERE id = %s", (update_id,))
    return rows[0] if rows else None


async def delete_verified_update(update_id: int):
    await execute("DELETE FROM verified_updates WHERE id = %s", (update_id,))


async def list_verified_updates(*, project_id=None, group_id=None, include_group_level=False, limit=200):
    """Newest first. By project: that project's updates (+ the group's project-less ones when include_group_level).
    By group: every update of the group, whichever project it belongs to."""
    sql = ("SELECT v.*, p.name AS project_name, u.name AS current_author_name FROM verified_updates v "
           "LEFT JOIN projects p ON p.id = v.project_id LEFT JOIN poc_users u ON u.id = v.author_id WHERE ")
    if project_id is not None:
        cond, params = "v.project_id = %s", [project_id]
        if include_group_level and group_id is not None:
            cond = "(v.project_id = %s OR (v.group_id = %s AND v.project_id IS NULL))"
            params.append(group_id)
    elif group_id is not None:
        cond, params = "v.group_id = %s", [group_id]
    else:
        return []
    rows = await query(f"{sql}{cond} ORDER BY v.id DESC LIMIT %s", (*params, limit))
    for r in rows:
        r["author_name"] = r.get("current_author_name") or r.get("author_name")
    return rows


def format_update_text(row: dict, department_name: str | None) -> str:
    lines = [f"{(department_name or 'Project').upper()} UPDATE"]
    for label, key in (("Completed", "completed"), ("In progress", "in_progress"),
                       ("Blocked", "blocked"), ("Next", "next_steps")):
        if row.get(key):
            lines.append(f"{label}: {row[key]}")
    if len(lines) == 1 and row.get("summary"):
        lines.append(row["summary"])
    return "\n".join(lines)


SOFT_TABLES = {"client": "clients", "project": "projects", "department": "departments", "user": "poc_users", "task": "tasks"}


async def soft_delete(entity: str, entity_id: int):
    """Marks a row deleted without removing it (messages, audit and relationships keep pointing at it).
    Users are also deactivated so they can no longer log in."""
    t = SOFT_TABLES[entity]
    extra = ", active = 0" if entity == "user" else ""
    await execute(f"UPDATE {t} SET deleted_at = NOW(){extra} WHERE id = %s AND deleted_at IS NULL", (entity_id,))


async def restore(entity: str, entity_id: int):
    t = SOFT_TABLES[entity]
    await execute(f"UPDATE {t} SET deleted_at = NULL WHERE id = %s", (entity_id,))


async def list_deleted(entity: str):
    t = SOFT_TABLES[entity]
    cols = "id, name" if entity in ("client", "project", "department", "user") else "id, title AS name"
    return await query(f"SELECT {cols}, deleted_at FROM {t} WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT 200")


# ------------------------------------------- routing / escalation / audit
async def set_thread_routing(thread_id: int, department_id, poc_id, status: str, confidence, project_id=None):
    await execute(
        "UPDATE conversation_threads SET current_department_id=%s, assigned_poc_id=%s, routing_status=%s, routing_confidence=%s, "
        "current_project_id=COALESCE(%s, current_project_id) WHERE id=%s",
        (department_id, poc_id, status, confidence, project_id, thread_id),
    )


async def log_routing(thread_id, event, project_id=None, client_message_id=None, department_id=None,
                      poc_user_id=None, account_id=None, confidence=None, reason=None):
    return await execute(
        """INSERT INTO conversation_routing (thread_id, project_id, client_message_id, event, department_id, poc_user_id, account_id, confidence, reason)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (thread_id, project_id, client_message_id, event, department_id, poc_user_id, account_id, confidence,
         (reason or "")[:500] or None),
    )


async def list_routing(thread_id: int):
    return await query(
        """SELECT r.*, d.name AS department_name, u.name AS poc_name FROM conversation_routing r
           LEFT JOIN departments d ON d.id = r.department_id LEFT JOIN poc_users u ON u.id = r.poc_user_id
           WHERE r.thread_id = %s ORDER BY r.id""",
        (thread_id,),
    )


async def create_escalation(thread_id, project_id, client_message_id, reason, detail=None) -> int:
    return await execute(
        "INSERT INTO escalations (thread_id, project_id, client_message_id, reason, detail) VALUES (%s,%s,%s,%s,%s)",
        (thread_id, project_id, client_message_id, reason, (detail or "")[:500] or None),
    )


async def list_escalations(project_ids=None, open_only=True):
    sql = ("SELECT e.*, p.name AS project_name FROM escalations e LEFT JOIN projects p ON p.id = e.project_id WHERE 1=1")
    params: list = []
    if open_only:
        sql += " AND e.status = 'open'"
    if project_ids is not None:
        if not project_ids:
            return []
        sql += " AND e.project_id IN (" + ",".join(["%s"] * len(project_ids)) + ")"
        params.extend(project_ids)
    return await query(sql + " ORDER BY e.id DESC LIMIT 100", tuple(params))


async def resolve_escalation(escalation_id: int):
    await execute("UPDATE escalations SET status='resolved', resolved_at=NOW() WHERE id=%s", (escalation_id,))


async def add_audit(user_id, action, entity=None, entity_id=None, detail=None, project_id=None, department_id=None, success=True):
    return await execute(
        "INSERT INTO audit_logs (user_id, action, entity, entity_id, detail, project_id, department_id, success) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (user_id, action, entity, str(entity_id) if entity_id is not None else None, detail, project_id, department_id, 1 if success else 0),
    )


async def list_audit(limit=100, project_id=None):
    sql = "SELECT a.*, u.name AS user_name FROM audit_logs a LEFT JOIN poc_users u ON u.id = a.user_id"
    params: list = []
    if project_id is not None:
        sql += " WHERE (a.project_id = %s OR (a.entity = 'project' AND a.entity_id = %s))"
        params.extend([project_id, str(project_id)])
    return await query(sql + " ORDER BY a.id DESC LIMIT %s", (*params, limit))


async def list_project_threads(group_id: int, project_id: int | None = None):
    scope = "" if project_id is None else " AND (t.current_project_id = %s OR t.current_project_id IS NULL)"
    return await query(
        """SELECT t.id, t.thread_key, t.status, t.ai_paused, t.routing_status, t.routing_confidence,
                  t.current_department_id, t.current_project_id, d.name AS department_name, t.assigned_poc_id, u.name AS poc_name, t.updated_at
           FROM conversation_threads t LEFT JOIN departments d ON d.id = t.current_department_id
           LEFT JOIN poc_users u ON u.id = t.assigned_poc_id WHERE t.group_id = %s""" + scope + " ORDER BY t.id DESC",
        (group_id,) if project_id is None else (group_id, project_id),
    )


# --------------------------------------------------------- notifications
async def add_notification(user_id, kind, message, project_id=None, thread_id=None, client_message_id=None, ai_reply_id=None,
                           title=None, entity_type=None, entity_id=None, dedupe_key=None):
    """Idempotent when dedupe_key is given: the same (recipient, key) is stored once, so a retried webhook cannot notify twice."""
    if dedupe_key and await query("SELECT id FROM poc_notifications WHERE poc_id = %s AND dedupe_key = %s", (user_id, dedupe_key)):
        return None
    return await execute(
        """INSERT INTO poc_notifications (poc_id, thread_id, client_message_id, ai_reply_id, status, kind, project_id, message, title, entity_type, entity_id, dedupe_key)
           VALUES (%s,%s,%s,%s,'unread',%s,%s,%s,%s,%s,%s,%s)""",
        (user_id, thread_id, client_message_id, ai_reply_id, kind, project_id, (message or "")[:500], (title or "")[:190] or None,
         entity_type, str(entity_id) if entity_id is not None else None, dedupe_key),
    )


async def list_user_notifications(user_id: int, unread_only=False, limit=100, offset=0):
    sql = "SELECT n.*, p.name AS project_name FROM poc_notifications n LEFT JOIN projects p ON p.id = n.project_id WHERE n.poc_id = %s"
    if unread_only:
        sql += " AND n.status = 'unread'"
    return await query(sql + " ORDER BY n.id DESC LIMIT %s OFFSET %s", (user_id, limit, offset))


async def unread_count(user_id: int) -> int:
    return int((await query("SELECT COUNT(*) c FROM poc_notifications WHERE poc_id = %s AND status = 'unread'", (user_id,)))[0]["c"])


async def mark_all_read(user_id: int):
    await execute("UPDATE poc_notifications SET status = 'read' WHERE poc_id = %s AND status = 'unread'", (user_id,))


# ------------------------------------------------ classification / assignments
async def save_classification(message_id, thread_id, project_id, department_id, c: dict, requires_approval: bool):
    await execute(
        """INSERT INTO message_classifications (message_id, thread_id, project_id, department_id, intent, category, urgency, confidence,
                                                 needs_clarification, needs_manager, requires_approval, reason)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
           ON DUPLICATE KEY UPDATE project_id=VALUES(project_id), department_id=VALUES(department_id), intent=VALUES(intent),
             category=VALUES(category), urgency=VALUES(urgency), confidence=VALUES(confidence), needs_clarification=VALUES(needs_clarification),
             needs_manager=VALUES(needs_manager), requires_approval=VALUES(requires_approval), reason=VALUES(reason)""",
        (message_id, thread_id, project_id, department_id, (c.get("intent") or "")[:120] or None, (c.get("category") or "")[:64] or None,
         c.get("urgency", "normal"), c.get("confidence"), 1 if c.get("needs_clarification") else 0, 1 if c.get("needs_manager") else 0,
         1 if requires_approval else 0, (c.get("reason") or "")[:500] or None),
    )


async def add_assignment(message_id, thread_id, project_id, department_id, poc_user_id, kind="primary"):
    await execute(
        "INSERT IGNORE INTO message_assignments (message_id, thread_id, project_id, department_id, poc_user_id, kind) VALUES (%s,%s,%s,%s,%s,%s)",
        (message_id, thread_id, project_id, department_id, poc_user_id, kind),
    )


async def list_assignments(thread_id: int):
    return await query(
        """SELECT a.*, d.name AS department_name, u.name AS poc_name, p.name AS project_name FROM message_assignments a
           LEFT JOIN departments d ON d.id = a.department_id LEFT JOIN poc_users u ON u.id = a.poc_user_id
           LEFT JOIN projects p ON p.id = a.project_id WHERE a.thread_id = %s ORDER BY a.id""", (thread_id,))


async def admin_ids() -> list[int]:
    return [r["id"] for r in await query("SELECT id FROM poc_users WHERE role='admin' AND active=1")]


# ------------------------------------------------------------ dashboards
async def _dept_progress(project_ids: list[int] | None, department_ids: list[int] | None = None):
    sql = ("SELECT d.id AS department_id, d.name, COUNT(t.id) AS total, "
           "SUM(t.status='completed') AS completed FROM departments d LEFT JOIN tasks t ON t.department_id = d.id "
           "AND t.status <> 'cancelled'")
    params: list = []
    if project_ids is not None:
        if not project_ids:
            return []
        sql += " AND t.project_id IN (" + ",".join(["%s"] * len(project_ids)) + ")"
        params.extend(project_ids)
    sql += " WHERE d.active = 1"
    if department_ids is not None:
        if not department_ids:
            return []
        sql += " AND d.id IN (" + ",".join(["%s"] * len(department_ids)) + ")"
        params.extend(department_ids)
    rows = await query(sql + " GROUP BY d.id, d.name ORDER BY d.name", tuple(params))
    for r in rows:
        r["total"] = int(r["total"] or 0)
        r["completed"] = int(r["completed"] or 0)
        r["progress_pct"] = round(100 * r["completed"] / r["total"]) if r["total"] else 0
    return rows


async def manager_dashboard(user: dict):
    pids = None if user["role"] == "admin" else await accessible_project_ids(user)
    scope = ""
    params: tuple = ()
    if pids is not None:
        if not pids:
            pids = [0]
        scope = " WHERE project_id IN (" + ",".join(["%s"] * len(pids)) + ")"
        params = tuple(pids)

    async def one(sql, p=()):
        return int((await query(sql, p))[0]["c"] or 0)

    proj_where = "" if pids is None else " WHERE id IN (" + ",".join(["%s"] * len(pids)) + ")"
    active_where = " WHERE status='active'" if pids is None else proj_where + " AND status='active'"
    pp = () if pids is None else tuple(pids)
    thread_scope = ("" if pids is None else
                    " AND g.id IN (SELECT group_id FROM projects WHERE id IN (" + ",".join(["%s"] * len(pids)) + "))")
    open_conv = await one(
        "SELECT COUNT(*) c FROM conversation_threads t JOIN client_groups g ON g.id=t.group_id WHERE t.status='open'" + thread_scope, pp)
    unresolved = await one(
        "SELECT COUNT(*) c FROM conversation_threads t JOIN client_groups g ON g.id=t.group_id WHERE t.status='open' "
        "AND (t.routing_status IN ('needs_clarification','manager_fallback'))" + thread_scope, pp)
    return {
        "total_clients": await one("SELECT COUNT(DISTINCT client_id) c FROM projects" + proj_where, pp),
        "total_projects": await one("SELECT COUNT(*) c FROM projects" + proj_where, pp),
        "active_projects": await one("SELECT COUNT(*) c FROM projects" + active_where, pp),
        "pending_tasks": await one("SELECT COUNT(*) c FROM tasks" + (scope + " AND" if scope else " WHERE") + " status NOT IN ('completed','cancelled')", params),
        "overdue_tasks": await one("SELECT COUNT(*) c FROM tasks" + (scope + " AND" if scope else " WHERE") + " status NOT IN ('completed','cancelled') AND due_date IS NOT NULL AND due_date < CURDATE()", params),
        "open_conversations": open_conv,
        "unresolved_messages": unresolved,
        "department_progress": await _dept_progress(None if user["role"] == "admin" else pids),
        "recent_updates": await list_updates(project_ids=None if user["role"] == "admin" else pids, limit=8),
        "escalations": await list_escalations(None if user["role"] == "admin" else pids),
        "poc_availability": await poc_availability(None if user["role"] == "admin" else pids),
        "whatsapp_accounts": await list_accounts_with_users(),
    }


async def poc_availability(project_ids):
    sql = ("SELECT DISTINCT u.id AS user_id, u.name, a.status AS account_status FROM project_departments pd "
           "JOIN poc_users u ON u.id = pd.poc_user_id LEFT JOIN whatsapp_account_users w ON w.user_id = u.id "
           "LEFT JOIN accounts a ON a.id = w.account_id")
    params: tuple = ()
    if project_ids is not None:
        if not project_ids:
            return []
        sql += " WHERE pd.project_id IN (" + ",".join(["%s"] * len(project_ids)) + ")"
        params = tuple(project_ids)
    rows = await query(sql, params)
    for r in rows:
        r["available"] = r["account_status"] == "connected"
    return rows


async def department_dashboard(user: dict):
    pids = await accessible_project_ids(user)
    dept_ids: set[int] = set()
    for pid in pids:
        dept_ids.update(await user_department_ids_in_project(user["id"], pid))
    if user["role"] == "admin":
        dept_ids = {d["id"] for d in await list_departments(active_only=True)}
    own_tasks = await list_tasks(assigned_user_id=user["id"], open_only=True)
    dept_tasks = []
    if user["role"] != "department_member":
        for d in dept_ids:
            dept_tasks.extend(await list_tasks(department_id=d, open_only=True, project_ids=pids))
    seen, uniq = set(), []
    for t in list(dept_tasks) + list(own_tasks):
        if t["id"] not in seen:
            seen.add(t["id"]); uniq.append(t)
    return {
        "departments": [d for d in await list_departments() if d["id"] in dept_ids],
        "projects": [p for p in await list_projects(user)],
        "tasks": uniq,
        "pending_tasks": len(uniq),
        "overdue_tasks": sum(1 for t in uniq if t.get("overdue")),
        "progress": await _dept_progress(pids, sorted(dept_ids)),
        "recent_updates": (await list_updates(project_ids=pids, limit=10, author_id=user["id"] if user["role"] == "department_member" else None)) if dept_ids else [],
        "notifications": await list_user_notifications(user["id"], unread_only=True, limit=20),
    }
