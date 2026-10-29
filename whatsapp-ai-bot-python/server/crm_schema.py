"""
Additive schema + safe data migration for the multi-client / multi-department
/ multi-POC CRM layer.

Design decisions (documented in README "Migration notes"):

* `poc_users` IS the users table. It is extended in place (new roles, full
  profile columns) instead of copied into a second `users` table. Every
  existing foreign key (client_groups.assigned_poc_id, poc_notifications.poc_id,
  pending_approvals.decided_by) keeps working and no data is copied, so there
  is nothing to lose. Legacy role 'poc' is kept and treated as department_poc.
* Nothing is dropped or renamed. Every ALTER is guarded by an
  information_schema check, so running this on every startup is safe.
* Each existing `client_groups` row becomes a CRM client + project (once; keyed
  on projects.group_id UNIQUE). Its old assigned_poc_id becomes the project's
  Main Manager. Threads/messages are untouched.
"""
from server.db import query, execute

NEW_ROLES = "'admin','poc','main_manager','department_poc','department_member'"


async def _has_column(table: str, column: str) -> bool:
    rows = await query(
        "SELECT 1 FROM information_schema.columns WHERE table_schema = DATABASE() "
        "AND table_name = %s AND column_name = %s",
        (table, column),
    )
    return bool(rows)


async def _has_index(table: str, index: str) -> bool:
    return bool(await query(
        "SELECT 1 FROM information_schema.statistics WHERE table_schema = DATABASE() AND table_name = %s AND index_name = %s",
        (table, index)))


async def _add_column(table: str, column: str, ddl: str):
    if not await _has_column(table, column):
        await execute(f"ALTER TABLE `{table}` ADD COLUMN {column} {ddl}")


