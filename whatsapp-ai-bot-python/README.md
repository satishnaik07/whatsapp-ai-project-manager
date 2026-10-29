# WhatsApp Project Manager

A dashboard and bot that manages client projects over WhatsApp groups.

A team links a WhatsApp number, adds a client's WhatsApp group to a project, and the system:

- reads messages from the client in that group,
- decides which department (Tech, Design, SEO, ...) should handle each message,
- drafts or sends a reply based on the project details, tasks and updates stored in the system,
- lets the team track tasks, post progress updates and approve replies from a web dashboard.

The original one-to-one sales bot is still in the code but is switched off by default
(`ENABLE_1TO1_AI_REPLIER=false`).

For the user flow and roles see [README-CRM.md](README-CRM.md).
For the full handover document see [DOCUMENTATION.md](DOCUMENTATION.md).

## How it is built

| Part | Technology | What it does |
|---|---|---|
| `bridge/` | Node.js (Baileys) | Connects to WhatsApp. Shows the QR code, receives messages, sends messages. It has no business logic. |
| `server/` | Python (FastAPI, Socket.IO) | API, login, database, AI replies, routing, notifications. |
| `public/` | HTML and JavaScript | The dashboard. Open `/pm/` for the project manager dashboard. |
| MySQL | Database | All data. Tables are created automatically on start. |

The bridge and the server talk to each other over HTTP on the same machine.

## Requirements

- Python 3.11 or newer
- Node.js and npm
- MySQL or MariaDB
- A DeepSeek API key (the AI uses DeepSeek through an OpenAI compatible API)

## Setup

1. Create the database (the app also creates it if it is missing):

   ```
   CREATE DATABASE whatsapp_ai_bot;
   ```

2. Copy `.env.example` to `.env` in the project root and fill in the values (see the table below).

3. Start the bridge (terminal 1):

   ```
   cd bridge
   npm install
   cp .env.example .env
   npm start
   ```

   It should print that it is running on port 4000.

4. Start the server (terminal 2, from the project root):

   ```
   python -m venv .venv
   source .venv/bin/activate        # Windows: .venv\Scripts\activate
   pip install -r requirements.txt
   uvicorn server.main:app --app-dir . --host 0.0.0.0 --port 8000
   ```

5. Open `http://localhost:8000/pm/`.
   The first time, type a username and a password (at least 8 characters) and press Log in.
   Because no user exists yet, you are asked to create the first admin.

## Main settings (`.env`)

| Name | Meaning |
|---|---|
| `PORT` | Server port. Default 8000. |
| `BRIDGE_URL` | Where the bridge runs. Default `http://localhost:4000`. |
| `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME` | MySQL connection. The default port in the example is 3307. Change it to 3306 if that is what your MySQL uses. |
| `DEEPSEEK_API_KEY`, `DEEPSEEK_MODEL`, `DEEPSEEK_BASE_URL` | AI provider settings. |
| `JWT_SECRET` | Secret used for dashboard logins. Required. Use a long random value. |
| `JWT_EXPIRY_SECONDS` | How long a login lasts. Default 12 hours. |
| `DEPARTMENT_CLASSIFICATION_CONFIDENCE_THRESHOLD` | Minimum confidence (0 to 1) before a message is sent straight to a department. Default 0.75. Below this the Main Manager gets it. |
| `POC_OFFLINE_FALLBACK_ENABLED` | If a department's POC has no connected WhatsApp, send the message to the Main Manager. Default true. |
| `GROUP_AI_REPLY_DELAY_SECONDS` | Wait before an automatic group reply goes out, so a person can step in first. |
| `ENABLE_1TO1_AI_REPLIER` | Turns the old one-to-one sales bot on or off. Default false. |
| `GOOGLE_*` | Only needed for the old sales bot's meeting booking. |

To make a random `JWT_SECRET`:

```
python -c "import secrets; print(secrets.token_hex(32))"
```

## Tests

```
pip install -r requirements-dev.txt
pytest tests -v
```

Most tests in `tests/test_crm.py` need a running MySQL or MariaDB. They use a separate database
called `whatsapp_ai_bot_test`, which is dropped and recreated on every run. If no database is
reachable those tests are skipped. Set `TEST_DB_PORT`, `DB_HOST`, `DB_USER` and `DB_PASSWORD` if
your database is not on the defaults.

## Project layout

```
bridge/        WhatsApp connector (Node.js)
server/        Python backend
  main.py        app start, webhooks, message handling
  group_db.py    database code for groups, threads, messages
  group_routes.py  /api/pm/... endpoints
  group_ai_replier.py  builds the AI reply
  crm_schema.py  database migration for the project/CRM tables
  repositories/  database queries
  routes/        /api/... endpoints
  services/      routing, notifications, audit, AI context
public/pm/     dashboard pages
sql/           optional SQL schema file
tests/         automated tests
```

## Security notes

- Do not commit `.env` files or `bridge/auth_sessions/`. The auth_sessions folder holds the
  logged-in WhatsApp session.
- Keep the bridge and the server on a private network. The bridge sends data to the server
  without a signature check.
- Use HTTPS in front of the server in production.