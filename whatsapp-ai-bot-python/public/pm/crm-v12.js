/* v12 additions: edit / delete / restore with confirmation, permission-driven buttons (GET /api/permissions),
   notifications panel with bell + polling, group mappings, readable audit trail, top bar + mobile menu,
   and search / sort / pagination for every long table. Loaded after crm.js and crm-projects.js. */
(function () {
  "use strict";
  const C = window.CRM;
  const { VIEWS, capi, post, patch, put, del, val, num, fmt, modal, options, field, statusPill, isAdmin, isMM, isMgr, isMember, ROLE_LABEL } = C;

  // ------------------------------------------------------------ permissions
  let PERMS = new Set(), permsLoaded = false;
  const can = (a) => PERMS.has(a);
  async function ensurePerms() {
    if (permsLoaded) return;
    try { PERMS = new Set((await capi("/permissions")).actions); permsLoaded = true; } catch (e) { /* not logged in yet */ }
  }
  C.can = can;

  // ------------------------------------------------------- confirm dialogs
  function confirmDialog(title, message, { confirmText = "Delete", danger = true } = {}) {
    return new Promise((resolve) => {
      const bg = document.createElement("div");
      bg.className = "modal-bg show"; bg.setAttribute("role", "alertdialog"); bg.setAttribute("aria-label", title);
      bg.innerHTML = `<div class="modal" style="width:440px"><div class="modal-head"><h3>${esc(title)}</h3></div>
        <div class="modal-body"><p style="margin:0;line-height:1.5">${message}</p></div>
        <div class="modal-foot"><button class="btn secondary" data-x="no">Cancel</button><button class="btn ${danger ? "danger" : ""}" data-x="yes">${esc(confirmText)}</button></div></div>`;
      document.body.appendChild(bg);
      const done = (v) => { bg.remove(); resolve(v); };
      bg.querySelector('[data-x="no"]').onclick = () => done(false);
      bg.querySelector('[data-x="yes"]').onclick = () => done(true);
      bg.addEventListener("keydown", (e) => { if (e.key === "Escape") done(false); });
      bg.querySelector('[data-x="no"]').focus();
    });
  }
  C.confirmDialog = confirmDialog;
  const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message); } };
  const actionBtn = (cls, label, attrs) => `<button class="btn sm ${cls}" ${attrs}>${label}</button>`;

  // ---------------------------------------------------------------- clients
  function clientForm(c = {}, mms = []) {
    return field("Client name", `<input id="cName" value="${esc(c.name || "")}" required/>`) + field("Company", `<input id="cCo" value="${esc(c.company_name || "")}"/>`) +
      field("Phone", `<input id="cPh" value="${esc(c.phone || "")}"/>`) + field("Email", `<input id="cMail" type="email" value="${esc(c.email || "")}"/>`) +
      field("Status", `<select id="cSt">${["lead", "active", "inactive"].map((s) => `<option ${s === (c.status || "active") ? "selected" : ""}>${s}</option>`).join("")}</select>`) +
      field("Main Manager", `<select id="cMM">${options(mms, "id", (u) => u.name, c.main_manager_id, "None")}</select>`) + field("Notes", `<textarea id="cNotes" rows="2">${esc(c.notes || "")}</textarea>`);
  }
  async function editClient(id, done) {
    const [c, users] = await Promise.all([capi("/clients/" + id), capi("/users")]);
    modal("Edit client", clientForm(c, users.filter((u) => u.role === "main_manager")), { onSubmit: async (close) => {
      if (!val("cName")) throw new Error("Client name is required");
      await patch("/clients/" + id, { name: val("cName"), company_name: val("cCo"), phone: val("cPh"), email: val("cMail"), status: val("cSt"), main_manager_id: num(val("cMM")), notes: val("cNotes") });
      close(); toast("Client updated"); done && done(); } });
  }
  async function deleteClient(id, name, done) {
    if (!(await confirmDialog("Delete client?", `<b>${esc(name)}</b> and all of its projects will be hidden. Conversations, tasks and the activity log are kept, and an admin can restore it later.`))) return;
    const r = await del("/clients/" + id); toast(`Client deleted (${r.projects_hidden} project(s) hidden)`); done && done();
  }
  VIEWS["crm-clients"] = async (main) => {
    await ensurePerms();
    const clients = await capi("/clients");
    main.innerHTML = `<div class="page-head"><h1>Clients</h1>${can("client.create") ? '<button class="btn" id="addClient">+ New client</button>' : ""}</div>
      <div class="card"><div class="tbl-wrap"><table><thead><tr><th>Client</th><th>Company</th><th>Main Manager</th><th>Phone</th><th>Projects</th><th>Status</th><th></th></tr></thead><tbody>
      ${clients.map((c) => `<tr><td><span class="link" data-client="${c.id}">${esc(c.name)}</span></td><td>${esc(c.company_name || "")}</td><td>${esc(c.main_manager_name || "NOT ASSIGNED")}</td><td>${esc(c.phone || "")}</td>
        <td>${c.project_count}</td><td>${statusPill(c.status)}</td><td class="row">${can("client.edit") ? actionBtn("secondary", "Edit", `data-cedit="${c.id}"`) : ""}${can("client.delete") ? actionBtn("danger", "Delete", `data-cdel="${c.id}" data-name="${esc(c.name)}"`) : ""}</td></tr>`).join("") || '<tr><td colspan="7" class="empty">No clients yet.</td></tr>'}</tbody></table></div></div>`;
    const again = () => showView("crm-clients");
    main.querySelectorAll("[data-client]").forEach((a) => (a.onclick = () => openClient(Number(a.dataset.client))));
    main.querySelectorAll("[data-cedit]").forEach((b) => (b.onclick = guard(() => editClient(Number(b.dataset.cedit), again))));
    main.querySelectorAll("[data-cdel]").forEach((b) => (b.onclick = guard(() => deleteClient(Number(b.dataset.cdel), b.dataset.name, again))));
    const add = main.querySelector("#addClient");
    if (add) add.onclick = () => { VIEWS.__newClient((c) => openClient(c.id)); };
  };
  // the add-client dialog lives in crm.js; reach it through the old Clients button flow
  VIEWS.__newClient = (done) => window.newClientModal ? window.newClientModal(done) : null;

  const origOpenClient = window.openClient;
  window.openClient = async function (id) {
    await ensurePerms(); await origOpenClient(id);
    const head = document.querySelector("#main .page-head"); if (!head) return;
    const box = document.createElement("div"); box.className = "row";
    if (can("client.edit")) box.insertAdjacentHTML("beforeend", actionBtn("secondary", "Edit client", 'id="ecl"'));
    if (can("client.delete")) box.insertAdjacentHTML("beforeend", actionBtn("danger", "Delete client", 'id="dcl"'));
    head.appendChild(box);
    const c = await capi("/clients/" + id);
    const e = box.querySelector("#ecl"), d = box.querySelector("#dcl");
    if (e) e.onclick = guard(() => editClient(id, () => openClient(id)));
    if (d) d.onclick = guard(() => deleteClient(id, c.name, () => showView("crm-clients")));
  };

  // --------------------------------------------------------------- projects
  async function editProject(p, done) {
    const users = isAdmin() ? await capi("/users") : [];
    modal("Edit project", field("Project name", `<input id="pName" value="${esc(p.name)}"/>`) + field("Description", `<textarea id="pDesc" rows="2">${esc(p.description || "")}</textarea>`) +
      field("Status", `<select id="pSt">${["planning", "active", "on_hold", "completed", "cancelled"].map((s) => `<option ${s === p.status ? "selected" : ""}>${s}</option>`).join("")}</select>`) +
      field("Priority", `<select id="pPr">${["low", "medium", "high", "urgent"].map((s) => `<option ${s === p.priority ? "selected" : ""}>${s}</option>`).join("")}</select>`) +
      field("Target date", `<input id="pDt" type="date" value="${esc(p.target_date ? String(p.target_date).slice(0, 10) : "")}"/>`) +
      (isAdmin() ? field("Main Manager", `<select id="pMM">${options(users.filter((u) => u.role === "main_manager"), "id", (u) => u.name, p.main_manager_id, "Select")}</select>`) : ""),
      { onSubmit: async (close) => {
        if (!val("pName")) throw new Error("Project name is required");
        const body = { name: val("pName"), description: val("pDesc"), status: val("pSt"), priority: val("pPr"), target_date: val("pDt") || null };
        if (isAdmin() && val("pMM")) body.main_manager_id = Number(val("pMM"));
        await patch("/projects/" + p.id, body); close(); toast("Project updated"); done && done(); } });
  }
  const origOpenProject = window.openProject;
  window.openProject = async function (id) {
    await ensurePerms(); await origOpenProject(id);
    const head = document.querySelector("#main .page-head .row"); if (!head) return;
    let p; try { p = (await capi("/projects/" + id)).project; } catch (e) { return; }
    const box = document.createElement("span"); box.className = "row";
    if (can("project.edit") && (isAdmin() || ME.id === p.main_manager_id)) box.insertAdjacentHTML("beforeend", actionBtn("secondary", "Edit", 'id="epr"'));
    if (can("project.delete")) box.insertAdjacentHTML("beforeend", actionBtn("danger", "Delete", 'id="dpr"'));
    head.appendChild(box);
    if (p.group_id && can("group.unmap") && (isAdmin() || ME.id === p.main_manager_id)) {
      const g = [...document.querySelectorAll("#main .card h3")].find((h) => /WhatsApp group/.test(h.textContent));
      if (g) g.parentElement.insertAdjacentHTML("beforeend", actionBtn("secondary", "Unlink group", 'id="ugr" style="margin-left:8px"'));
      const u = document.getElementById("ugr");
      if (u) u.onclick = guard(async () => {
        if (!(await confirmDialog("Unlink WhatsApp group?", `Client messages in this group will no longer be routed to <b>${esc(p.name)}</b>. The group, its members and all history are kept, and other projects using it are not affected.`, { confirmText: "Unlink" }))) return;
        await del(`/projects/${id}/group`); toast("Group unlinked"); openProject(id); });
    }
    const e = box.querySelector("#epr"), d = box.querySelector("#dpr");
    if (e) e.onclick = guard(() => editProject(p, () => openProject(id)));
    if (d) d.onclick = guard(async () => {
      if (!(await confirmDialog("Delete project?", `<b>${esc(p.name)}</b> will be hidden and stop receiving routed messages. Messages, tasks and history are kept and an admin can restore it.`))) return;
      await del("/projects/" + id); toast("Project deleted"); showView("crm-projects"); });
  };

  // ------------------------------- departments / users / tasks: add Delete and Edit
  function afterView(name, fn) {
    const orig = VIEWS[name];
    VIEWS[name] = async (main) => { await ensurePerms(); await orig(main); await fn(main); };
  }
  afterView("crm-departments", (main) => {
    if (!can("department.delete")) return;
    main.querySelectorAll("[data-edit]").forEach((b) => {
      const d = b.closest("tr").children[0].textContent;
      b.insertAdjacentHTML("afterend", ` <button class="btn sm danger" data-ddel="${b.dataset.edit}">Delete</button>`);
      b.parentElement.querySelector("[data-ddel]").onclick = guard(async () => {
        if (!(await confirmDialog("Delete department?", `<b>${esc(d)}</b> will be hidden from the pickers. Projects, tasks and history that used it keep their link, and an admin can restore it.`))) return;
        await del("/departments/" + b.dataset.edit); toast("Department deleted"); showView("crm-departments"); });
    });
  });
  afterView("crm-users", (main) => {
    if (!can("user.delete")) return;
    main.querySelectorAll("[data-edit]").forEach((b) => {
      const n = b.closest("tr").children[0].textContent;
      b.insertAdjacentHTML("afterend", ` <button class="btn sm danger" data-udel="${b.dataset.edit}">Delete</button>`);
      b.parentElement.querySelector("[data-udel]").onclick = guard(async () => {
        if (!(await confirmDialog("Delete user?", `<b>${esc(n)}</b> will no longer be able to log in and their WhatsApp link is removed. Their past tasks, updates and audit entries stay.`))) return;
        await del("/users/" + b.dataset.udel); toast("User deleted"); showView("crm-users"); });
    });
  });
  afterView("crm-tasks", async (main) => {
    if (isMember() || !(can("task.edit") || can("task.delete"))) return;
    const rows = await capi("/tasks");
    const th = main.querySelector("table thead tr"); if (th) th.insertAdjacentHTML("beforeend", "<th></th>");
    main.querySelectorAll("table tbody tr").forEach((tr) => {
      const sel = tr.querySelector("select[data-task]"); if (!sel) return;
      const t = rows.find((x) => x.id === Number(sel.dataset.task)); if (!t) return;
      tr.insertAdjacentHTML("beforeend", `<td class="row">${can("task.edit") ? actionBtn("secondary", "Edit", `data-tedit="${t.id}"`) : ""}${can("task.delete") ? actionBtn("danger", "Delete", `data-tdel="${t.id}"`) : ""}</td>`);
    });
    main.querySelectorAll("[data-tedit]").forEach((b) => (b.onclick = () => {
      const t = rows.find((x) => x.id === Number(b.dataset.tedit));
      modal("Edit task", field("Title", `<input id="teT" value="${esc(t.title)}"/>`) + field("Description", `<textarea id="teD" rows="2">${esc(t.description || "")}</textarea>`) +
        field("Priority", `<select id="teP">${["low", "medium", "high", "urgent"].map((s) => `<option ${s === t.priority ? "selected" : ""}>${s}</option>`).join("")}</select>`) +
        field("Due date", `<input id="teDue" type="date" value="${esc(t.due_date ? String(t.due_date).slice(0, 10) : "")}"/>`), { onSubmit: async (close) => {
          if (!val("teT")) throw new Error("Title is required");
          await patch("/tasks/" + t.id, { title: val("teT"), description: val("teD"), priority: val("teP"), due_date: val("teDue") || null }); close(); toast("Task updated"); showView("crm-tasks"); } });
    }));
    main.querySelectorAll("[data-tdel]").forEach((b) => (b.onclick = guard(async () => {
      if (!(await confirmDialog("Delete task?", "The task is hidden from lists; its history stays in the activity log."))) return;
      await del("/tasks/" + b.dataset.tdel); toast("Task deleted"); showView("crm-tasks"); })));
  });

  // ---------------------------------------------------------- notifications
  VIEWS["crm-notifications"] = async (main) => {
    let unreadOnly = false, offset = 0, rows = [];
    const open = async (n) => {
      if (n.status === "unread") { await post("/notifications/" + n.id + "/read"); n.status = "read"; refreshBell(); }
      if (n.project_id) openProject(n.project_id); else draw();
    };
    const draw = () => {
      main.innerHTML = `<div class="page-head"><h1>Notifications</h1><div class="row"><select id="nFilter"><option value="all">All</option><option value="unread" ${unreadOnly ? "selected" : ""}>Unread only</option></select>
        <button class="btn secondary" id="nAll">Mark all read</button></div></div>
        <div class="card">${rows.map((n) => `<div class="notif-item ${n.status}" data-n="${n.id}" style="cursor:pointer" tabindex="0"><span class="dotn"></span><div style="flex:1">
          <div style="font-weight:600">${esc(n.title || n.kind.replace(/_/g, " "))}</div><div class="muted">${esc(n.message || "")}</div>
          <div class="hint">${n.project_name ? esc(n.project_name) + " &middot; " : ""}${fmt(n.created_at)}</div></div>${n.project_id ? '<span class="link">Open</span>' : ""}</div>`).join("") || '<div class="empty">You are all caught up.</div>'}
          ${rows.length && rows.length % 50 === 0 ? '<div style="text-align:center;margin-top:10px"><button class="btn secondary" id="nMore">Load more</button></div>' : ""}</div>`;
      main.querySelectorAll("[data-n]").forEach((el) => { const go = () => open(rows.find((x) => x.id === Number(el.dataset.n))); el.onclick = go; el.onkeydown = (e) => { if (e.key === "Enter") go(); }; });
      main.querySelector("#nFilter").onchange = async (e) => { unreadOnly = e.target.value === "unread"; offset = 0; await load(); };
      main.querySelector("#nAll").onclick = async () => { await post("/notifications/read-all"); refreshBell(); await load(); };
      const more = main.querySelector("#nMore"); if (more) more.onclick = async () => { offset += 50; rows = rows.concat(await capi(`/notifications?limit=50&offset=${offset}${unreadOnly ? "&unread_only=true" : ""}`)); draw(); };
    };
    const load = async () => { rows = await capi(`/notifications?limit=50&offset=0${unreadOnly ? "&unread_only=true" : ""}`); draw(); };
    await load();
  };

  // ----------------------------------------------------- group mappings view
  VIEWS["crm-mappings"] = async (main) => {
    const groups = await capi("/group-mappings");
    main.innerHTML = `<div class="page-head"><h1>Group mappings</h1></div>
      <p class="hint" style="margin-bottom:12px">One WhatsApp group can serve several projects, and each project has its own departments, POCs and members.</p>
      ${groups.map((g) => `<div class="card"><h3 style="margin-top:0">${esc(g.group_name || g.whatsapp_group_id)}</h3><div class="tbl-wrap"><table><thead><tr><th>Project</th><th>Client</th><th>Department</th><th>POC</th><th>Members</th></tr></thead><tbody>
        ${g.projects.map((p) => p.departments.length ? p.departments.map((d, i) => `<tr>${i === 0 ? `<td rowspan="${p.departments.length}"><span class="link" data-proj="${p.project_id}">${esc(p.project)}</span></td><td rowspan="${p.departments.length}">${esc(p.client)}</td>` : ""}
          <td>${esc(d.department)}</td><td>${esc(d.poc || "none")}</td><td>${esc(d.members.join(", "))}</td></tr>`).join("") : `<tr><td><span class="link" data-proj="${p.project_id}">${esc(p.project)}</span></td><td>${esc(p.client)}</td><td colspan="3" class="muted">No departments yet</td></tr>`).join("")}</tbody></table></div></div>`).join("") || '<div class="card empty">No WhatsApp group is mapped to a project yet.</div>'}`;
    main.querySelectorAll("[data-proj]").forEach((a) => (a.onclick = () => openProject(Number(a.dataset.proj))));
  };

  // ------------------------------------------------ restore deleted (admin)
  VIEWS["crm-deleted"] = async (main) => {
    const kinds = [["client", "clients", "Clients"], ["project", "projects", "Projects"], ["department", "departments", "Departments"], ["user", "users", "Users"]];
    const data = await Promise.all(kinds.map(([k]) => capi("/deleted/" + k)));
    main.innerHTML = `<div class="page-head"><h1>Deleted items</h1></div><p class="hint" style="margin-bottom:12px">Nothing is erased: deleted records are hidden and can be restored here. A project can only be restored after its client.</p>
      ${kinds.map(([k, plural, label], i) => `<div class="card"><h3 style="margin-top:0">${label}</h3>${data[i].map((r) => `<div class="row between" style="margin:6px 0"><span>${esc(r.name)} <span class="muted">deleted ${fmt(r.deleted_at)}</span></span>
        <button class="btn sm secondary" data-restore="${plural}:${r.id}" ${k === "client" ? 'data-wp="1"' : ""}>Restore</button></div>`).join("") || '<p class="muted">Nothing deleted.</p>'}</div>`).join("")}`;
    main.querySelectorAll("[data-restore]").forEach((b) => (b.onclick = guard(async () => {
      const [pl, id] = b.dataset.restore.split(":"); await post(`/${pl}/${id}/restore`, b.dataset.wp ? { with_projects: true } : {}); toast("Restored"); showView("crm-deleted"); })));
  };

  // ---------------------------------------------- readable activity log
  const fv = (v) => (v === null || v === undefined || v === "" ? "empty" : typeof v === "object" ? JSON.stringify(v) : String(v));
  function explain(detail) {
    try {
      const d = JSON.parse(detail);
      if (d && (d.before !== undefined || d.after !== undefined)) {
        const keys = [...new Set([...Object.keys(d.before || {}), ...Object.keys(d.after || {})])];
        return keys.map((k) => `${k}: ${fv((d.before || {})[k])} \u2192 ${fv((d.after || {})[k])}`).join("; ") + (d.note ? ` (${d.note})` : "");
      }
    } catch (e) { /* plain text */ }
    return detail || "";
  }
  VIEWS["crm-activity"] = async (main) => {
    const rows = await capi("/audit?limit=300");
    const actions = [...new Set(rows.map((r) => r.action))].sort();
    main.innerHTML = `<div class="page-head"><h1>Activity log</h1><select id="aFilter"><option value="">All actions</option>${actions.map((a) => `<option>${esc(a)}</option>`).join("")}</select></div>
      <p class="hint" style="margin-bottom:12px">Who did what and when, with the previous and new values. Passwords and tokens are never stored.</p>
      <div class="card"><div class="tbl-wrap"><table><thead><tr><th>When</th><th>Who</th><th>Action</th><th>What changed</th><th>OK</th></tr></thead><tbody>
      ${rows.map((a) => `<tr data-act="${esc(a.action)}"><td class="muted">${fmt(a.created_at)}</td><td>${esc(a.user_name || "system")}</td><td>${esc(a.action)}</td><td>${esc(explain(a.detail))}</td><td>${a.success ? "" : "failed"}</td></tr>`).join("") || '<tr><td colspan="5" class="empty">Nothing yet.</td></tr>'}</tbody></table></div></div>`;
    main.querySelector("#aFilter").onchange = (e) => main.querySelectorAll("tbody tr[data-act]").forEach((tr) => (tr.dataset.hide = e.target.value && tr.dataset.act !== e.target.value ? "1" : "", tr.style.display = tr.dataset.hide ? "none" : ""));
  };

  // -------------------------------------------- top bar, bell, mobile menu
  const app = document.getElementById("app");
  const bellN = document.getElementById("bellN");
  async function refreshBell() {
    if (typeof ME === "undefined" || !ME) return;
    try { const r = await capi("/notifications/unread-count"); bellN.textContent = r.unread > 99 ? "99+" : r.unread; bellN.classList.toggle("show", r.unread > 0); } catch (e) { /* offline */ }
  }
  document.getElementById("bell").onclick = () => showView("crm-notifications");
  document.getElementById("hamburger").onclick = () => app.classList.toggle("nav-open");
  document.getElementById("scrim").onclick = () => app.classList.remove("nav-open");
  document.getElementById("nav").addEventListener("click", (e) => { if (e.target.closest(".nav-item")) app.classList.remove("nav-open"); });
  setInterval(refreshBell, 30000);   // polling keeps the badge current without a socket

  const NAV2 = [["crm-mappings", "Group mappings", "admin,main_manager"], ["crm-deleted", "Deleted items", "admin"]];
  const nav = document.getElementById("nav"), spacer = nav.querySelector(".spacer");
  NAV2.forEach(([v, t, who]) => { const d = document.createElement("div"); d.className = "nav-item"; d.dataset.view = v; d.dataset.who = who; d.textContent = t; d.onclick = () => showView(v); nav.insertBefore(d, spacer); });
  const titleOf = (view) => { const n = document.querySelector(`.nav-item[data-view="${view}"]`); return n ? n.textContent : "Dashboard"; };

  const inner = window.showView;
  window.showView = async function (view) {
    await ensurePerms();
    const r = await inner(view);
    document.getElementById("pageTitle").textContent = titleOf(view);
    if (typeof ME !== "undefined" && ME) document.getElementById("roleChip").textContent = ROLE_LABEL[ME.role] || ME.role;
    document.querySelectorAll(".nav-item[data-who]").forEach((e) => { if (typeof ME !== "undefined" && ME) e.style.display = e.dataset.who.split(",").includes(ME.role) ? "" : "none"; });
    refreshBell();
    return r;
  };
  const t0 = setInterval(() => { if (typeof ME !== "undefined" && ME) { clearInterval(t0); ensurePerms(); refreshBell(); } }, 200);

  // ------------------------------- loading skeleton + table search/sort/pager
  const main = document.getElementById("main");
  const skeleton = '<div class="skeleton" style="height:30px;width:220px;margin-bottom:18px"></div><div class="skeleton" style="height:90px;margin-bottom:14px"></div><div class="skeleton" style="height:220px"></div>';
  function enhance(table) {
    if (table.dataset.enh || table.closest(".modal")) return;
    const body = table.tBodies[0]; if (!body) return;
    const real = [...body.rows].filter((r) => !(r.cells.length === 1 && r.cells[0].hasAttribute("colspan")));
    if (real.length <= 6) return;
    table.dataset.enh = "1";
    const wrap = table.closest(".tbl-wrap") || table.parentElement;
    const tools = document.createElement("div"); tools.className = "tbl-tools";
    tools.innerHTML = '<input type="search" placeholder="Search this table" aria-label="Search this table"/><span class="muted"></span>';
    wrap.parentElement.insertBefore(tools, wrap);
    const pager = document.createElement("div"); pager.className = "pager"; wrap.after(pager);
    let page = 0, q = "", dir = 1, col = -1; const size = 10;
    const apply = () => {
      const vis = real.filter((r) => !q || r.textContent.toLowerCase().includes(q));
      real.forEach((r) => (r.style.display = "none"));
      const pages = Math.max(1, Math.ceil(vis.length / size)); page = Math.min(page, pages - 1);
      vis.slice(page * size, (page + 1) * size).forEach((r) => (r.style.display = ""));
      tools.lastChild.textContent = `${vis.length} of ${real.length}`;
      pager.innerHTML = vis.length > size ? `<button class="btn sm secondary" data-pg="-1" ${page === 0 ? "disabled" : ""}>Prev</button> Page ${page + 1} of ${pages} <button class="btn sm secondary" data-pg="1" ${page >= pages - 1 ? "disabled" : ""}>Next</button>` : "";
      pager.querySelectorAll("[data-pg]").forEach((b) => (b.onclick = () => { page += Number(b.dataset.pg); apply(); }));
    };
    tools.querySelector("input").oninput = (e) => { q = e.target.value.toLowerCase(); page = 0; apply(); };
    [...(table.tHead ? table.tHead.rows[0].cells : [])].forEach((th, i) => {
      if (!th.textContent.trim()) return;
      th.classList.add("sortable"); th.setAttribute("tabindex", "0");
      const sort = () => {
        dir = col === i ? -dir : 1; col = i;
        const key = (r) => { const t = (r.cells[i] ? r.cells[i].textContent : "").trim(); const n = parseFloat(t); return Number.isNaN(n) || !/^[\d.\-%\s]+$/.test(t) ? t.toLowerCase() : n; };
        real.sort((a, b) => (key(a) > key(b) ? 1 : key(a) < key(b) ? -1 : 0) * dir).forEach((r) => body.appendChild(r)); apply();
      };
      th.onclick = sort; th.onkeydown = (e) => { if (e.key === "Enter") sort(); };
    });
    apply();
  }
  let timer = null;
  const obs = new MutationObserver(() => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      obs.disconnect();
      if (main.children.length === 1 && /^Loading/.test(main.textContent.trim())) main.innerHTML = skeleton;
      main.querySelectorAll("table").forEach(enhance);
      obs.observe(main, { childList: true, subtree: true });
    }, 60);
  });
  obs.observe(main, { childList: true, subtree: true });
})();