async def init_crm_tables():
    # ---- users (poc_users extended in place) ----------------------------
    await execute(f"ALTER TABLE poc_users MODIFY COLUMN role ENUM({NEW_ROLES}) NOT NULL DEFAULT 'poc'")
    await _add_column("poc_users", "email", "VARCHAR(190) DEFAULT NULL")
    await _add_column("poc_users", "phone", "VARCHAR(32) DEFAULT NULL")
    await _add_column("poc_users", "updated_at", "TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP")

    await execute(
        """CREATE TABLE IF NOT EXISTS departments (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(128) NOT NULL UNIQUE,
            description TEXT,
            active TINYINT(1) NOT NULL DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"""
    )
    await execute(
        """CREATE TABLE IF NOT EXISTS user_departments (
            user_id INT NOT NULL,
            department_id INT NOT NULL,
            PRIMARY KEY (user_id, department_id),
            FOREIGN KEY (user_id) REFERENCES poc_users(id) ON DELETE CASCADE,
            FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE CASCADE
        )"""
    )
    # One WhatsApp account <-> one system user (both directions unique).
    await execute(
        """CREATE TABLE IF NOT EXISTS whatsapp_account_users (
            account_id VARCHAR(64) NOT NULL PRIMARY KEY,
            user_id INT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES poc_users(id) ON DELETE CASCADE
        )"""
    )

    # ---- CRM ------------------------------------------------------------
    await execute(
        """CREATE TABLE IF NOT EXISTS clients (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(190) NOT NULL,
            company_name VARCHAR(190) DEFAULT NULL,
            phone VARCHAR(32) DEFAULT NULL,
            email VARCHAR(190) DEFAULT NULL,
            notes TEXT,
            status ENUM('lead','active','inactive') NOT NULL DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        )"""
    )
    await execute(
        """CREATE TABLE IF NOT EXISTS projects (
            id INT AUTO_INCREMENT PRIMARY KEY,
            client_id INT NOT NULL,
            group_id INT DEFAULT NULL UNIQUE,
            name VARCHAR(255) NOT NULL,
            description TEXT,
            status ENUM('planning','active','on_hold','completed','cancelled') NOT NULL DEFAULT 'planning',
            priority ENUM('low','medium','high','urgent') NOT NULL DEFAULT 'medium',
            start_date DATE DEFAULT NULL,
            target_date DATE DEFAULT NULL,
            main_manager_id INT DEFAULT NULL,
            routing_threshold DECIMAL(3,2) DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE,
            FOREIGN KEY (group_id) REFERENCES client_groups(id) ON DELETE SET NULL,
            FOREIGN KEY (main_manager_id) REFERENCES poc_users(id) ON DELETE SET NULL
        )"""
    )
    # Exactly one POC per (project, department): PRIMARY KEY enforces it.
    await execute(
        """CREATE TABLE IF NOT EXISTS project_departments (
            project_id INT NOT NULL,
            department_id INT NOT NULL,
            poc_user_id INT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (project_id, department_id),
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE CASCADE,
            FOREIGN KEY (poc_user_id) REFERENCES poc_users(id) ON DELETE SET NULL
        )"""
    )
    await execute(
        """CREATE TABLE IF NOT EXISTS project_department_members (
            project_id INT NOT NULL,
            department_id INT NOT NULL,
            user_id INT NOT NULL,
            PRIMARY KEY (project_id, department_id, user_id),
            FOREIGN KEY (project_id, department_id) REFERENCES project_departments(project_id, department_id) ON DELETE CASCADE,
            FOREIGN KEY (user_id) REFERENCES poc_users(id) ON DELETE CASCADE
        )"""
    )
    await execute(
        """CREATE TABLE IF NOT EXISTS tasks (
            id INT AUTO_INCREMENT PRIMARY KEY,
            project_id INT NOT NULL,
            department_id INT DEFAULT NULL,
            assigned_user_id INT DEFAULT NULL,
            title VARCHAR(255) NOT NULL,
            description TEXT,
            priority ENUM('low','medium','high','urgent') NOT NULL DEFAULT 'medium',
            status ENUM('todo','in_progress','blocked','review','completed','cancelled') NOT NULL DEFAULT 'todo',
            due_date DATE DEFAULT NULL,
            created_by INT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE SET NULL,
            FOREIGN KEY (assigned_user_id) REFERENCES poc_users(id) ON DELETE SET NULL,
            FOREIGN KEY (created_by) REFERENCES poc_users(id) ON DELETE SET NULL
        )"""
    )
    await execute(
        """CREATE TABLE IF NOT EXISTS project_updates (
            id INT AUTO_INCREMENT PRIMARY KEY,
            project_id INT NOT NULL,
            department_id INT DEFAULT NULL,
            author_id INT DEFAULT NULL,
            completed TEXT, in_progress TEXT, blocked TEXT, next_steps TEXT,
            summary VARCHAR(500) DEFAULT NULL,
            visibility ENUM('internal','client') NOT NULL DEFAULT 'internal',
            sent_to_client TINYINT(1) NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (department_id) REFERENCES departments(id) ON DELETE SET NULL,
            FOREIGN KEY (author_id) REFERENCES poc_users(id) ON DELETE SET NULL
        )"""
    )

    # ---- routing / escalation / audit ------------------------------------
    for col, ddl in (
        ("current_department_id", "INT DEFAULT NULL"),
        ("assigned_poc_id", "INT DEFAULT NULL"),
        ("routing_status", "VARCHAR(32) DEFAULT NULL"),
        ("routing_confidence", "DECIMAL(4,3) DEFAULT NULL"),
    ):
        await _add_column("conversation_threads", col, ddl)

    await execute(
        """CREATE TABLE IF NOT EXISTS conversation_routing (
            id INT AUTO_INCREMENT PRIMARY KEY,
            thread_id INT NOT NULL,
            project_id INT DEFAULT NULL,
            client_message_id INT DEFAULT NULL,
            event VARCHAR(48) NOT NULL,
            department_id INT DEFAULT NULL,
            poc_user_id INT DEFAULT NULL,
            account_id VARCHAR(64) DEFAULT NULL,
            confidence DECIMAL(4,3) DEFAULT NULL,
            reason VARCHAR(500) DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_routing_thread (thread_id),
            FOREIGN KEY (thread_id) REFERENCES conversation_threads(id) ON DELETE CASCADE
        )"""
    )
    await execute(
        """CREATE TABLE IF NOT EXISTS escalations (
            id INT AUTO_INCREMENT PRIMARY KEY,
            thread_id INT NOT NULL,
            project_id INT DEFAULT NULL,
            client_message_id INT DEFAULT NULL,
            reason VARCHAR(64) NOT NULL,
            detail VARCHAR(500) DEFAULT NULL,
            status ENUM('open','resolved') NOT NULL DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            resolved_at TIMESTAMP NULL DEFAULT NULL,
            FOREIGN KEY (thread_id) REFERENCES conversation_threads(id) ON DELETE CASCADE
        )"""
    )
    await execute(
        """CREATE TABLE IF NOT EXISTS audit_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT DEFAULT NULL,
            action VARCHAR(64) NOT NULL,
            entity VARCHAR(64) DEFAULT NULL,
            entity_id VARCHAR(64) DEFAULT NULL,
            detail TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_audit_action (action)
        )"""
    )

    # ---- notifications: targeted + typed (columns added, old rows keep working)
    await _add_column("poc_notifications", "kind", "VARCHAR(32) NOT NULL DEFAULT 'client_message'")
    await _add_column("poc_notifications", "project_id", "INT DEFAULT NULL")
    await _add_column("poc_notifications", "message", "VARCHAR(500) DEFAULT NULL")
    await execute("ALTER TABLE poc_notifications MODIFY COLUMN thread_id INT NULL")

    # ---- richer project memory (spec 45) ---------------------------------
    for col in ("objectives", "technical_context", "design_context", "seo_context",
                "known_decisions", "constraints_notes"):
        await _add_column("project_context", col, "TEXT")

    # ---- role-based flow (v11) ---------------------------------------------
    # Departments belong to the Main Manager who created them (NULL = shared/global,
    # e.g. created by an admin or by an earlier version). Two managers may both have "Tech".
    await _add_column("departments", "owner_user_id", "INT DEFAULT NULL")
    if await query("SELECT 1 FROM information_schema.statistics WHERE table_schema = DATABASE() "
                   "AND table_name = 'departments' AND index_name = 'name'"):
        await execute("ALTER TABLE departments DROP INDEX `name`")
    if not await query("SELECT 1 FROM information_schema.statistics WHERE table_schema = DATABASE() "
                       "AND table_name = 'departments' AND index_name = 'uq_dept_owner_name'"):
        await execute("ALTER TABLE departments ADD UNIQUE KEY uq_dept_owner_name (owner_user_id, name)")
    await _add_column("poc_users", "created_by", "INT DEFAULT NULL")       # who created this login
    await _add_column("clients", "main_manager_id", "INT DEFAULT NULL")    # default manager of the client's projects
    await _add_column("project_updates", "task_id", "INT DEFAULT NULL")    # a member's note on one task
    await execute(
        """CREATE TABLE IF NOT EXISTS account_link_requests (
            account_id VARCHAR(64) NOT NULL PRIMARY KEY,
            user_id INT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (user_id) REFERENCES poc_users(id) ON DELETE CASCADE
        )"""
    )
    # clients created before this version: take the manager of their first project
    await execute(
        """UPDATE clients c SET c.main_manager_id = (
               SELECT p.main_manager_id FROM projects p WHERE p.client_id = c.id AND p.main_manager_id IS NOT NULL
               ORDER BY p.id LIMIT 1) WHERE c.main_manager_id IS NULL"""
    )

    # ---- v12: one WhatsApp group can serve many projects / departments ------------------
    # ROOT CAUSE of "This WhatsApp group is already configured": projects.group_id was UNIQUE (1 group : 1 project).
    # The physical group stays unique per WhatsApp account (client_groups UNIQUE(account_id, whatsapp_group_id): that
    # rule is correct, one config row per real group). Many projects may now point at it; each project keeps its own
    # departments and POCs (project_departments PK (project_id, department_id)).
    if not await _has_index("projects", "idx_projects_group"):      # the FK needs an index before the unique one can go
        await execute("ALTER TABLE projects ADD INDEX idx_projects_group (group_id)")
    if await _has_index("projects", "group_id"):
        await execute("ALTER TABLE projects DROP INDEX `group_id`")
    await _add_column("conversation_threads", "current_project_id", "INT DEFAULT NULL")

    # ---- soft delete: history, audit and relationships survive a delete ------------------
    for t in ("clients", "projects", "departments", "poc_users", "tasks"):
        await _add_column(t, "deleted_at", "TIMESTAMP NULL DEFAULT NULL")
    await _add_column("audit_logs", "project_id", "INT DEFAULT NULL")
    await _add_column("audit_logs", "department_id", "INT DEFAULT NULL")
    await _add_column("audit_logs", "success", "TINYINT(1) NOT NULL DEFAULT 1")

    # ---- richer notifications -----------------------------------------------------------
    await _add_column("poc_notifications", "title", "VARCHAR(190) DEFAULT NULL")
    await _add_column("poc_notifications", "entity_type", "VARCHAR(32) DEFAULT NULL")
    await _add_column("poc_notifications", "entity_id", "VARCHAR(64) DEFAULT NULL")
    await _add_column("poc_notifications", "dedupe_key", "VARCHAR(190) DEFAULT NULL")
    if not await _has_index("poc_notifications", "uq_notif_dedupe"):
        await execute("ALTER TABLE poc_notifications ADD UNIQUE KEY uq_notif_dedupe (poc_id, dedupe_key)")

    # ---- AI classification + per-department assignment records ---------------------------
    await execute(
        """CREATE TABLE IF NOT EXISTS message_classifications (
            id INT AUTO_INCREMENT PRIMARY KEY,
            message_id INT DEFAULT NULL,
            thread_id INT NOT NULL,
            project_id INT DEFAULT NULL,
            department_id INT DEFAULT NULL,
            intent VARCHAR(120) DEFAULT NULL,
            category VARCHAR(64) DEFAULT NULL,
            urgency ENUM('low','normal','high','urgent') NOT NULL DEFAULT 'normal',
            confidence DECIMAL(4,3) DEFAULT NULL,
            needs_clarification TINYINT(1) NOT NULL DEFAULT 0,
            needs_manager TINYINT(1) NOT NULL DEFAULT 0,
            requires_approval TINYINT(1) NOT NULL DEFAULT 0,
            reason VARCHAR(500) DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_class_message (message_id),
            INDEX idx_class_thread (thread_id),
            FOREIGN KEY (thread_id) REFERENCES conversation_threads(id) ON DELETE CASCADE
        )"""
    )
    await execute(
        """CREATE TABLE IF NOT EXISTS message_assignments (
            id INT AUTO_INCREMENT PRIMARY KEY,
            message_id INT NOT NULL,
            thread_id INT NOT NULL,
            project_id INT NOT NULL,
            department_id INT DEFAULT NULL,
            poc_user_id INT DEFAULT NULL,
            kind ENUM('primary','secondary','manager') NOT NULL DEFAULT 'primary',
            status ENUM('open','done') NOT NULL DEFAULT 'open',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE KEY uq_assign (message_id, project_id, department_id, kind),
            INDEX idx_assign_poc (poc_user_id),
            FOREIGN KEY (thread_id) REFERENCES conversation_threads(id) ON DELETE CASCADE
        )"""
    )

    # ---- v13: verified updates (who confirmed what, per project / group) + 'both' visibility ------
    # visibility: internal = CRM only, client = sent to the WhatsApp group, both = CRM record AND sent to the group.
    ct = await query(
        "SELECT column_type AS ct FROM information_schema.columns WHERE table_schema = DATABASE() "
        "AND table_name = 'project_updates' AND column_name = 'visibility'")
    if ct and "both" not in str(ct[0].get("ct") or "").lower():
        await execute("ALTER TABLE project_updates MODIFY COLUMN visibility ENUM('internal','client','both') NOT NULL DEFAULT 'internal'")
    # the dashboard's "Status" box used to be VARCHAR(64): a longer status failed to save without any visible error
    st = await query("SELECT data_type AS dt FROM information_schema.columns WHERE table_schema = DATABASE() "
                     "AND table_name = 'project_context' AND column_name = 'status'")
    if st and str(st[0].get("dt") or "").lower() == "varchar":
        await execute("ALTER TABLE project_context MODIFY COLUMN status TEXT")
    await execute(
        """CREATE TABLE IF NOT EXISTS verified_updates (
            id INT AUTO_INCREMENT PRIMARY KEY,
            group_id INT DEFAULT NULL,
            project_id INT DEFAULT NULL,
            author_id INT DEFAULT NULL,
            author_name VARCHAR(190) DEFAULT NULL,
            author_role VARCHAR(32) DEFAULT NULL,
            text TEXT NOT NULL,
            visibility ENUM('internal','client','both') NOT NULL DEFAULT 'internal',
            sent_to_client TINYINT(1) NOT NULL DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_vu_group (group_id),
            INDEX idx_vu_project (project_id),
            FOREIGN KEY (group_id) REFERENCES client_groups(id) ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE,
            FOREIGN KEY (author_id) REFERENCES poc_users(id) ON DELETE SET NULL
        )"""
    )

    await migrate_legacy_groups()
    print("[crm] schema ready")


