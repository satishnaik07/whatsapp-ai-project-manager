# Projects, departments and roles

This file explains how the project manager part works day to day.

## Structure

```
Client
  Project
    WhatsApp group
    Departments (Tech, Design, SEO, ...)
      one POC (point of contact) per department
      team members
      tasks
    Updates and verified updates
```

One WhatsApp group can serve more than one project, as long as all the projects belong to the
same client.

## Roles

| Role | What they do |
|---|---|
| Admin | Creates clients, projects and users. Sees everything. |
| Main Manager | Looks after the projects of a client. Links their own WhatsApp, creates departments, adds the group, POCs and members, creates tasks. |
| Department POC | Handles one department in a project. Links their own WhatsApp, creates tasks for their members, posts updates, approves replies. |
| Department Member | Sees only their own tasks. Changes the status and adds a progress note. |

Each person logs in with their own user ID and password and sees only their own menu.

Reporting chain: a member's note goes to their POC. A POC's update goes to the Main Manager.
Only an update marked for the client is posted to the WhatsApp group, and it is always sent
from the POC's or manager's own number.

## Permissions

"Own" means their own projects, department or people.

| Action | Admin | Main Manager | POC | Member |
|---|---|---|---|---|
| Create, edit, delete clients | yes | no | no | no |
| Create, delete projects | yes | no | no | no |
| Edit project | yes | own | no | no |
| Create departments | yes | yes | no | no |
| Create, edit, delete users | yes | no | no | no |
| Link or unlink group, assign POCs and members | yes | own projects | no | no |
| Link own WhatsApp | yes | yes | yes | no |
| Create tasks | yes | own projects | own department | no |
| Delete tasks | yes | own projects | no (can cancel) | no |
| Post update | yes | yes | yes | internal note only |
| Add verified update | yes | own projects | own projects | no (read only) |
| Approve AI replies | yes | yes | yes | no |
| Activity log and deleted items | yes | no | no | no |

The checks are done on the server, not only in the screens. A forbidden action returns 403.

## Setting up a project

1. Admin creates a client and a Main Manager login for that client.
2. Admin creates a project under the client.
3. Main Manager logs in and links their WhatsApp (My WhatsApp, scan the QR code).
4. Main Manager creates departments.
5. Main Manager opens the project and uses Add group and team:
   pick the WhatsApp group, mark each member as client or our team, choose a POC per department,
   assign members to departments, create logins, then choose the AI mode.
6. POCs and members log in. POCs link their own WhatsApp.

A department can have only one POC in a project. Deleted clients, projects, departments,
users and tasks are hidden, not erased. Admin can restore them from Deleted items.

## How a client message is handled

1. A client writes in the group. Messages from people who are not in the group list are only
   logged, never answered.
2. The system finds the project and the open conversation thread.
3. Messages about pricing, contracts, legal topics, an angry client, or a request for the
   manager go to the Main Manager and need approval.
4. Otherwise the AI picks a department. If it is not confident enough it asks the client one
   short question instead.
5. The department's POC becomes responsible. If the POC has no connected WhatsApp, the Main
   Manager takes it.
6. The AI writes a reply according to the group's mode:
   - Automatic: sent after a short delay.
   - Approval: saved as a draft for a POC or manager to approve or edit.
   - Manual: the AI does not reply.
7. The reply is sent from the responsible person's WhatsApp number. If that fails, the system
   tries the manager's number and then the number the group was added with.

A thread can be paused so the AI stays silent for that conversation only.

## What the AI reads before replying

- Project description, scope, status and communication instructions (set on the group page)
- The last 60 messages in the group, with times
- Verified updates, with who added them and whether the client was already told
- All tasks of the project: assigned person, status, due date and the latest member note
- Progress updates from POCs, managers and members

The AI knows when the client was last written to, so it gives only what is new. If nothing has
changed it says so and mentions what is still in progress. It says it will check with the team
only when the system has no information on the question. It never names team members or
mentions the dashboard.

Only these count as confirmed: verified updates, tasks with status completed, and the
"completed" part of a POC or manager update. A member's note is treated as a report, not as
confirmation.

## Verified updates

A verified update is a fact that a Main Manager, POC or admin has confirmed, for example
"Contact form bug fixed and deployed". It can be added on the group page and on the project
page. Everyone on the project can see the list, including who added each one and when.

When adding one you choose:

- Internal: saved in the system only.
- Client group: sent to the WhatsApp group.
- Both: saved and sent to the group.

The same three choices are available when posting a normal progress update.

## Dashboard pages

Admin also keeps the original pages: Overview, Client Groups, Approvals, Message Logs and
Team Users. The Client Groups page shows one group at a time with its project context,
verified updates, members and conversation threads.

## Common problems

- Bridge not connecting or no QR code: check that the bridge is running on port 4000 and that
  `BRIDGE_URL` matches. If the phone logged the session out, remove the account and scan again.
- Database error on start: check `DB_PORT` (3306 or 3307), the password, and that MySQL is running.
- Logged out all the time or 401 errors: `JWT_SECRET` must stay the same between restarts.
- Messages always go to the Main Manager: the department has no POC, or the POC's WhatsApp is
  not linked or not connected. Check the POC status on the project page.
- A message gets no reply: the sender must be marked as a client in the group list, the group
  needs a project, and AI must be enabled and not paused.
- Older chat history is missing: use Import older chat history on the group page.
  WhatsApp only gives old history for a short time after linking.