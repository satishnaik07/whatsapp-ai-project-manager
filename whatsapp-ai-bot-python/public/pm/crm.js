/* Role-based CRM screens for the Project Manager dashboard.
   Loaded after index.html's own script, so it reuses its globals (TOKEN, ME, esc, toast, setActiveNav, showView, api).
   Roles: admin -> main_manager -> department_poc -> department_member. Each sees only their own menu. */
(function () {
  "use strict";
  const VIEWS = {};
  const isAdmin = () => ME.role === "admin";
  const isMM = () => ME.role === "main_manager";
  const isMgr = () => isAdmin() || isMM();
  const isMember = () => ME.role === "department_member";
  const isPoc = () => ME.role === "department_poc" || ME.role === "poc";
  const ROLE_LABEL = { admin: "Admin", main_manager: "Main Manager", department_poc: "Department POC", department_member: "Department Member", poc: "POC (legacy)" };

  async function capi(path, opts = {}) {
    const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
    if (TOKEN) headers["Authorization"] = "Bearer " + TOKEN;
    const res = await fetch("/api" + path, Object.assign({}, opts, { headers }));
    if (res.status === 401) { logout(); throw new Error("Session expired"); }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(typeof data.detail === "string" ? data.detail : "Request failed (" + res.status + ")");
    return data;
  }
  const post = (p, b) => capi(p, { method: "POST", body: JSON.stringify(b || {}) });
  const patch = (p, b) => capi(p, { method: "PATCH", body: JSON.stringify(b || {}) });
  const put = (p, b) => capi(p, { method: "PUT", body: JSON.stringify(b || {}) });
  const del = (p) => capi(p, { method: "DELETE" });
  const val = (id) => { const e = document.getElementById(id); return e ? e.value.trim() : ""; };
  const num = (v) => (v === "" || v == null ? null : Number(v));
  const fmt = (d) => (d ? String(d).replace("T", " ").slice(0, 16) : "");
  const genPass = () => { const a = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"; let s = ""; const r = crypto.getRandomValues(new Uint32Array(10)); r.forEach((n) => (s += a[n % a.length])); return s; };

  const style = document.createElement("style");
  style.textContent = `
    .bar{height:8px;background:var(--wa-panel-2);border-radius:99px;overflow:hidden;min-width:90px}.bar>i{display:block;height:100%;background:var(--wa-green-light)}
    .pill{display:inline-block;padding:2px 9px;border-radius:99px;font-size:12px;background:var(--wa-panel-2);border:1px solid var(--border)}
    
    .two{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:900px){.two{grid-template-columns:1fr}}
    .tbl-wrap{overflow-x:auto}.crm-card{margin-bottom:16px}.crm-card h3{margin-top:0}
    .link{color:var(--wa-green-light);cursor:pointer;text-decoration:underline}
    .steps{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px}.steps span{font-size:11px;padding:2px 8px;border-radius:99px;border:1px solid var(--border);color:var(--text-dim)}
    .steps span.on{background:var(--wa-green);color:#fff;border-color:var(--wa-green)}
    .chk{display:flex;gap:8px;align-items:center;margin:3px 0;font-weight:400}.chk input{width:auto}
    .todo{display:flex;gap:10px;align-items:center;margin:8px 0}.todo .dot2{width:20px;height:20px;border-radius:50%;border:2px solid var(--border);display:inline-flex;align-items:center;justify-content:center;font-size:12px}
    .todo.done .dot2{background:var(--wa-green);border-color:var(--wa-green);color:#fff}
    .big-qr{width:240px;height:240px;background:#fff;border-radius:8px;padding:8px}
    .msg-in{color:var(--text)}.msg-out{color:var(--wa-green-light)}
    @media print{body *{visibility:hidden}.cred-sheet,.cred-sheet *{visibility:visible}.cred-sheet{position:absolute;left:0;top:0}}
  `;
  document.head.appendChild(style);

  // ------------------------------------------------------------ generic bits
  function modal(title, bodyHtml, { submit = "Save", onSubmit, extraFoot = "", width = 680 } = {}) {
    const bg = document.createElement("div");
    bg.className = "modal-bg show";
    bg.innerHTML = `<div class="modal" style="width:${width}px"><div class="modal-head"><h3>${esc(title)}</h3></div>
      <div class="modal-body">${bodyHtml}<div id="mErr" style="color:var(--danger);font-size:13px;min-height:18px"></div></div>
      <div class="modal-foot">${extraFoot}<button class="btn secondary" data-x="cancel">${onSubmit ? "Cancel" : "Close"}</button>
      ${onSubmit ? `<button class="btn" data-x="ok">${esc(submit)}</button>` : ""}</div></div>`;
    document.body.appendChild(bg);
    const close = () => bg.remove();
    bg.querySelector('[data-x="cancel"]').onclick = close;
    const ok = bg.querySelector('[data-x="ok"]');
    if (ok) ok.onclick = async () => { ok.disabled = true; try { await onSubmit(close); } catch (e) { bg.querySelector("#mErr").textContent = e.message; ok.disabled = false; } };
    return { bg, close, err: (m) => (bg.querySelector("#mErr").textContent = m) };
  }
  const options = (list, vk, lk, sel, blank) =>
    (blank != null ? `<option value="">${esc(blank)}</option>` : "") +
    list.map((o) => `<option value="${esc(o[vk])}" ${String(o[vk]) === String(sel) ? "selected" : ""}>${esc(typeof lk === "function" ? lk(o) : o[lk])}</option>`).join("");
  const field = (label, inner, hint) => `<div class="field"><label>${esc(label)}</label>${hint ? `<p class="hint">${esc(hint)}</p>` : ""}${inner}</div>`;
  const bar = (pct) => `<div class="bar"><i style="width:${pct || 0}%"></i></div>`;
  const statusPill = (s) => `<span class="pill ${["active", "completed", "connected", "routed"].includes(s) ? "ok" : ["blocked", "disconnected", "cancelled"].includes(s) ? "bad" : "warn"}">${esc(s || "")}</span>`;

  // Logins are shown once: this sheet is what the Main Manager / admin hands to each person.
  function credentialsSheet(rows, title = "Logins created") {
    const url = location.origin + "/pm/";
    const text = rows.map((r) => `${r.name} (${r.role}${r.department ? ", " + r.department : ""})\nLogin: ${url}\nUser ID: ${r.username}\nPassword: ${r.password || "(existing login, unchanged)"}`).join("\n\n");
    const m = modal(title, `<p class="hint">Passwords are shown only now. Copy or print this and give each person their own User ID and password.</p>
      <div class="cred-sheet"><div class="tbl-wrap"><table><thead><tr><th>Name</th><th>Role</th><th>Department</th><th>User ID</th><th>Password</th></tr></thead><tbody>
      ${rows.map((r) => `<tr><td>${esc(r.name)}</td><td>${esc(r.role)}</td><td>${esc(r.department || "")}</td><td><code>${esc(r.username)}</code></td><td><code>${r.password ? esc(r.password) : "unchanged"}</code></td></tr>`).join("")}</tbody></table></div>
      <p class="hint">Login page: ${esc(url)}</p></div>`,
      { extraFoot: '<button class="btn secondary" id="credCopy">Copy all</button><button class="btn secondary" id="credPrint">Print</button>', width: 760 });
    m.bg.querySelector("#credCopy").onclick = () => { navigator.clipboard?.writeText(text); toast("Copied"); };
    m.bg.querySelector("#credPrint").onclick = () => window.print();
    return m;
  }

  // ----------------------------------------------------------- shared tables
  function updatesTable(rows) {
    return `<div class="tbl-wrap"><table><thead><tr><th>Project</th><th>Dept</th><th>Update</th><th>Visibility</th><th>By</th><th>When</th></tr></thead><tbody>
      ${(rows || []).map((u) => `<tr><td>${esc(u.project_name)}</td><td>${esc(u.department_name || "")}</td>
        <td>${u.task_title ? `<div class="muted">Task: ${esc(u.task_title)}</div>` : ""}${["completed", "in_progress", "blocked", "next_steps", "summary"].filter((k) => u[k]).map((k) => `<div><b>${k.replace("_", " ")}:</b> ${esc(u[k])}</div>`).join("")}</td>
        <td>${u.visibility === "client" || u.visibility === "both" ? `<span class="pill ${u.sent_to_client ? "ok" : "bad"}">${u.visibility === "both" ? "CRM + client group" : "client group"} ${u.sent_to_client ? "(sent)" : "(not delivered)"}</span>` : '<span class="pill">internal (CRM only)</span>'}</td>
        <td>${esc(u.author_name || "")}</td><td class="muted">${fmt(u.created_at)}</td></tr>`).join("") || '<tr><td colspan="6" class="muted">No updates yet.</td></tr>'}</tbody></table></div>`;
  }
  const TASK_STATUSES = ["todo", "in_progress", "blocked", "review", "completed", "cancelled"];
  function tasksTable(rows, { notes = false } = {}) {
    return `<div class="tbl-wrap"><table><thead><tr><th>Task</th><th>Project</th><th>Dept</th><th>Assignee</th><th>Priority</th><th>Due</th><th>Status</th>${notes ? "<th></th>" : ""}</tr></thead><tbody>
      ${(rows || []).map((t) => `<tr><td>${esc(t.title)}${t.description ? `<div class="muted">${esc(t.description)}</div>` : ""}</td><td>${esc(t.project_name || "")}</td><td>${esc(t.department_name || "")}</td><td>${esc(t.assignee_name || "")}</td>
        <td>${esc(t.priority)}</td><td style="${t.overdue ? "color:var(--danger)" : ""}">${esc(t.due_date ? String(t.due_date).slice(0, 10) : "")}</td>
        <td><select data-task="${t.id}">${TASK_STATUSES.map((s) => `<option ${s === t.status ? "selected" : ""}>${s}</option>`).join("")}</select></td>
        ${notes ? `<td><button class="btn sm secondary" data-note="${t.id}" data-proj="${t.project_id}">Add update</button></td>` : ""}</tr>`).join("") || `<tr><td colspan="${notes ? 8 : 7}" class="muted">No tasks.</td></tr>`}</tbody></table></div>`;
  }
  function bindTaskStatus(root) {
    root.querySelectorAll("select[data-task]").forEach((s) => (s.onchange = async () => { try { await patch("/tasks/" + s.dataset.task, { status: s.value }); toast("Task updated"); } catch (e) { toast(e.message); } }));
  }
  function projectsTable(rows) {
    return `<div class="tbl-wrap"><table><thead><tr><th>Project</th><th>Client</th><th>Main Manager</th><th>WhatsApp group</th><th>Priority</th><th>Status</th></tr></thead><tbody>
      ${(rows || []).map((p) => `<tr><td><span class="link" data-proj="${p.id}">${esc(p.name)}</span></td><td>${esc(p.client_name)}</td><td>${esc(p.main_manager_name || "NOT SET")}</td>
        <td>${esc(p.group_name || "not linked yet")}</td><td>${esc(p.priority)}</td><td>${statusPill(p.status)}</td></tr>`).join("") || '<tr><td colspan="6" class="muted">No projects yet.</td></tr>'}</tbody></table></div>`;
  }
  function bindProjectLinks(root) { root.querySelectorAll("[data-proj]:not(button)").forEach((a) => (a.onclick = () => openProject(Number(a.dataset.proj)))); }
  function accountsTable(rows, editable, users) {
    return `<div class="tbl-wrap"><table><thead><tr><th>Account</th><th>Phone</th><th>Status</th><th>Linked user</th></tr></thead><tbody>
      ${(rows || []).map((a) => `<tr><td>${esc(a.name || a.id.slice(0, 8))}</td><td>${esc(a.phone_number || "")}</td><td>${statusPill(a.status)}</td>
        <td>${editable ? `<select data-acc="${esc(a.id)}">${options(users, "id", (u) => u.name + " (" + (ROLE_LABEL[u.role] || u.role) + ")", a.user_id, "Not linked")}</select>` : esc(a.user_name || "Not linked")}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">No WhatsApp accounts.</td></tr>'}</tbody></table></div>`;
  }

  // ------------------------------------------- task / update modals (shared)
  async function taskModal(onDone, pre) {
    const projects = await capi("/projects");
    if (!projects.length) throw new Error("No projects yet");
    let D = null;
    const m = modal("New task", field("Project", `<select id="tProj">${options(projects, "id", (p) => p.name + " (" + p.client_name + ")", null)}</select>`) +
      field("Department", `<select id="tDept"></select>`) + field("Assign to", `<select id="tWho"></select>`, "Only people in that department can be assigned.") +
      field("Title", `<input id="tTitle"/>`) + field("Priority", `<select id="tPrio">${["low", "medium", "high", "urgent"].map((s) => `<option ${s === "medium" ? "selected" : ""}>${s}</option>`).join("")}</select>`) +
      field("Due date", `<input id="tDue" type="date"/>`) + field("Description", `<textarea id="tDesc" rows="2"></textarea>`),
      { onSubmit: async (close) => { await post(`/projects/${val("tProj")}/tasks`, { title: val("tTitle"), department_id: num(val("tDept")), assigned_user_id: num(val("tWho")), priority: val("tPrio"), due_date: val("tDue") || null, description: val("tDesc") }); close(); toast("Task created"); onDone && onDone(); } });
    const q = (s) => m.bg.querySelector(s);
    const fillWho = () => { const d = D.departments.find((x) => x.department_id === Number(q("#tDept").value)); q("#tWho").innerHTML = options(d ? d.members : [], "user_id", "name", null, "Unassigned"); };
    const loadProject = async () => {
      D = await capi("/projects/" + q("#tProj").value);
      const mine = isMgr() ? D.departments : D.departments.filter((d) => d.poc_user_id === ME.id);
      q("#tDept").innerHTML = options(mine, "department_id", "name", null); fillWho();
    };
    q("#tProj").onchange = () => loadProject().catch((e) => m.err(e.message)); q("#tDept").onchange = fillWho;
    if (pre) q("#tProj").value = pre;
    await loadProject();
  }
  async function updateModal(onDone, pre) {
    const projects = await capi("/projects");
    if (!projects.length) throw new Error("No projects yet");
    let D = null;
    const m = modal("Post update", field("Project", `<select id="uProj">${options(projects, "id", "name", null)}</select>`) +
      field("Department", `<select id="uDept"></select>`) + field("Related task (optional)", `<select id="uTask"></select>`) +
      field("Completed", `<textarea id="uDone" rows="2"></textarea>`) + field("In progress", `<textarea id="uProg" rows="2"></textarea>`) +
      field("Blocked", `<textarea id="uBlock" rows="2"></textarea>`) + field("Next", `<textarea id="uNext" rows="2"></textarea>`) +
      field("Visibility", `<select id="uVis"><option value="internal">Internal: only your team and the Main Manager see it</option><option value="client">Send to the client group (through your WhatsApp)</option><option value="both">Both: save in the CRM and send to the client group</option></select>`, "Client group and Both are posted to the WhatsApp group. Internal stays in the CRM."),
      { submit: "Post", onSubmit: async (close) => {
        const r = await post(`/projects/${val("uProj")}/updates`, { department_id: num(val("uDept")), task_id: num(val("uTask")), completed: val("uDone"), in_progress: val("uProg"), blocked: val("uBlock"), next_steps: val("uNext"), visibility: val("uVis") });
        toast(r.delivery_error ? "Saved, but not sent to client: " + r.delivery_error : r.sent_to_client ? "Posted to the WhatsApp group" : "Update saved"); close(); onDone && onDone(); } });
    const q = (s) => m.bg.querySelector(s);
    const fillTasks = () => { q("#uTask").innerHTML = options(D.tasks.filter((t) => String(t.department_id) === q("#uDept").value), "id", "title", null, "None"); };
    const load = async () => {
      D = await capi("/projects/" + q("#uProj").value);
      q("#uDept").innerHTML = options(D.departments, "department_id", "name", null, isMgr() ? "Whole project" : null); fillTasks();
    };
    q("#uProj").onchange = () => load().catch((e) => m.err(e.message)); q("#uDept").onchange = fillTasks;
    if (pre) q("#uProj").value = pre;
    await load();
  }
  function noteModal(taskId, projectId, onDone) {
    modal("Update this task", field("Progress note", `<textarea id="nNote" rows="4" placeholder="What did you do? What is left? Anything blocking you?"></textarea>`, "Your POC sees this. It is not sent to the client."),
      { submit: "Send to POC", onSubmit: async (close) => { if (!val("nNote")) throw new Error("Write a note first"); await post(`/projects/${projectId}/updates`, { task_id: taskId, in_progress: val("nNote"), visibility: "internal" }); close(); toast("Sent to your POC"); onDone && onDone(); } });
  }

  // =================================================================== VIEWS
  // ------------------------------------------------------------- dashboard
  VIEWS["crm-dashboard"] = async (main) => {
    if (isMM()) return mmDashboard(main);
    if (isAdmin()) return adminDashboard(main);
    const d = await capi("/dashboard/department");
    main.innerHTML = `<div class="page-head"><h1>My department</h1></div>
      <div class="grid-stats"><div class="stat"><div class="num">${d.projects.length}</div><div class="label">My projects</div></div>
        <div class="stat"><div class="num">${d.pending_tasks}</div><div class="label">Pending tasks</div></div>
        <div class="stat"><div class="num">${d.overdue_tasks}</div><div class="label">Overdue tasks</div></div>
        <div class="stat"><div class="num">${d.notifications.length}</div><div class="label">Unread notifications</div></div></div>
      <div class="card crm-card"><h3>My department progress</h3>${d.progress.map((p) => `<div class="row between" style="margin:8px 0"><span style="min-width:90px">${esc(p.name)}</span>${bar(p.progress_pct)}<b>${p.progress_pct}%</b></div>`).join("") || '<p class="muted">You are not in any department yet.</p>'}</div>
      <div class="card crm-card"><h3>My projects</h3>${projectsTable(d.projects)}</div>
      <div class="card crm-card"><h3>Open tasks in my department</h3>${tasksTable(d.tasks)}</div>
      <div class="card crm-card"><h3>Recent updates</h3>${updatesTable(d.recent_updates)}</div>`;
    bindProjectLinks(main); bindTaskStatus(main);
  };

  async function managerStats(main, d) {
    return `<div class="grid-stats">${[["Clients", d.total_clients], ["Projects", d.total_projects], ["Active projects", d.active_projects], ["Pending tasks", d.pending_tasks],
      ["Overdue tasks", d.overdue_tasks], ["Open conversations", d.open_conversations], ["Unresolved messages", d.unresolved_messages]].map(([l, n]) => `<div class="stat"><div class="num">${n}</div><div class="label">${l}</div></div>`).join("")}</div>
    <div class="two"><div class="card crm-card"><h3>Department progress</h3>${d.department_progress.map((p) => `<div class="row between" style="margin:8px 0"><span style="min-width:90px">${esc(p.name)}</span>${bar(p.progress_pct)}<b>${p.progress_pct}%</b></div>`).join("") || '<p class="muted">No tasks yet.</p>'}</div>
      <div class="card crm-card"><h3>POC availability</h3>${d.poc_availability.map((p) => `<div class="row between" style="margin:6px 0"><span>${esc(p.name)}</span><span class="pill ${p.available ? "ok" : "bad"}">${p.available ? "WhatsApp connected" : esc(p.account_status || "WhatsApp not linked")}</span></div>`).join("") || '<p class="muted">No POCs yet.</p>'}</div></div>
    <div class="card crm-card"><h3>Escalations</h3><div class="tbl-wrap"><table><thead><tr><th>Project</th><th>Reason</th><th>Detail</th><th>When</th><th></th></tr></thead><tbody>
      ${d.escalations.map((e) => `<tr><td>${esc(e.project_name)}</td><td>${esc(e.reason)}</td><td>${esc((e.detail || "").slice(0, 80))}</td><td class="muted">${fmt(e.created_at)}</td><td><button class="btn sm secondary" data-resolve="${e.id}">Resolve</button></td></tr>`).join("") || '<tr><td colspan="5" class="muted">No open escalations.</td></tr>'}</tbody></table></div></div>
    <div class="card crm-card"><h3>Recent updates</h3>${updatesTable(d.recent_updates)}</div>`;
  }
  const bindResolve = (main, again) => main.querySelectorAll("[data-resolve]").forEach((b) => (b.onclick = async () => { await post("/escalations/" + b.dataset.resolve + "/resolve"); showView(again); }));

  async function adminDashboard(main) {
    const [d, clients] = await Promise.all([capi("/dashboard/manager"), capi("/clients")]);
    main.innerHTML = `<div class="page-head"><h1>Admin dashboard</h1><button class="btn" id="goNew">+ New client</button></div>
      <div class="card crm-card"><h3>Clients</h3>${clientsTable(clients)}</div>${await managerStats(main, d)}
      <div class="card crm-card"><h3>WhatsApp accounts</h3>${accountsTable(d.whatsapp_accounts, false)}</div>`;
    main.querySelector("#goNew").onclick = () => newClientModal(() => showView("crm-dashboard"));
    bindClientLinks(main); bindResolve(main, "crm-dashboard");
  }

  async function mmDashboard(main) {
    const [mw, deps, projects, d] = await Promise.all([capi("/my/whatsapp"), capi("/departments"), capi("/projects"), capi("/dashboard/manager")]);
    const linked = mw.account && mw.account.status === "connected";
    const needGroup = projects.filter((p) => !p.group_id);
    const byClient = {};
    projects.forEach((p) => (byClient[p.client_name] = byClient[p.client_name] || []).push(p));
    const todo = (ok, text, view) => `<div class="todo ${ok ? "done" : ""}"><span class="dot2">${ok ? "&#10003;" : ""}</span><span>${text}</span>${ok ? "" : `<a class="link" data-go="${view}">Do this</a>`}</div>`;
    main.innerHTML = `<div class="page-head"><h1>Welcome, ${esc(ME.name || ME.username)}</h1></div>
      <div class="two"><div class="card crm-card"><h3>Getting started</h3>
        ${todo(linked, "1. Link your WhatsApp number", "crm-whatsapp")}
        ${todo(deps.length > 0, "2. Create your departments (Tech, Sales, Execution ...)", "crm-departments")}
        ${todo(projects.length > 0 && needGroup.length === 0, "3. Add the WhatsApp group, POCs and members for each project", "crm-projects")}</div>
      <div class="card crm-card"><h3>My clients</h3>${Object.keys(byClient).map((c) => `<div style="margin:8px 0"><b>${esc(c)}</b>${byClient[c].map((p) => `<div class="row between" style="margin:4px 0 0 12px"><span class="link" data-proj="${p.id}">${esc(p.name)}</span>
          ${p.group_id ? statusPill(p.status) : `<button class="btn sm" data-addgroup="${p.id}">Add group &amp; team</button>`}</div>`).join("")}</div>`).join("") || '<p class="muted">No client is assigned to you yet. Ask the admin.</p>'}</div></div>
      ${await managerStats(main, d)}`;
    main.querySelectorAll("[data-go]").forEach((a) => (a.onclick = () => showView(a.dataset.go)));
    main.querySelectorAll("[data-addgroup]").forEach((b) => (b.onclick = () => openGroupWizard(Number(b.dataset.addgroup))));
    bindProjectLinks(main); bindResolve(main, "crm-dashboard");
  }

  // --------------------------------------------------------------- clients
  function clientsTable(rows) {
    return `<div class="tbl-wrap"><table><thead><tr><th>Client</th><th>Company</th><th>Main Manager</th><th>Phone</th><th>Projects</th><th>Status</th></tr></thead><tbody>
      ${rows.map((c) => `<tr><td><span class="link" data-client="${c.id}">${esc(c.name)}</span></td><td>${esc(c.company_name || "")}</td><td>${esc(c.main_manager_name || "NOT ASSIGNED")}</td><td>${esc(c.phone || "")}</td><td>${c.project_count}</td><td>${statusPill(c.status)}</td></tr>`).join("") || '<tr><td colspan="6" class="muted">No clients yet.</td></tr>'}</tbody></table></div>`;
  }
  const bindClientLinks = (root) => root.querySelectorAll("[data-client]").forEach((a) => (a.onclick = () => openClient(Number(a.dataset.client))));

  // Main Manager picker used by the admin: pick one, or create a new Main Manager login right here.
  function mmPicker(users, sel) {
    const mms = users.filter((u) => u.role === "main_manager");
    return field("Main Manager", `<select id="cMM">${options(mms, "id", (u) => u.name + " (" + u.username + ")", sel, "Select Main Manager")}<option value="__new">+ Create a new Main Manager login</option></select>`) +
      `<div id="newMM" style="display:none">${field("Name", `<input id="mmName"/>`)}${field("Phone", `<input id="mmPhone"/>`)}${field("User ID", `<input id="mmUser"/>`)}${field("Password (8+ characters)", `<div class="row"><input id="mmPass" value="${genPass()}"/><button class="btn sm secondary" type="button" id="mmGen">Generate</button></div>`, "You will see it once after saving, to give to the manager.")}</div>`;
  }
  function bindMMPicker(root) {
    const sel = root.querySelector("#cMM"), box = root.querySelector("#newMM");
    sel.onchange = () => (box.style.display = sel.value === "__new" ? "" : "none");
    root.querySelector("#mmGen").onclick = () => (root.querySelector("#mmPass").value = genPass());
  }
  async function resolveMM() {   // returns {id, creds|null}
    if (val("cMM") !== "__new") return { id: num(val("cMM")), creds: null };
    if (!val("mmName") || !val("mmUser")) throw new Error("Main Manager name and user ID are required");
    const u = await post("/users", { name: val("mmName"), phone: val("mmPhone"), username: val("mmUser"), password: val("mmPass"), role: "main_manager" });
    return { id: u.id, creds: { name: u.name, role: "Main Manager", department: "", username: u.username, password: val("mmPass") } };
  }

  async function newClientModal(done) {
    const users = await capi("/users");
    const m = modal("New client", field("Client name", `<input id="cName"/>`) + field("Company", `<input id="cCo"/>`) + field("Phone", `<input id="cPh"/>`) + field("Email", `<input id="cMail"/>`) +
      field("Notes", `<textarea id="cNotes" rows="2"></textarea>`) + mmPicker(users, null), { submit: "Create client", onSubmit: async (close) => {
        if (!val("cName")) throw new Error("Client name is required");
        const mm = await resolveMM();
        if (!mm.id) throw new Error("Select or create a Main Manager");
        const c = await post("/clients", { name: val("cName"), company_name: val("cCo"), phone: val("cPh"), email: val("cMail"), notes: val("cNotes"), main_manager_id: mm.id });
        close(); if (mm.creds) credentialsSheet([mm.creds], "Main Manager login"); done && done(c);
      } });
    bindMMPicker(m.bg);
  }

  VIEWS["crm-clients"] = async (main) => {
    const clients = await capi("/clients");
    main.innerHTML = `<div class="page-head"><h1>Clients</h1>${isAdmin() ? '<button class="btn" id="addClient">+ New client</button>' : ""}</div>
      <div class="card">${clientsTable(clients)}</div>`;
    bindClientLinks(main);
    const b = main.querySelector("#addClient");
    if (b) b.onclick = () => newClientModal((c) => openClient(c.id));
  };

  window.openClient = async function (id) {
    const main = document.getElementById("main");
    main.innerHTML = "<p class='muted'>Loading…</p>";
    let c; try { c = await capi("/clients/" + id); } catch (e) { main.innerHTML = `<p style="color:var(--danger)">${esc(e.message)}</p>`; return; }
    main.innerHTML = `<div class="page-head"><div><a class="link" id="back">&larr; Clients</a><h1 style="margin-top:6px">${esc(c.name)}</h1>
        <p class="hint">${esc(c.company_name || "")} ${esc(c.email || "")} ${esc(c.phone || "")} &middot; Main Manager: <b>${esc(c.main_manager_name || "NOT ASSIGNED")}</b></p></div>
        ${isAdmin() ? '<button class="btn" id="addProj">+ Add project</button>' : ""}</div>
      ${c.notes ? `<div class="card crm-card"><p>${esc(c.notes)}</p></div>` : ""}
      <div class="card crm-card"><h3>Projects</h3>${projectsTable(c.projects)}</div>`;
    main.querySelector("#back").onclick = () => showView("crm-clients");
    bindProjectLinks(main);
    const add = main.querySelector("#addProj");
    if (add) add.onclick = () => newProjectModal(c, () => openClient(id));
  };

  async function newProjectModal(client, done) {
    const clients = client ? [client] : await capi("/clients");
    const users = await capi("/users");
    const mms = users.filter((u) => u.role === "main_manager");
    const m = modal("New project", field("Client", `<select id="pClient">${options(clients, "id", "name", client && client.id)}</select>`) +
      field("Project name", `<input id="pName"/>`) + field("Description", `<textarea id="pDesc" rows="2"></textarea>`) +
      field("Priority", `<select id="pPrio">${["low", "medium", "high", "urgent"].map((s) => `<option ${s === "medium" ? "selected" : ""}>${s}</option>`).join("")}</select>`) +
      field("Target date", `<input id="pDate" type="date"/>`) +
      field("Main Manager", `<select id="pMM">${options(mms, "id", (u) => u.name, client && client.main_manager_id, "Select Main Manager")}</select>`, "Defaults to the client's Main Manager. The manager then adds the WhatsApp group, POCs and members."),
      { submit: "Create project", onSubmit: async (close) => {
        if (!val("pName")) throw new Error("Project name is required");
        await post("/projects", { client_id: num(val("pClient")), name: val("pName"), description: val("pDesc"), priority: val("pPrio"), target_date: val("pDate") || null, main_manager_id: num(val("pMM")), status: "planning" });
        close(); toast("Project created"); done && done(); } });
    return m;
  }
  window.newProjectModal = newProjectModal;
  window.newClientModal = newClientModal;

  // ----------------------------------------------------------- departments
  VIEWS["crm-departments"] = async (main) => {
    const deps = await capi("/departments");
    const canEdit = (d) => isAdmin() || d.owner_user_id === ME.id;
    main.innerHTML = `<div class="page-head"><h1>Departments</h1>${isMgr() ? '<button class="btn" id="addDep">+ Add department</button>' : ""}</div>
      <p class="hint" style="margin-bottom:12px">${isMM() ? "Create the departments for your projects first (for example Tech, Sales, Execution). You pick them again when you add POCs and members." : ""} The description helps the AI decide which department a client message belongs to, so be specific.</p>
      <div class="card"><div class="tbl-wrap"><table><thead><tr><th>Name</th><th>Description (used by AI routing)</th>${isAdmin() ? "<th>Owner</th>" : ""}<th>Active</th><th></th></tr></thead><tbody>
      ${deps.map((d) => `<tr><td>${esc(d.name)}</td><td>${esc(d.description || "")}</td>${isAdmin() ? `<td>${esc(d.owner_name || "shared")}</td>` : ""}<td>${d.active ? "Yes" : "No"}</td>
        <td>${canEdit(d) ? `<button class="btn sm secondary" data-edit="${d.id}">Edit</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="5" class="muted">${isMM() ? "No departments yet. Click + Add department." : "You are not in a department yet."}</td></tr>`}</tbody></table></div></div>`;
    const form = (d = {}) => field("Name", `<input id="dName" value="${esc(d.name || "")}" placeholder="Tech"/>`) +
      field("Description", `<textarea id="dDesc" rows="3" placeholder="e.g. Website/software development, bugs, APIs, login, dashboards">${esc(d.description || "")}</textarea>`, "Used by the AI classifier.") +
      (d.id ? field("Active", `<select id="dAct"><option value="1" ${d.active ? "selected" : ""}>Yes</option><option value="0" ${d.active ? "" : "selected"}>No</option></select>`) : "");
    const add = main.querySelector("#addDep");
    if (add) add.onclick = () => modal("Add department", form(), { onSubmit: async (close) => { await post("/departments", { name: val("dName"), description: val("dDesc") }); close(); showView("crm-departments"); } });
    main.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => {
      const d = deps.find((x) => x.id === Number(b.dataset.edit));
      modal("Edit department", form(d), { onSubmit: async (close) => { await patch("/departments/" + d.id, { name: val("dName"), description: val("dDesc"), active: Number(val("dAct")) }); close(); showView("crm-departments"); } });
    }));
  };

  // ----------------------------------------------------------------- users
  VIEWS["crm-users"] = async (main) => {
    const [users, deps, accs] = await Promise.all([capi("/users"), capi("/departments"), isAdmin() ? capi("/whatsapp-accounts") : []]);
    main.innerHTML = `<div class="page-head"><h1>${isAdmin() ? "Users" : "My team"}</h1>${isAdmin() ? '<button class="btn" id="addUser">+ Create user</button>' : ""}</div>
      ${isMM() ? '<p class="hint" style="margin-bottom:12px">Logins for your POCs and members are created while you add them to a project (Projects &rarr; Add people). Here you can reset a password or switch someone off.</p>' : ""}
      <div class="card"><div class="tbl-wrap"><table><thead><tr><th>Name</th><th>User ID</th><th>Role</th><th>Phone</th><th>Departments</th><th>WhatsApp</th><th>Active</th><th></th></tr></thead><tbody>
      ${users.map((u) => `<tr><td>${esc(u.name)}</td><td>${esc(u.username)}</td><td>${esc(ROLE_LABEL[u.role] || u.role)}</td><td>${esc(u.phone || "")}</td><td>${u.departments.map((d) => esc(d.name)).join(", ")}</td>
        <td>${u.whatsapp_account ? `${esc(u.whatsapp_account.phone_number || "")} ${statusPill(u.whatsapp_account.status)}` : '<span class="muted">not linked</span>'}</td>
        <td>${u.active ? "Yes" : "No"}</td><td>${u.id === ME.id ? "" : `<button class="btn sm secondary" data-edit="${u.id}">Edit</button>`}</td></tr>`).join("") || '<tr><td colspan="8" class="muted">No users yet.</td></tr>'}</tbody></table></div></div>`;
    const form = (u) => {
      const mine = new Set((u?.departments || []).map((d) => d.id));
      return field("Full name", `<input id="uName" value="${esc(u?.name || "")}"/>`) + (u ? "" : field("User ID", `<input id="uUser"/>`)) +
        field(u ? "New password (leave blank to keep)" : "Password (8+ characters)", `<div class="row"><input id="uPass" value="${u ? "" : genPass()}"/><button class="btn sm secondary" type="button" id="uGen">Generate</button></div>`) +
        field("Phone", `<input id="uPhone" value="${esc(u?.phone || "")}"/>`) + field("Email", `<input id="uMail" value="${esc(u?.email || "")}"/>`) +
        (isAdmin() ? field("Role", `<select id="uRole">${["main_manager", "department_poc", "department_member", "admin"].map((r) => `<option value="${r}" ${u?.role === r ? "selected" : ""}>${ROLE_LABEL[r]}</option>`).join("")}</select>`) +
          field("Departments", deps.map((d) => `<label class="chk"><input type="checkbox" class="uDep" value="${d.id}" ${mine.has(d.id) ? "checked" : ""}/> ${esc(d.name)}</label>`).join("") || '<span class="muted">No departments yet.</span>') +
          field("Linked WhatsApp account", `<select id="uAcc">${options(accs, "id", (a) => (a.phone_number || a.id.slice(0, 8)) + " (" + a.status + ")", u?.whatsapp_account?.account_id, "None")}</select>`) : "") +
        (u ? field("Active", `<select id="uAct"><option value="1" ${u.active ? "selected" : ""}>Yes</option><option value="0" ${u.active ? "" : "selected"}>No</option></select>`) : "");
    };
    const deptIds = () => [...document.querySelectorAll(".uDep:checked")].map((c) => Number(c.value));
    const gen = (m) => { const g = m.bg.querySelector("#uGen"); if (g) g.onclick = () => (m.bg.querySelector("#uPass").value = genPass()); };
    const add = main.querySelector("#addUser");
    if (add) add.onclick = () => { const m = modal("Create user", form(null), { onSubmit: async (close) => {
      const pw = val("uPass");
      await post("/users", { username: val("uUser"), password: pw, name: val("uName"), email: val("uMail"), phone: val("uPhone"), role: val("uRole"), department_ids: deptIds(), account_id: val("uAcc") || null });
      close(); credentialsSheet([{ name: val("uName"), role: ROLE_LABEL[val("uRole")], username: val("uUser"), password: pw }], "Login created"); showView("crm-users"); } }); gen(m); };
    main.querySelectorAll("[data-edit]").forEach((b) => (b.onclick = () => {
      const u = users.find((x) => x.id === Number(b.dataset.edit));
      const m = modal("Edit " + u.name, form(u), { onSubmit: async (close) => {
        const body = { name: val("uName"), email: val("uMail"), phone: val("uPhone"), active: Number(val("uAct")) };
        if (isAdmin()) Object.assign(body, { role: val("uRole"), department_ids: deptIds(), account_id: val("uAcc") || null });
        const pw = val("uPass"); if (pw) body.password = pw;
        await patch("/users/" + u.id, body); close();
        if (pw) credentialsSheet([{ name: u.name, role: ROLE_LABEL[u.role], username: u.username, password: pw }], "New password"); showView("crm-users"); } }); gen(m);
    }));
  };

  // -------------------------------------------------------- WA accounts (admin)
  VIEWS["crm-accounts"] = async (main) => {
    const [accs, users] = await Promise.all([capi("/whatsapp-accounts"), capi("/users")]);
    main.innerHTML = `<div class="page-head"><h1>WhatsApp Accounts</h1></div>
      <p class="hint" style="margin-bottom:12px">Main Managers and POCs link their own number from their dashboard (My WhatsApp); it is attached to them automatically. You can fix a link here.</p>
      <div class="card">${accountsTable(accs, true, users)}</div>`;
    main.querySelectorAll("select[data-acc]").forEach((s) => (s.onchange = async () => { try { await put("/whatsapp-accounts/" + encodeURIComponent(s.dataset.acc) + "/user", { user_id: num(s.value) }); toast("Saved"); showView("crm-accounts"); } catch (e) { toast(e.message); } }));
  };

  // ------------------------------------------------------------ My WhatsApp
  let waTimer = null;
  VIEWS["crm-whatsapp"] = async (main) => {
    clearInterval(waTimer);
    const draw = async () => {
      const st = await capi("/my/whatsapp");
      const a = st.account;
      main.innerHTML = `<div class="page-head"><h1>My WhatsApp</h1></div><div class="card crm-card" style="max-width:560px">
        ${a ? `<p>Linked number: <b>${esc(a.phone_number || "")}</b> ${statusPill(a.status)}</p>
          <p class="hint">${isMM() ? "Your group messages are received through this number." : "When a client asks something about your department, replies are sent from this number."} Your number must be a member of the client's WhatsApp group.</p>
          <button class="btn secondary" id="waOff">Disconnect</button>`
        : st.pending ? `<p><b>Scan this QR code</b></p>${st.pending.qr ? `<img class="big-qr" src="${st.pending.qr}" alt="QR"/>` : "<p class='muted'>Generating QR code…</p>"}
            <p class="hint">On your phone: WhatsApp &rarr; Settings &rarr; Linked devices &rarr; Link a device. This page updates by itself.</p><button class="btn secondary" id="waOff">Cancel</button>`
        : `<p>Link the WhatsApp number you will use for this work. It connects automatically to your role.</p><button class="btn" id="waOn">Link my WhatsApp</button>`}</div>`;
      const on = main.querySelector("#waOn"), off = main.querySelector("#waOff");
      if (on) on.onclick = async () => { try { await post("/my/whatsapp/connect"); draw(); } catch (e) { toast(e.message); } };
      if (off) off.onclick = async () => { await del("/my/whatsapp"); draw(); };
      if (st.pending) waTimer = setTimeout(() => { if (document.getElementById("main") === main && main.querySelector(".big-qr, .muted")) draw().catch(() => {}); }, 2500);
    };
    await draw();
  };

  // ----------------------------------------------------------------- tasks
  VIEWS["crm-tasks"] = async (main) => {
    const rows = await capi("/tasks");
    main.innerHTML = `<div class="page-head"><h1>${isMember() ? "My tasks" : "Tasks"}</h1>${isMember() ? "" : '<button class="btn" id="newTask">+ New task</button>'}</div>
      <p class="hint" style="margin-bottom:12px">${isMember() ? "These are the tasks assigned to you. Change the status as you work, and use Add update to tell your POC what you did." : "Assign tasks to the members of your department. Status changes are reported up: member to POC, POC to Main Manager."}</p>
      <div class="card">${tasksTable(rows, { notes: isMember() })}</div>`;
    bindTaskStatus(main);
    main.querySelectorAll("[data-note]").forEach((b) => (b.onclick = () => noteModal(Number(b.dataset.note), Number(b.dataset.proj), () => showView("crm-tasks"))));
    const n = main.querySelector("#newTask");
    if (n) n.onclick = () => taskModal(() => showView("crm-tasks")).catch((e) => toast(e.message));
  };

  VIEWS["crm-updates"] = async (main) => {
    const rows = await capi("/updates");
    main.innerHTML = `<div class="page-head"><h1>Updates</h1><button class="btn" id="newUpd">+ Post update</button></div>
      <p class="hint" style="margin-bottom:12px">Choose "Send to the client group" only when the client should hear about it. It is posted to the WhatsApp group through your own WhatsApp. Everything else stays internal.</p><div class="card">${updatesTable(rows)}</div>`;
    main.querySelector("#newUpd").onclick = () => updateModal(() => showView("crm-updates")).catch((e) => toast(e.message));
  };

  VIEWS["crm-notifications"] = async (main) => {
    const rows = await capi("/notifications");
    main.innerHTML = `<div class="page-head"><h1>Notifications</h1></div><div class="card"><div class="tbl-wrap"><table><thead><tr><th>Type</th><th>Project</th><th>Message</th><th>When</th><th></th></tr></thead><tbody>
      ${rows.map((n) => `<tr style="${n.status === "unread" ? "font-weight:600" : "opacity:.7"}"><td>${esc(n.kind)}</td><td>${esc(n.project_name || "")}</td><td>${esc(n.message || "")}</td><td class="muted">${fmt(n.created_at)}</td>
        <td>${n.status === "unread" ? `<button class="btn sm secondary" data-read="${n.id}">Mark read</button>` : ""}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">Nothing yet.</td></tr>'}</tbody></table></div></div>`;
    main.querySelectorAll("[data-read]").forEach((b) => (b.onclick = async () => { await post("/notifications/" + b.dataset.read + "/read"); showView("crm-notifications"); }));
  };

  VIEWS["crm-activity"] = async (main) => {
    const rows = await capi("/audit?limit=200");
    main.innerHTML = `<div class="page-head"><h1>Activity log</h1></div><p class="hint" style="margin-bottom:12px">Everything the Main Managers, POCs and members did: logins created, POCs assigned, tasks, updates, messages routed.</p>
      <div class="card"><div class="tbl-wrap"><table><thead><tr><th>When</th><th>Who</th><th>Action</th><th>Detail</th></tr></thead><tbody>
      ${rows.map((a) => `<tr><td class="muted">${fmt(a.created_at)}</td><td>${esc(a.user_name || "system")}</td><td>${esc(a.action)}</td><td>${esc(a.detail || "")}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">Nothing yet.</td></tr>'}</tbody></table></div></div>`;
  };

  window.CRM = { VIEWS, capi, post, patch, put, del, val, num, fmt, genPass, modal, options, field, bar, statusPill, updatesTable, tasksTable, projectsTable,
    bindTaskStatus, bindProjectLinks, isAdmin, isMM, isMgr, isMember, isPoc, ROLE_LABEL, TASK_STATUSES, credentialsSheet, taskModal, updateModal, newProjectModal };
})();
