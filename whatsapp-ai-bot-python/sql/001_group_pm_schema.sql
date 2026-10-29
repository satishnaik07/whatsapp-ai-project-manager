-- WhatsApp AI Project Manager / CRM — schema migration
--
-- This is a REFERENCE copy of the exact same DDL that
-- server/group_db.py's init_group_tables() runs automatically every
-- time the Python app starts (see main.py's on_startup handler). You do
-- NOT have to run this file by hand — starting the app is enough.
--
-- It's provided so a DBA can review/apply it explicitly (e.g. in a
-- staging pipeline that runs migrations separately from app startup).
--
-- Safe to run multiple times: every statement is idempotent
-- (CREATE TABLE IF NOT EXISTS). No existing table (accounts, contacts,
-- messages, meetings) is touched, altered, or dropped by this file.
--
-- Apply with:
--   mysql -u <user> -p <database_name> < sql/001_group_pm_schema.sql

CREATE TABLE IF NOT EXISTS poc_users (
    id INT AUTO_INCREMENT PRIMARY KEY,
    username VARCHAR(64) NOT NULL UNIQUE,
    password_hash VARCHAR(255) NOT NULL,
    name VARCHAR(128) DEFAULT NULL,
    role ENUM('admin','poc') NOT NULL DEFAULT 'poc',
    active TINYINT(1) DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS client_groups (
    id INT AUTO_INCREMENT PRIMARY KEY,
    account_id VARCHAR(64) NOT NULL,
    whatsapp_group_id VARCHAR(128) NOT NULL,
    group_name VARCHAR(255) DEFAULT NULL,
    project_name VARCHAR(255) DEFAULT NULL,
    assigned_poc_id INT DEFAULT NULL,
    ai_enabled TINYINT(1) DEFAULT 1,
    ai_mode ENUM('auto','approval','manual') NOT NULL DEFAULT 'approval',
    active TINYINT(1) DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY unique_group (account_id, whatsapp_group_id),
    FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE CASCADE,
    FOREIGN KEY (assigned_poc_id) REFERENCES poc_users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS group_members (
    id INT AUTO_INCREMENT PRIMARY KEY,
    group_id INT NOT NULL,
    member_jid VARCHAR(128) NOT NULL,
    member_name VARCHAR(128) DEFAULT NULL,
    role ENUM('client','internal') NOT NULL,
    active TINYINT(1) DEFAULT 1,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_member (group_id, member_jid),
    FOREIGN KEY (group_id) REFERENCES client_groups(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS conversation_threads (
    id INT AUTO_INCREMENT PRIMARY KEY,
    thread_key VARCHAR(64) NOT NULL UNIQUE,
    group_id INT NOT NULL,
    project_name VARCHAR(255) DEFAULT NULL,
    status ENUM('open','resolved','closed') NOT NULL DEFAULT 'open',
    ai_paused TINYINT(1) NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES client_groups(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS group_messages (
    id INT AUTO_INCREMENT PRIMARY KEY,
    thread_id INT NOT NULL,
    group_id INT NOT NULL,
    whatsapp_message_id VARCHAR(128) DEFAULT NULL,
    sender_jid VARCHAR(128) DEFAULT NULL,
    sender_name VARCHAR(128) DEFAULT NULL,
    sender_role ENUM('client','internal','ai','unknown') NOT NULL DEFAULT 'unknown',
    direction ENUM('in','out') NOT NULL,
    body TEXT,
    message_type VARCHAR(32) DEFAULT 'text',
    reply_to_whatsapp_id VARCHAR(128) DEFAULT NULL,
    is_client_visible TINYINT(1) DEFAULT 1,
    processing_status ENUM('received','processed','ignored','failed') NOT NULL DEFAULT 'received',
    delivery_status ENUM('n_a','pending','sent','failed') NOT NULL DEFAULT 'n_a',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_wa_message (group_id, whatsapp_message_id),
    FOREIGN KEY (thread_id) REFERENCES conversation_threads(id) ON DELETE CASCADE,
    FOREIGN KEY (group_id) REFERENCES client_groups(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS project_context (
    id INT AUTO_INCREMENT PRIMARY KEY,
    group_id INT NOT NULL UNIQUE,
    description TEXT,
    scope TEXT,
    status VARCHAR(64) DEFAULT NULL,
    verified_updates TEXT,
    communication_instructions TEXT,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    FOREIGN KEY (group_id) REFERENCES client_groups(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pending_approvals (
    id INT AUTO_INCREMENT PRIMARY KEY,
    thread_id INT NOT NULL,
    group_id INT NOT NULL,
    client_message_id INT DEFAULT NULL,
    draft_reply TEXT,
    status ENUM('pending','approved','rejected','sent') NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    decided_at TIMESTAMP NULL DEFAULT NULL,
    decided_by INT DEFAULT NULL,
    FOREIGN KEY (thread_id) REFERENCES conversation_threads(id) ON DELETE CASCADE,
    FOREIGN KEY (group_id) REFERENCES client_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (client_message_id) REFERENCES group_messages(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS poc_notifications (
    id INT AUTO_INCREMENT PRIMARY KEY,
    poc_id INT DEFAULT NULL,
    thread_id INT NOT NULL,
    client_message_id INT DEFAULT NULL,
    ai_reply_id INT DEFAULT NULL,
    status ENUM('unread','read') NOT NULL DEFAULT 'unread',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (poc_id) REFERENCES poc_users(id) ON DELETE SET NULL,
    FOREIGN KEY (thread_id) REFERENCES conversation_threads(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS group_history_buffer (
    id INT AUTO_INCREMENT PRIMARY KEY,
    account_id VARCHAR(64) NOT NULL,
    group_jid VARCHAR(128) NOT NULL,
    whatsapp_message_id VARCHAR(128) DEFAULT NULL,
    sender_jid VARCHAR(128) DEFAULT NULL,
    sender_name VARCHAR(128) DEFAULT NULL,
    direction ENUM('in','out') NOT NULL,
    body TEXT,
    wa_timestamp INT DEFAULT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY unique_buffered_msg (account_id, group_jid, whatsapp_message_id)
);

-- Upgrading a database that was set up before ai_paused existed on
-- conversation_threads (safe no-op if the column is already there):
-- ALTER TABLE conversation_threads ADD COLUMN ai_paused TINYINT(1) NOT NULL DEFAULT 0;