async def migrate_legacy_groups():
    """One-time (idempotent) mapping of pre-CRM client_groups into
    clients + projects. Never deletes or edits legacy rows."""
    rows = await query(
        """SELECT g.id, g.group_name, g.project_name, g.assigned_poc_id
           FROM client_groups g LEFT JOIN projects p ON p.group_id = g.id
           WHERE p.id IS NULL"""
    )
    for g in rows:
        name = g["group_name"] or f"Group {g['id']}"
        client_id = await execute(
            "INSERT INTO clients (name, notes, main_manager_id) VALUES (%s, %s, %s)",
            (name, "Auto-created from existing WhatsApp group during CRM migration", g["assigned_poc_id"]),
        )
        manager = g["assigned_poc_id"]
        if manager:
            # a legacy login POC becomes the project's Main Manager; make sure the role can act as one
            await execute(
                "UPDATE poc_users SET role = 'main_manager' WHERE id = %s AND role = 'poc'", (manager,)
            )
        await execute(
            "INSERT INTO projects (client_id, group_id, name, status, main_manager_id) VALUES (%s,%s,%s,'active',%s)",
            (client_id, g["id"], g["project_name"] or name, manager),
        )
    if rows:
        print(f"[crm] migrated {len(rows)} legacy group(s) into clients/projects")
