/* Projects, the "Add group & team" wizard (Main Manager), and role-based navigation. */
(function () {
  "use strict";
  const C = window.CRM;
  const { VIEWS, capi, post, patch, put, del, val, num, fmt, genPass, modal, options, field, bar, statusPill, updatesTable, tasksTable, projectsTable,
          bindTaskStatus, bindProjectLinks, isAdmin, isMM, isMgr, isMember, ROLE_LABEL, credentialsSheet, taskModal, updateModal, newProjectModal } = C;

  // ---------------------------------------------------------------- list
  VIEWS["crm-projects"] = async (main) => {
    const rows = await capi("/projects");
    main.innerHTML = `<div class="page-head"><h1>Projects</h1>${isAdmin() ? '<button class="btn" id="newProj">+ Create project</button>' : ""}</div>
      ${isMM() ? '<p class="hint" style="margin-bottom:12px">Open a project to add its WhatsApp group, POCs and members.</p>' : ""}<div class="card">${projectsTable(rows)}</div>`;
    bindProjectLinks(main);
    const b = main.querySelector("#newProj");
    if (b) b.onclick = () => newProjectModal(null, () => showView("crm-projects")).catch((e) => toast(e.message));
  };

  // -------------------------------------------------------------- details
  window.openProject = async function (id) {
    const main = document.getElementById("main");
    main.innerHTML = "<p class='muted'>Loading…</p>";
    let D;
    try { D = await capi("/projects/" + id); } catch (e) { main.innerHTML = `<p style="color:var(--danger)">${esc(e.message)}</p>`; return; }
    const p = D.project;
    const manage = isAdmin() || ME.id === p.main_manager_id;
    const myLead = D.departments.some((d) => d.poc_user_id === ME.id);
    const team = manage ? (await capi("/users")).filter((u) => !["admin", "main_manager"].includes(u.role)) : [];
    const allDepts = manage ? (await capi("/departments")).filter((d) => d.active) : [];

    main.innerHTML = `
    <div class="page-head"><div><a class="link" id="back">&larr; Projects</a><h1 style="margin-top:6px">${esc(p.name)}</h1>
      <p class="hint">${esc(p.client_name)} &middot; Main Manager: ${esc(p.main_manager_name || "NOT SET")}</p></div>
      <div class="row">${manage ? `<select id="pStatus">${["planning", "active", "on_hold", "completed", "cancelled"].map((s) => `<option ${s === p.status ? "selected" : ""}>${s}</option>`).join("")}</select>
        <select id="pPrio">${["low", "medium", "high", "urgent"].map((s) => `<option ${s === p.priority ? "selected" : ""}>${s}</option>`).join("")}</select>
        ${p.group_id ? `<select id="pMode" title="AI mode">${["auto", "approval", "manual"].map((s) => `<option ${s === p.ai_mode ? "selected" : ""}>${s}</option>`).join("")}</select>` : ""}` : statusPill(p.status)}</div></div>

    <div class="two"><div class="card crm-card"><h3>WhatsApp group</h3>
        ${p.group_id ? `<p><b>${esc(p.group_name)}</b></p><p class="muted">AI mode: ${esc(p.ai_mode || "")} &middot; Client members: ${D.client_members.map((m) => esc(m.member_name || m.member_jid)).join(", ") || "none"}</p>${manage ? '<button class="btn sm" id="addPeople">Add POCs / members</button>' : ""}`
          : `<p class="muted">No group linked yet.</p>${manage ? '<button class="btn" id="addGroup">Add group &amp; team</button>' : ""}`}</div>
      <div class="card crm-card"><h3>Progress</h3>${D.progress.map((x) => `<div class="row between" style="margin:8px 0"><span style="min-width:90px">${esc(x.name)}</span>${bar(x.progress_pct)}<b>${x.progress_pct}%</b></div>`).join("") || '<p class="muted">No tasks yet.</p>'}</div></div>

    <div class="card crm-card"><div class="page-head"><h3 style="margin:0">Departments, POCs and members</h3>${manage ? '<button class="btn sm" id="addDept">+ Add department</button>' : ""}</div>
      <div class="tbl-wrap"><table><thead><tr><th>Department</th><th>POC (one per department)</th><th>WhatsApp</th><th>Members</th><th></th></tr></thead><tbody>
      ${D.departments.map((d) => `<tr><td>${esc(d.name)}</td>
        <td>${manage ? `<select data-poc="${d.department_id}">${options(team, "id", (u) => u.name, d.poc_user_id, "No POC")}</select>` : esc(d.poc_name || "none")}</td>
        <td>${d.poc_problem ? `<span class="pill bad" title="Messages for this department go to the Main Manager">${esc(d.poc_problem.replace(/_/g, " "))}</span>` : d.poc_user_id ? '<span class="pill ok">connected</span>' : ""}</td>
        <td>${d.members.map((m) => `<span class="pill">${esc(m.name)}${manage ? ` <a class="link" data-rm="${d.department_id}:${m.user_id}">x</a>` : ""}</span>`).join(" ")}</td>
        <td>${manage ? `<button class="btn sm secondary" data-rmdept="${d.department_id}">Remove</button>` : ""}</td></tr>`).join("") || '<tr><td colspan="5" class="muted">No departments assigned yet. Client messages go to the Main Manager until you add them.</td></tr>'}</tbody></table></div></div>

    <div class="card crm-card"><div class="page-head"><h3 style="margin:0">Tasks</h3>${manage || myLead ? '<button class="btn sm" id="addTask">+ Add task</button>' : ""}</div>${tasksTable(D.tasks)}</div>
    <div class="card crm-card"><div class="page-head"><h3 style="margin:0">Updates</h3><button class="btn sm" id="addUpd">+ Post update</button></div>${updatesTable(D.updates)}</div>
    <div class="card crm-card" id="vuProject"></div>

    <div class="card crm-card"><h3>Conversations</h3><div class="tbl-wrap"><table><thead><tr><th>Thread</th><th>Department</th><th>Responsible</th><th>Routing</th><th>Confidence</th><th>AI</th><th></th></tr></thead><tbody>
      ${D.conversations.map((c) => `<tr><td>${esc(c.thread_key)}</td><td>${esc(c.department_name || "-")}</td><td>${esc(c.poc_name || "-")}</td><td>${statusPill(c.routing_status || "new")}</td>
        <td>${c.routing_confidence != null ? Math.round(c.routing_confidence * 100) + "%" : "-"}</td><td>${c.ai_paused ? '<span class="pill warn">paused</span>' : "on"}</td>
        <td class="row"><button class="btn sm secondary" data-msgs="${c.id}">Messages</button><button class="btn sm secondary" data-route="${c.id}">Routing</button>${manage ? `<button class="btn sm secondary" data-reassign="${c.id}">Reassign</button>` : ""}</td></tr>`).join("") || '<tr><td colspan="7" class="muted">No conversations yet.</td></tr>'}</tbody></table></div></div>
    <div class="card crm-card"><h3>Open escalations</h3>${D.escalations.map((e) => `<div>${esc(e.reason)}: ${esc((e.detail || "").slice(0, 70))}</div>`).join("") || '<p class="muted">None.</p>'}</div>
    ${D.activity_log.length ? `<div class="card crm-card"><h3>Activity log</h3><div class="tbl-wrap"><table><tbody>${D.activity_log.map((a) => `<tr><td class="muted">${fmt(a.created_at)}</td><td>${esc(a.user_name || "system")}</td><td>${esc(a.action)}</td><td>${esc(a.detail || "")}</td></tr>`).join("")}</tbody></table></div></div>` : ""}`;

    const reload = () => openProject(id);
    const $ = (s) => main.querySelector(s);
    const guard = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast(e.message); } };
    $("#back").onclick = () => showView("crm-projects");
    bindTaskStatus(main);
    if ($("#addGroup")) $("#addGroup").onclick = () => openGroupWizard(id);
    if ($("#addPeople")) $("#addPeople").onclick = () => openGroupWizard(id);
    if (manage) {
      $("#pStatus").onchange = guard(async (e) => { await patch("/projects/" + id, { status: e.target.value }); toast("Saved"); });
      $("#pPrio").onchange = guard(async (e) => { await patch("/projects/" + id, { priority: e.target.value }); toast("Saved"); });
      if ($("#pMode")) $("#pMode").onchange = guard(async (e) => { await patch("/projects/" + id, { ai_mode: e.target.value }); toast("AI mode saved"); });
      main.querySelectorAll("[data-poc]").forEach((s) => (s.onchange = guard(async () => { if (!s.value) return toast("Pick a person to replace the POC"); await post(`/projects/${id}/departments/${s.dataset.poc}/poc`, { user_id: Number(s.value) }); toast("POC updated"); reload(); })));
      main.querySelectorAll("[data-rm]").forEach((a) => (a.onclick = guard(async () => { const [d, u] = a.dataset.rm.split(":"); await del(`/projects/${id}/departments/${d}/members/${u}`); reload(); })));
      main.querySelectorAll("[data-rmdept]").forEach((b) => (b.onclick = guard(async () => { if (confirm("Remove this department from the project?")) { await del(`/projects/${id}/departments/${b.dataset.rmdept}`); reload(); } })));
      $("#addDept").onclick = () => {
        const free = allDepts.filter((d) => !D.departments.some((x) => x.department_id === d.id));
        modal("Add department", field("Department", `<select id="adDept">${options(free, "id", "name", null)}</select>`, "Only your own departments are listed. Create more under Departments."),
          { onSubmit: async (close) => { if (!val("adDept")) throw new Error("No department left to add"); await post(`/projects/${id}/departments`, { department_id: Number(val("adDept")) }); close(); reload(); } });
      };
    }
    if ($("#addTask")) $("#addTask").onclick = () => taskModal(reload, id).catch((e) => toast(e.message));
    $("#addUpd").onclick = () => updateModal(reload, id).catch((e) => toast(e.message));
    if (window.mountVerifiedUpdates) window.mountVerifiedUpdates($("#vuProject"), {
      title: "Verified updates",
      load: () => capi(`/projects/${id}/verified-updates`),
      add: (body) => post(`/projects/${id}/verified-updates`, body),
    });
    main.querySelectorAll("[data-route]").forEach((b) => (b.onclick = guard(async () => {
      const h = await capi("/conversations/" + b.dataset.route + "/routing");
      modal("Routing history", `<div class="tbl-wrap"><table><thead><tr><th>When</th><th>Event</th><th>Dept</th><th>POC</th><th>Conf.</th><th>Note</th></tr></thead><tbody>${h.events.map((e) => `<tr><td class="muted">${fmt(e.created_at)}</td><td>${esc(e.event)}</td><td>${esc(e.department_name || "")}</td><td>${esc(e.poc_name || "")}</td><td>${e.confidence != null ? Math.round(e.confidence * 100) + "%" : ""}</td><td>${esc(e.reason || "")}</td></tr>`).join("")}</tbody></table></div>`);
    })));
    main.querySelectorAll("[data-msgs]").forEach((b) => (b.onclick = guard(async () => {
      const tid = b.dataset.msgs, msgs = await api("/threads/" + tid + "/messages"), th = D.conversations.find((c) => c.id === Number(tid));
      const m = modal("Conversation " + (th ? th.thread_key : tid), `<div style="max-height:380px;overflow:auto">${msgs.map((x) => `<div class="${x.direction === "in" ? "msg-in" : "msg-out"}" style="margin:6px 0"><b>${esc(x.sender_name || x.sender_role)}</b> <span class="muted">${fmt(x.created_at)}</span><br>${esc(x.body || "")}</div>`).join("") || '<p class="muted">No messages.</p>'}</div>`,
        { extraFoot: `<button class="btn secondary" id="pauseBtn">${th && th.ai_paused ? "Resume AI" : "Pause AI"}</button>` });
      m.bg.querySelector("#pauseBtn").onclick = guard(async () => { await api("/threads/" + tid + (th && th.ai_paused ? "/resume" : "/pause"), { method: "POST" }); toast("Done"); m.close(); reload(); });
    })));
    main.querySelectorAll("[data-reassign]").forEach((b) => (b.onclick = () => {
      modal("Reassign conversation", field("Department", `<select id="raDept">${options(D.departments, "department_id", "name", null)}</select>`) +
        field("Responsible person (blank = that department's POC)", `<select id="raPoc">${options(team, "id", (u) => u.name, null, "Department POC")}</select>`),
        { onSubmit: async (close) => { await patch("/conversations/" + b.dataset.reassign + "/routing", { department_id: num(val("raDept")), poc_user_id: num(val("raPoc")) }); close(); reload(); } });
    }));
  };

  // ============================================ Add group & team (Main Manager)
  const STEPS = ["WhatsApp group", "Clients & team", "POCs", "Members", "Logins", "Finish"];
  window.openGroupWizard = async function (projectId) {
    let D, deps, mw, accountId;
    try {
      [D, deps] = await Promise.all([capi("/projects/" + projectId), capi("/departments")]);
      if (isAdmin()) { const a = (await capi("/whatsapp-accounts")).find((x) => x.user_id === D.project.main_manager_id); mw = { account: a ? { account_id: a.id, status: a.status, phone_number: a.phone_number } : null }; }
      else mw = await capi("/my/whatsapp");
    } catch (e) { return toast(e.message); }
    const project = D.project, hasGroup = !!project.group_id;
    deps = deps.filter((d) => d.active);
    accountId = mw.account && mw.account.status === "connected" ? mw.account.account_id : null;
    if (!hasGroup && !accountId) return modal("Link WhatsApp first", `<p>The group is read from the Main Manager's own WhatsApp number. Link it first, then come back.</p>`, { extraFoot: '<button class="btn" id="goWa">Go to My WhatsApp</button>' }).bg.querySelector("#goWa").onclick = () => { document.querySelector(".modal-bg:last-child").remove(); showView("crm-whatsapp"); };
    if (!deps.length) return modal("Create departments first", `<p>Create your departments (Tech, Sales, Execution ...) first. You choose them for each POC and member.</p>`, { extraFoot: '<button class="btn" id="goDep">Go to Departments</button>' }).bg.querySelector("#goDep").onclick = () => { document.querySelector(".modal-bg:last-child").remove(); showView("crm-departments"); };

    const W = { step: hasGroup ? 2 : 0, groups: [], group_jid: "", group_name: "", members: [], pocRows: [], memberDept: {}, logins: {}, phones: {}, ai_mode: "approval", activate: project.status !== "active" };
    const nameOf = (m) => (m.name && m.name.trim()) || String(m.phone || m.phoneNumber || m.jid).split("@")[0].replace(/\D/g, "") || m.jid;
    if (hasGroup) W.members = (await capi(`/projects/${projectId}/roster`)).map((m) => Object.assign({}, m, { name: nameOf(m) }));
    const startStep = W.step;
    const existingPoc = {};      // department_id -> poc name already set in this project
    D.departments.forEach((d) => { if (d.poc_user_id) existingPoc[d.department_id] = d.poc_name; });
    const bg = document.createElement("div"); bg.className = "modal-bg show"; document.body.appendChild(bg);
    const byJid = (j) => W.members.find((m) => m.jid === j);
    const digits = (m) => String(W.phones[m.jid] ?? m.phone ?? "").replace(/\D/g, "") || String(m.jid).split("@")[0].replace(/\D/g, "");
    const team = () => W.members.filter((m) => m.role === "internal");
    const assigned = (m) => !!m.department_id;                                  // already in a department of this project
    const pocJids = () => W.pocRows.map((r) => r.jid).filter(Boolean);
    const pocDeptName = (deptId) => { const r = W.pocRows.find((x) => String(x.dept) === String(deptId)); return r && byJid(r.jid) ? byJid(r.jid).name : existingPoc[deptId] || ""; };
    const deptsWithPoc = () => deps.filter((d) => existingPoc[d.id] || W.pocRows.some((r) => String(r.dept) === String(d.id)));
    const deptName = (id) => (deps.find((d) => d.id === Number(id)) || {}).name || "";

    const body = () => {
      if (W.step === 0) return field("Your WhatsApp number", `<input disabled value="${esc(mw.account.phone_number || accountId.slice(0, 8))}"/>`) +
        `<button class="btn sm secondary" id="wFetchG">Find my groups</button>` + field("Group", `<select id="wGroup">${options(W.groups, "jid", (g) => g.name + " (" + g.participantCount + " members)", W.group_jid, "Select the client's group")}</select>`) +
        `<button class="btn sm secondary" id="wFetchM">Fetch members</button><p class="hint" style="margin-top:8px">${W.members.length ? W.members.length + " members fetched." : ""}</p>`;
      if (W.step === 1) return `<p class="hint">Mark who is on the client side and who is on your team. Skip anyone who should be ignored.</p><div class="tbl-wrap"><table><tbody>
        ${W.members.map((m, i) => `<tr><td>${esc(m.name || m.jid)}</td><td class="muted">${esc(m.phone || "")}</td><td><select data-mrole="${i}">${[["client", "Client"], ["internal", "Our team"], ["skip", "Skip"]].map(([v, t]) => `<option value="${v}" ${m.role === v ? "selected" : ""}>${t}</option>`).join("")}</select></td></tr>`).join("")}</tbody></table></div>`;
      if (W.step === 2) {
        const free = team().filter((m) => !assigned(m));
        return `<p class="hint">Pick each POC and the department they lead. A department can have only one POC, so used departments disappear from the other rows. Leave a row blank to unselect.</p>
          ${Object.keys(existingPoc).map((d) => `<p class="muted">Already set: ${esc(deptName(d))} &rarr; ${esc(existingPoc[d])}</p>`).join("")}
          ${W.pocRows.map((r, i) => {
            const takenDepts = new Set([...Object.keys(existingPoc), ...W.pocRows.filter((_, j) => j !== i).map((x) => String(x.dept)).filter(Boolean)]);
            const takenJids = new Set(W.pocRows.filter((_, j) => j !== i).map((x) => x.jid));
            return `<div class="row" style="margin:8px 0;gap:8px"><select data-pj="${i}">${options(free.filter((m) => !takenJids.has(m.jid)), "jid", (m) => (m.name || m.jid) + " (" + (m.phone || "") + ")", r.jid, "Select person")}</select>
              <select data-pd="${i}">${options(deps.filter((d) => !takenDepts.has(String(d.id))), "id", "name", r.dept, "Select department")}</select><button class="btn sm secondary" data-rmrow="${i}">x</button></div>`;
          }).join("")}<button class="btn sm secondary" id="addRow">+ Add POC</button>${!free.length ? '<p class="hint">Everyone on your team is already assigned.</p>' : ""}`;
      }
      if (W.step === 3) {
        const pocSet = new Set(pocJids()); const rest = team().filter((m) => !pocSet.has(m.jid));
        const opts = deptsWithPoc();
        return `<p class="hint">Choose each member's department. The department's POC appears automatically.</p>${!opts.length ? '<p class="muted">Add at least one POC first.</p>' : ""}
          <div class="tbl-wrap"><table><thead><tr><th>Member</th><th>Number</th><th>Department</th><th>POC</th></tr></thead><tbody>
          ${rest.map((m) => assigned(m) ? `<tr><td>${esc(m.name)}</td><td class="muted">${esc(m.phone || "")}</td><td>${esc(m.department)}</td><td>${esc(m.poc_name || "")}</td></tr>`
            : `<tr><td>${esc(m.name || m.jid)}</td><td class="muted">${esc(m.phone || "")}</td><td><select data-md="${esc(m.jid)}">${options(opts, "id", "name", W.memberDept[m.jid], "Not assigned")}</select></td><td>${esc(W.memberDept[m.jid] ? pocDeptName(W.memberDept[m.jid]) : "")}</td></tr>`).join("") || '<tr><td colspan="4" class="muted">No other team members.</td></tr>'}</tbody></table></div>`;
      }
      if (W.step === 4) {
        const people = peopleList();
        return `<p class="hint">Each person gets a User ID and password for their own dashboard. If the number already has a login it is reused.</p>${!people.length ? '<p class="muted">Nobody to create yet. Go back and assign POCs or members.</p>' : ""}
          <div class="tbl-wrap"><table><thead><tr><th>Name</th><th>Role</th><th>Department</th><th>Number</th><th>User ID</th><th>Password</th></tr></thead><tbody>
          ${people.map((p, i) => { const m = p.m; const L = W.logins[m.jid] || (W.logins[m.jid] = { u: "", p: genPass() });
            return `<tr><td>${esc(m.name)}</td><td>${p.is_poc ? "POC" : "Member"}</td><td>${esc(deptName(p.dept))}</td>
              <td><input data-ph="${esc(m.jid)}" value="${esc(digits(m))}" style="width:130px"/></td>
              ${m.user_id ? `<td colspan="2" class="muted">existing login: ${esc(m.username)}</td>` : `<td><input data-lu="${esc(m.jid)}" value="${esc(L.u)}" placeholder="auto" style="width:120px"/></td><td><input data-lp="${esc(m.jid)}" value="${esc(L.p)}" style="width:120px"/></td>`}</tr>`; }).join("")}</tbody></table></div>`;
      }
      const n = peopleList();
      return `<div class="card"><p><b>${esc(project.name)}</b> for ${esc(project.client_name)}</p>${hasGroup ? "" : `<p>Group: ${esc(W.group_name)} &middot; clients: ${W.members.filter((m) => m.role === "client").map((m) => esc(m.name)).join(", ") || "none"}</p>`}
        ${n.map((p) => `<p>${p.is_poc ? "POC" : "Member"}: ${esc(p.m.name)} &rarr; ${esc(deptName(p.dept))}</p>`).join("") || "<p class='muted'>No people to add.</p>"}</div>` +
        (hasGroup ? "" : field("AI replies", `<select id="wAi">${["approval", "auto", "manual"].map((s) => `<option ${s === W.ai_mode ? "selected" : ""}>${s}</option>`).join("")}</select>`, "approval = POC/manager approves each AI draft. auto = AI sends. manual = AI never sends.")) +
        (project.status !== "active" ? `<label class="chk"><input type="checkbox" id="wAct" ${W.activate ? "checked" : ""}/> Activate project now (client messages start routing)</label>` : "");
    };
    function peopleList() {
      const out = [];
      W.pocRows.filter((r) => r.jid && r.dept).forEach((r) => byJid(r.jid) && out.push({ m: byJid(r.jid), dept: r.dept, is_poc: true }));
      team().forEach((m) => { if (!pocJids().includes(m.jid) && !assigned(m) && W.memberDept[m.jid]) out.push({ m, dept: W.memberDept[m.jid], is_poc: false }); });
      return out;
    }
    function collect() {
      const q = (s) => bg.querySelectorAll(s);
      if (W.step === 0) { const sel = bg.querySelector("#wGroup"); W.group_jid = sel.value; W.group_name = sel.selectedOptions[0] ? sel.selectedOptions[0].textContent.replace(/ \(\d+ members\)$/, "") : ""; }
      if (W.step === 1) q("[data-mrole]").forEach((s) => (W.members[s.dataset.mrole].role = s.value));
      if (W.step === 2) { q("[data-pj]").forEach((s) => (W.pocRows[s.dataset.pj].jid = s.value)); q("[data-pd]").forEach((s) => (W.pocRows[s.dataset.pd].dept = s.value)); }
      if (W.step === 3) q("[data-md]").forEach((s) => { if (s.value) W.memberDept[s.dataset.md] = s.value; else delete W.memberDept[s.dataset.md]; });
      if (W.step === 4) { q("[data-ph]").forEach((i) => (W.phones[i.dataset.ph] = i.value)); q("[data-lu]").forEach((i) => ((W.logins[i.dataset.lu] = W.logins[i.dataset.lu] || {}).u = i.value.trim())); q("[data-lp]").forEach((i) => ((W.logins[i.dataset.lp] = W.logins[i.dataset.lp] || {}).p = i.value.trim())); }
      if (W.step === 5) { if (bg.querySelector("#wAi")) W.ai_mode = bg.querySelector("#wAi").value; if (bg.querySelector("#wAct")) W.activate = bg.querySelector("#wAct").checked; }
    }
    function check() {
      if (W.step === 0 && !W.group_jid) return "Select the client's group";
      if (W.step === 0 && !W.members.length) return "Fetch the group members first";
      if (W.step === 1 && !W.members.some((m) => m.role === "client")) return "Mark at least one member as Client";
      if (W.step === 2 && W.pocRows.some((r) => !r.jid || !r.dept)) return "Every POC row needs a person and a department (or remove the row)";
      if (W.step === 4) for (const p of peopleList()) if (digits(p.m).length < 7) return `Enter a valid phone number for ${p.m.name}`;
      if (W.step === 4) for (const p of peopleList()) { const L = W.logins[p.m.jid]; if (!p.m.user_id && L && L.p && L.p.length < 8) return `Password for ${p.m.name} must be at least 8 characters`; }
    }
    async function finish() {
      const members = W.members.filter((m) => m.role !== "skip").map((m) => ({ jid: m.jid, name: m.name, phone: digits(m), role: m.role }));
      if (!hasGroup) await post(`/projects/${projectId}/group`, { whatsapp_group_id: W.group_jid, group_name: W.group_name, ai_mode: W.ai_mode, members });
      const people = peopleList().map((p) => { const L = W.logins[p.m.jid] || {}; return { jid: p.m.jid, name: p.m.name, phone: digits(p.m), department_id: Number(p.dept), is_poc: p.is_poc, username: p.m.user_id ? undefined : L.u || undefined, password: p.m.user_id ? undefined : L.p || undefined }; });
      let res = { results: [] };
      if (people.length) res = await post(`/projects/${projectId}/team`, { people });
      if (W.activate && project.status !== "active") await patch(`/projects/${projectId}`, { status: "active" });
      bg.remove(); toast("Saved");
      const created = res.results.filter((r) => r.created);
      if (created.length) credentialsSheet(created.map((r) => ({ name: r.name, role: r.role, department: r.department, username: r.username, password: r.password })));
      openProject(projectId);
    }
    function render() {
      const shown = STEPS.map((s, i) => [s, i]).filter(([, i]) => i >= startStep);
      bg.innerHTML = `<div class="modal" style="width:780px"><div class="modal-head"><h3>${hasGroup ? "Add POCs and members" : "Add group &amp; team"} &middot; ${esc(project.name)}</h3>
        <div class="steps" style="margin:10px 0 0">${shown.map(([s, i]) => `<span class="${i === W.step ? "on" : ""}">${i - startStep + 1}. ${s}</span>`).join("")}</div></div>
        <div class="modal-body">${body()}<div id="wErr" style="color:var(--danger);font-size:13px;min-height:18px"></div></div>
        <div class="modal-foot"><button class="btn secondary" id="wCancel">Cancel</button>${W.step > startStep ? '<button class="btn secondary" id="wBack">Back</button>' : ""}
        ${W.step < 5 ? '<button class="btn" id="wNext">Next</button>' : '<button class="btn" id="wGo">Save &amp; create logins</button>'}</div></div>`;
      const $ = (s) => bg.querySelector(s), err = (m) => ($("#wErr").textContent = m);
      $("#wCancel").onclick = () => bg.remove();
      if ($("#wBack")) $("#wBack").onclick = () => { collect(); W.step--; render(); };
      if ($("#wNext")) $("#wNext").onclick = () => { collect(); const m = check(); if (m) return err(m); W.step++; render(); };
      if ($("#wGo")) $("#wGo").onclick = async () => { collect(); $("#wGo").disabled = true; try { await finish(); } catch (e) { err(e.message); $("#wGo").disabled = false; } };
      const again = () => { collect(); render(); };
      if (W.step === 0) {
        $("#wFetchG").onclick = async () => { try { W.groups = await api("/discover-groups?account_id=" + encodeURIComponent(accountId)); if (!W.groups.length) err("No groups found on this number."); render(); } catch (e) { err(e.message); } };
        $("#wFetchM").onclick = async () => { collect(); if (!W.group_jid) return err("Select a group first"); try {
          const ms = await api("/discover-groups/" + encodeURIComponent(W.group_jid) + "/members?account_id=" + encodeURIComponent(accountId));
          W.members = ms.filter((m) => !m.isMe).map((m) => ({ jid: m.jid, name: nameOf(m), phone: m.phoneNumber || "", role: "skip" })); render(); } catch (e) { err(e.message); } };
      }
      if (W.step === 2) {
        bg.querySelectorAll("[data-pj],[data-pd]").forEach((s) => (s.onchange = again));
        bg.querySelectorAll("[data-rmrow]").forEach((b) => (b.onclick = () => { collect(); W.pocRows.splice(Number(b.dataset.rmrow), 1); render(); }));
        $("#addRow").onclick = () => { collect(); W.pocRows.push({ jid: "", dept: "" }); render(); };
      }
      if (W.step === 3) bg.querySelectorAll("[data-md]").forEach((s) => (s.onchange = again));
    }
    render();
  };

  // ---------------------------------------------------- nav and role routing
  const NAV = [
    ["crm-dashboard", "Dashboard", "admin,main_manager,department_poc,poc"],
    ["crm-clients", "Clients", "admin,main_manager"],
    ["crm-projects", "Projects", "admin,main_manager,department_poc,poc"],
    ["crm-departments", "Departments", "admin,main_manager"],
    ["crm-whatsapp", "My WhatsApp", "main_manager,department_poc,poc"],
    ["crm-accounts", "WhatsApp Accounts", "admin"],
    ["crm-users", "Users", "admin,main_manager"],
    ["crm-tasks", "Tasks", "admin,main_manager,department_poc,poc,department_member"],
    ["crm-updates", "Updates", "admin,main_manager,department_poc,poc"],
    ["crm-notifications", "Notifications", "admin,main_manager,department_poc,poc,department_member"],
    ["crm-activity", "Activity log", "admin"],
  ];
  const nav = document.getElementById("nav"), spacer = nav.querySelector(".spacer");
  const label = document.createElement("div");
  label.style.cssText = "padding:10px 18px 4px;font-size:11px;letter-spacing:.08em;color:var(--text-dim);text-transform:uppercase";
  label.textContent = "CRM"; nav.insertBefore(label, spacer);
  NAV.forEach(([v, t, who]) => {
    const d = document.createElement("div"); d.className = "nav-item"; d.dataset.view = v; d.dataset.who = who; d.dataset.label = t; d.textContent = t;
    d.onclick = () => showView(v); nav.insertBefore(d, spacer);
  });
  const home = () => (isMember() ? "crm-tasks" : "crm-dashboard");
  function applyNav() {
    const role = ME.role;
    document.querySelectorAll(".nav-item[data-who]").forEach((e) => {
      e.style.display = e.dataset.who.split(",").includes(role) ? "" : "none";
      if (e.dataset.view === "crm-users") e.textContent = isAdmin() ? "Users" : "My team";
      if (e.dataset.view === "crm-tasks") e.textContent = isMember() ? "My tasks" : "Tasks";
    });
    // the original admin pages: admin keeps all of them; managers and POCs keep only Approvals; members see none
    document.querySelectorAll(".nav-item[data-view]:not([data-who])").forEach((e) => {
      e.style.display = isAdmin() || (e.dataset.view === "approvals" && !isMember()) ? "" : "none";
    });
    label.style.display = isAdmin() ? "" : "none";
  }
  let first = true;
  const navTimer = setInterval(() => { if (typeof ME !== "undefined" && ME) { applyNav(); clearInterval(navTimer); if (first) { first = false; showView(home()); } } }, 150);
  const original = window.showView;
  window.showView = async function (view) {
    if (typeof ME !== "undefined" && ME) applyNav();
    if (first && view === "overview" && ME) { first = false; view = home(); }   // role-specific landing page
    if (!VIEWS[view]) return original(view);
    setActiveNav(view);
    const main = document.getElementById("main");
    main.innerHTML = "<p class='muted'>Loading…</p>";
    try { await VIEWS[view](main); } catch (e) { main.innerHTML = "<p style='color:var(--danger)'>" + esc(e.message) + "</p>"; }
  };
})();
