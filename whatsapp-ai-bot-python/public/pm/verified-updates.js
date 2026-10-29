/* Verified updates widget, shared by the group dashboard (index.html) and the project page (crm-projects.js).
   window.mountVerifiedUpdates(element, { load, add, remove?, title? })
     load()      -> { items, can_add, projects, has_group }
     add(body)   -> { sent_to_client, delivery_error, items, can_add, projects, has_group }   body = { text, visibility, project_id? }
   Every entry shows who added it, when, where it went (CRM only / client group / both) and, for the author, a manager or an
   admin, a Remove button. Everybody who can open the project or group sees the whole list. */
(function () {
  "use strict";
  const ROLE = { admin: "Admin", main_manager: "Main Manager", department_poc: "POC", poc: "POC", department_member: "Member" };
  const VIS = {
    internal: "Internal: saved in the CRM, only your team and the Main Manager see it",
    client: "Client group: send to the WhatsApp group (through your WhatsApp)",
    both: "Both: save in the CRM and send to the client group",
  };
  const when = (d) => (d ? String(d).replace("T", " ").slice(0, 16) : "");

  function pill(it) {
    if (it.legacy) return '<span class="pill">older entry</span>';
    if (it.visibility === "internal") return '<span class="pill">Internal (CRM only)</span>';
    const label = it.visibility === "both" ? "CRM + client group" : "Client group";
    return it.sent_to_client ? `<span class="pill ok">${label} (sent)</span>` : `<span class="pill bad" title="Saved in the CRM, but WhatsApp did not deliver it">${label} (not delivered)</span>`;
  }

  window.mountVerifiedUpdates = function (root, cfg) {
    const remove = cfg.remove || ((id) => window.CRM.del("/verified-updates/" + id));
    let state = { items: [], can_add: false, projects: [], has_group: true };

    function draw() {
      const pick = state.projects.length > 1
        ? `<select id="vuProject" title="Which project is this about?"><option value="">Whole group (all projects)</option>${state.projects.map((p) => `<option value="${p.id}">${esc(p.name)}</option>`).join("")}</select>` : "";
      const form = state.can_add ? `
        <div class="field" style="margin-bottom:14px">
          <label for="vuText">Add a verified update</label>
          <div class="hint">A fact you've confirmed (bug fixed, milestone shipped, decision made). The AI only says something is "done" if it is listed here, and it uses these updates when the client asks "any update?".</div>
          <textarea id="vuText" rows="2" style="margin-top:6px" placeholder="e.g. Contact form bug fixed and deployed to production"></textarea>
          <div class="input-row" style="margin-top:8px">
            ${pick}
            <select id="vuVis" title="Where should this update go?">
              <option value="internal">${esc(VIS.internal)}</option>
              <option value="client" ${state.has_group ? "" : "disabled"}>${esc(VIS.client)}</option>
              <option value="both" ${state.has_group ? "" : "disabled"}>${esc(VIS.both)}</option>
            </select>
            <button class="btn" id="vuAdd" style="flex:none">Add</button>
          </div>
          ${state.has_group ? "" : '<div class="hint" style="margin-top:4px">Link a WhatsApp group to this project to send updates to the client.</div>'}
        </div>` : '<p class="hint" style="margin-bottom:10px">Only the Main Manager, POCs and admins can add verified updates. Everyone on the project can read them.</p>';
      const list = state.items.map((it) => `
        <div class="vu-item" style="border:1px solid var(--border);border-radius:10px;padding:10px 12px;margin-bottom:8px">
          <div style="white-space:pre-wrap">${esc(it.text)}</div>
          <div class="row between" style="margin-top:6px;gap:8px;flex-wrap:wrap">
            <span class="muted" style="font-size:12.5px">${it.author_name ? "Added by <b>" + esc(it.author_name) + "</b>" + (it.author_role && ROLE[it.author_role] ? " (" + ROLE[it.author_role] + ")" : "") : "Added by someone"}${it.created_at ? " &middot; " + esc(when(it.created_at)) : ""}${it.project_name ? " &middot; " + esc(it.project_name) : ""}</span>
            <span>${pill(it)}${it.can_delete ? ` <button class="btn secondary sm" data-vu-del="${it.id}" style="margin-left:6px">Remove</button>` : ""}</span>
          </div>
        </div>`).join("") || '<p class="muted">No verified updates yet.</p>';
      root.innerHTML = `<h3 style="margin-top:0">${esc(cfg.title || "Verified updates")}</h3>${form}<div id="vuList">${list}</div>`;
      bind();
    }

    function bind() {
      const add = root.querySelector("#vuAdd");
      if (add) add.onclick = async () => {
        const text = root.querySelector("#vuText").value.trim();
        if (!text) return toast("Write the update first");
        const body = { text, visibility: root.querySelector("#vuVis").value };
        const pj = root.querySelector("#vuProject");
        if (pj && pj.value) body.project_id = Number(pj.value);
        add.disabled = true;
        try {
          const r = await cfg.add(body);
          state = r;
          toast(r.delivery_error ? "Saved, but not sent to the client: " + r.delivery_error
            : r.sent_to_client ? (body.visibility === "both" ? "Saved in the CRM and sent to the client group" : "Sent to the client group")
            : "Verified update saved");
          draw();
        } catch (e) { toast(e.message); add.disabled = false; }
      };
      root.querySelectorAll("[data-vu-del]").forEach((b) => (b.onclick = async () => {
        if (!confirm("Remove this verified update? The AI will stop treating it as confirmed.")) return;
        try { await remove(b.dataset.vuDel); state = await cfg.load(); draw(); toast("Removed"); } catch (e) { toast(e.message); }
      }));
    }

    root.innerHTML = '<p class="muted">Loading…</p>';
    cfg.load().then((s) => { state = s; draw(); }).catch((e) => { root.innerHTML = `<p style="color:var(--danger)">${esc(e.message)}</p>`; });
  };
})();
