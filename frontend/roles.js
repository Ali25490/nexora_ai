/* NEXORA AI — three-role platform pages (Manager / Top Manager / Owner),
   approvals, AR/AP, scenarios, reports. Uses globals from app.js
   (S, $, api, esc, fmt$, kpiCard, P, renderPage). Server enforces RBAC;
   nav hiding below is UX-only. */
(function () {
  const NX = {};
  window.NX = NX;

  const row = (cells, header) =>
    "<tr>" + cells.map(c => header ? `<th>${c}</th>` : `<td>${c}</td>`).join("") + "</tr>";
  const money = fmt$;
  const kpiRow = items => `<div class="grid kpis">` + items.map(i =>
    `<div class="card"><h3>${i[0]}</h3><div class="kpi-val">${i[1]}</div>
     <div class="kpi-sub">${i[2] || ""}</div></div>`).join("") + `</div>`;

  const agingTable = ag => `<div class="table-wrap"><table>
    ${row(["Bucket", "Amount"], true)}
    ${[["Current", ag.buckets.current], ["1–30 days", ag.buckets["1_30"]],
       ["31–60 days", ag.buckets["31_60"]], ["61–90 days", ag.buckets["61_90"]],
       ["90+ days", ag.buckets["90_plus"]]].map(b => row([b[0], money(b[1])])).join("")}
    ${row(["<b>Total open</b>", `<b>${money(ag.total_cents)}</b> (${ag.open_count} invoices)`])}
    ${row([`<span class="delta-down">Overdue</span>`, money(ag.overdue_cents)])}
    </table></div>`;

  /* ---------- Manager dashboard ---------- */
  NX.dashManager = async function () {
    const d = await api(`/orgs/${S.org}/dashboards/manager`);
    P.innerHTML = `
    <h2 style="margin-bottom:12px">🧭 Manager Dashboard ${d.department ? `<span class="badge">${esc(d.department)}</span>` : ""}</h2>
    ${kpiRow([
      ["Dept Revenue (30d)", money(d.department_revenue_cents), ""],
      ["Dept Expenses (30d)", money(d.department_expenses_cents), ""],
      ["Dept Profit (30d)", money(d.department_profit_cents), ""],
      ["Budget Utilization", d.budget.utilization_pct != null ? d.budget.utilization_pct + "%" : "n/a",
       `${money(d.budget.actual_cents)} / ${money(d.budget.planned_cents)} (${d.budget.period})`],
      ["Budget Variance", money(d.budget.variance_cents), "planned − actual"]])}
    <div class="grid cols" style="margin-top:14px">
      <div class="card"><h3>Upcoming Receivables (14 days)</h3><div class="table-wrap"><table>
        ${row(["#", "Amount", "Due"], true)}
        ${d.upcoming_ar.map(i => row([esc(i.number), money(i.amount_cents), esc(i.due_date)])).join("") ||
          row(['<span class="muted">None</span>', "", ""])}
      </table></div></div>
      <div class="card"><h3>Upcoming Payables (14 days)</h3><div class="table-wrap"><table>
        ${row(["#", "Amount", "Due", "Status"], true)}
        ${d.upcoming_ap.map(i => row([esc(i.number), money(i.amount_cents), esc(i.due_date),
          `<span class="badge">${esc(i.status)}</span>`])).join("") ||
          row(['<span class="muted">None</span>', "", "", ""])}
      </table></div></div>
    </div>
    <div class="grid cols3" style="margin-top:14px">
      <div class="card"><h3>Open Tasks</h3><div class="table-wrap"><table>
        ${row(["Title", "Priority", "Due"], true)}
        ${d.open_tasks.map(t => row([esc(t.title), `<span class="badge ${esc(t.priority)}">${esc(t.priority)}</span>`,
          esc(t.due_date || "—")])).join("") || row(['<span class="muted">No open tasks</span>', "", ""])}
      </table></div></div>
      <div class="card"><h3>Anomalies to Investigate</h3><div class="table-wrap"><table>
        ${row(["Metric", "Date", "Deviation", "Status"], true)}
        ${d.anomalies.map(a => row([esc(a.metric), esc(a.date),
          `<span class="badge ${esc(a.priority)}">${a.deviation_pct}%</span>`, esc(a.status)])).join("") ||
          row(['<span class="muted">None</span>', "", "", ""])}
      </table></div></div>
      <div class="card"><h3>My Requests</h3><div class="table-wrap"><table>
        ${row(["Title", "Amount", "Status"], true)}
        ${d.my_requests.map(r => row([esc(r.title), money(r.amount_cents),
          `<span class="badge">${esc(r.status)}</span>`])).join("") ||
          row(['<span class="muted">No requests yet</span>', "", ""])}
      </table></div>
      <button class="btn" style="margin-top:10px" onclick="go('approvals')">Submit new request</button></div>
    </div>`;
  };

  /* ---------- Shared consolidated block for Top Manager / Owner ---------- */
  function consolidatedHTML(d) {
    const deptRows = Object.entries(d.departments || {}).map(([name, v]) =>
      row([esc(name), money(v.revenue_cents), money(v.expense_cents),
        `<span class="${v.profit_cents >= 0 ? "delta-up" : "delta-down"}">${money(v.profit_cents)}</span>`])).join("");
    const cash = d.cash_forecast;
    return `
    ${kpiRow([
      ["Revenue (30d)", money(d.revenue_cents), ""],
      ["Gross Profit", money(d.gross_profit_cents), `COGS ${money(d.cogs_cents)}`],
      ["Operating Profit", money(d.operating_profit_cents), ""],
      ["EBITDA", money(d.ebitda_cents), "= operating profit (D&A not separated)"],
      ["Budget Utilization", d.budget.utilization_pct != null ? d.budget.utilization_pct + "%" : "n/a",
       `${money(d.budget.actual_cents)} / ${money(d.budget.planned_cents)}`],
      ["High-priority Anomalies", d.high_anomalies, ""]])}
    <div class="grid cols" style="margin-top:14px">
      <div class="card"><h3>Department Profitability (30d)</h3><div class="table-wrap"><table>
        ${row(["Department", "Revenue", "Expenses", "Profit"], true)}${deptRows}
      </table></div></div>
      <div class="card"><h3>Cash Forecast (13 weeks)</h3>
        ${cash ? `<div class="kpi-val">${money(cash.min_cash_cents)}</div>
          <div class="kpi-sub">projected minimum · opening ${money(cash.opening_cash_cents)}</div>
          ${cash.cash_gap_warning ? `<p class="delta-down" style="margin-top:6px">⚠ Cash gap projected in week ${cash.gap_week}</p>` : '<p class="delta-up" style="margin-top:6px">No cash gap projected</p>'}
          ${d.forecast_accuracy ? `<div class="kpi-sub" style="margin-top:8px">Forecast MAPE ${d.forecast_accuracy.mape ?? "n/a"}% vs naive ${d.forecast_accuracy.baseline_mape ?? "n/a"}%</div>` : ""}`
        : '<p class="muted">Not enough history for a forecast yet.</p>'}
      </div>
    </div>
    <div class="grid cols" style="margin-top:14px">
      <div class="card"><h3>Accounts Receivable Aging</h3>${agingTable(d.ar)}</div>
      <div class="card"><h3>Accounts Payable Aging</h3>${agingTable(d.ap)}</div>
    </div>
    <div class="card" style="margin-top:14px"><h3>Pending Approvals <a href="#" onclick="go('approvals');return false">Open workflow</a></h3>
      <div class="table-wrap"><table>
      ${row(["Title", "Kind", "Amount", "Department", "Status"], true)}
      ${(d.pending_approvals || []).map(r => row([esc(r.title), esc(r.kind), money(r.amount_cents),
        esc(r.department), `<span class="badge">${esc(r.status)}</span>`])).join("") ||
        row(['<span class="muted">Queue is empty</span>', "", "", "", ""])}
      </table></div></div>`;
  }

  NX.dashTop = async function () {
    const d = await api(`/orgs/${S.org}/dashboards/top-manager`);
    P.innerHTML = `<h2 style="margin-bottom:12px">🏢 Top Manager Dashboard</h2>` + consolidatedHTML(d);
  };

  NX.dashOwner = async function () {
    const d = await api(`/orgs/${S.org}/dashboards/owner`);
    P.innerHTML = `<h2 style="margin-bottom:12px">👑 Owner Dashboard</h2>` + consolidatedHTML(d) + `
    <div class="grid cols3" style="margin-top:14px">
      <div class="card"><h3>Net Income</h3><div class="kpi-val">${money(d.net_income_cents)}</div>
        <div class="kpi-sub">${esc(d.net_income_note)}</div></div>
      <div class="card"><h3>Cash Balance / Runway</h3>
        <div class="kpi-val">${d.cash_balance_cents != null ? money(d.cash_balance_cents) : "n/a"}</div>
        <div class="kpi-sub">Runway: ${d.cash_runway_months != null ? d.cash_runway_months + " months" : "profitable / n/a"}</div></div>
      <div class="card"><h3>Working Capital</h3><div class="kpi-val">${money(d.working_capital_cents)}</div>
        <div class="kpi-sub">cash + AR − AP</div></div>
    </div>
    <div class="grid cols" style="margin-top:14px">
      <div class="card"><h3>Approval Policy</h3>
        <p>Top Manager delegated limit: <b>${money(d.approval_policy.top_manager_limit_cents)}</b> (${esc(d.approval_policy.currency)})</p>
        <p class="muted" style="font-size:12px;margin-top:6px">Larger amounts require Owner approval. Self-approval is blocked. All decisions are written to the immutable audit log.</p>
        <button class="btn ghost" style="margin-top:10px" onclick="nxPolicy()">Change limit</button></div>
      <div class="card"><h3>Recent Audit Events</h3><div class="table-wrap"><table>
        ${row(["Action", "Entity", "User", "At"], true)}
        ${(d.audit_recent || []).map(a => row([esc(a.action), esc(a.entity), a.user_id ?? "—",
          esc(a.at.slice(0, 19).replace("T", " "))])).join("")}
      </table></div></div>
    </div>`;
  };

  window.nxPolicy = async () => {
    const v = prompt("New Top Manager delegated limit, USD:", "10000");
    if (v == null) return;
    try {
      await api(`/orgs/${S.org}/approval-policy`, { method: "PUT",
        body: JSON.stringify({ top_manager_limit_cents: Math.round(parseFloat(v) * 100), currency: "USD" }) });
      NX.dashOwner();
    } catch (e) { alert(e.message); }
  };

  /* ---------- Approvals workflow ---------- */
  NX.approvals = async function () {
    const d = await api(`/orgs/${S.org}/approvals`);
    P.innerHTML = `
    <h2 style="margin-bottom:12px">✍️ Approvals <span class="badge">Top Manager limit: ${money(d.policy.top_manager_limit_cents)}</span></h2>
    <div class="card" style="margin-bottom:14px"><h3>Submit request</h3>
      <div class="row">
        <select id="ap-kind" class="icon-btn"><option value="expense">Expense</option>
          <option value="payment">Payment</option><option value="budget">Budget</option></select>
        <input id="ap-title" placeholder="Title" class="icon-btn" style="min-width:200px">
        <input id="ap-amount" type="number" min="0" step="0.01" placeholder="Amount USD" class="icon-btn" style="width:130px">
        <input id="ap-dept" placeholder="Department" class="icon-btn" style="width:130px">
        <button class="btn" onclick="nxSubmit()">Submit</button>
      </div></div>
    <div class="card"><h3>Requests</h3><div class="table-wrap"><table>
      ${row(["ID", "Title", "Kind", "Amount", "Dept", "Status", "Actions"], true)}
      ${d.requests.map(r => row(["#" + r.id, esc(r.title), esc(r.kind), money(r.amount_cents),
        esc(r.department), `<span class="badge">${esc(r.status)}</span>`,
        (r.status === "pending" || r.status === "info_requested")
          ? `<button class="btn ghost" onclick="nxAct(${r.id},'approve')">Approve</button>
             <button class="btn ghost" onclick="nxAct(${r.id},'reject')">Reject</button>
             <button class="btn ghost" onclick="nxAct(${r.id},'request_info')">Info</button>
             <button class="btn ghost" onclick="nxEvents(${r.id})">History</button>`
          : `<button class="btn ghost" onclick="nxEvents(${r.id})">History</button>`])).join("") ||
        row(['<span class="muted">No requests</span>', "", "", "", "", "", ""])}
    </table></div></div>
    <div id="ap-events" style="margin-top:14px"></div>`;
  };
  window.nxSubmit = async () => {
    try {
      await api(`/orgs/${S.org}/approvals`, { method: "POST", body: JSON.stringify({
        kind: $("#ap-kind").value, title: $("#ap-title").value,
        amount_cents: Math.round(parseFloat($("#ap-amount").value || "0") * 100),
        department: $("#ap-dept").value }) });
      NX.approvals();
    } catch (e) { alert(e.message); }
  };
  window.nxAct = async (id, action) => {
    const note = prompt(`Note for '${action}' (optional)`) || "";
    try {
      await api(`/orgs/${S.org}/approvals/${id}/act`, { method: "POST",
        body: JSON.stringify({ action, note }) });
      NX.approvals();
    } catch (e) { alert(e.message); }
  };
  window.nxEvents = async (id) => {
    const ev = await api(`/orgs/${S.org}/approvals/${id}/events`);
    $("#ap-events").innerHTML = `<div class="card"><h3>History — request #${id} (immutable)</h3>
      <div class="table-wrap"><table>${row(["When", "Actor", "Action", "Note"], true)}
      ${ev.map(e => row([esc(e.at.slice(0, 19).replace("T", " ")), e.actor_id, esc(e.action), esc(e.note)])).join("")}
      </table></div></div>`;
  };

  /* ---------- AR / AP ---------- */
  NX.arap = async function () {
    const [ar, ap] = await Promise.all([api(`/orgs/${S.org}/ar`), api(`/orgs/${S.org}/ap`)]);
    const invTable = (invs, kind) => `<div class="table-wrap"><table>
      ${row(["#", "Amount", "Issued", "Due", "Status", "Dept"], true)}
      ${invs.slice(0, 30).map(i => row([esc(i.number), money(i.amount_cents), esc(i.issued_date),
        esc(i.due_date), `<span class="badge">${esc(i.status)}</span>`, esc(i.department)])).join("") ||
        row(['<span class="muted">No invoices</span>', "", "", "", "", ""])}
      </table></div>`;
    P.innerHTML = `<h2 style="margin-bottom:12px">🧾 Accounts Receivable / Payable</h2>
    <div class="grid cols">
      <div class="card"><h3>AR Aging</h3>${agingTable(ar.aging)}</div>
      <div class="card"><h3>AP Aging</h3>${agingTable(ap.aging)}</div>
    </div>
    <div class="grid cols" style="margin-top:14px">
      <div class="card"><h3>Customer Invoices</h3>${invTable(ar.invoices)}</div>
      <div class="card"><h3>Supplier Bills</h3>${invTable(ap.invoices)}</div>
    </div>`;
  };

  /* ---------- Scenarios (what-if engine) ---------- */
  NX.scenarios = async function () {
    const list = await api(`/orgs/${S.org}/scenarios`);
    P.innerHTML = `<h2 style="margin-bottom:12px">🧪 Scenario Comparison</h2>
    <div class="card" style="margin-bottom:14px"><h3>New scenario (computed by the what-if engine)</h3>
      <div class="row">
        <input id="sc-name" placeholder="Name (e.g. Downside −10% revenue)" class="icon-btn" style="min-width:220px">
        <input id="sc-price" type="number" placeholder="price %" class="icon-btn" style="width:90px">
        <input id="sc-vol" type="number" placeholder="volume %" class="icon-btn" style="width:90px">
        <input id="sc-cost" type="number" placeholder="cost %" class="icon-btn" style="width:90px">
        <input id="sc-mkt" type="number" placeholder="marketing %" class="icon-btn" style="width:110px">
        <button class="btn" onclick="nxScenario()">Run & save</button>
      </div>
      <p class="muted" style="font-size:12px;margin-top:6px">Period: last 90 days. Linear assumptions; demand elasticity not modeled.</p></div>
    <div class="card"><h3>Saved scenarios</h3><div class="table-wrap"><table>
      ${row(["", "Name", "Params", "Period"], true)}
      ${list.map(s => row([`<input type="checkbox" class="sc-cmp" value="${s.id}">`, esc(s.name),
        `<span class="muted">${esc(JSON.stringify(s.params))}</span>`,
        esc(s.period.join(" → "))])).join("") || row(["", '<span class="muted">None yet</span>', "", ""])}
      </table></div>
      <button class="btn ghost" style="margin-top:10px" onclick="nxCompare()">Compare selected</button></div>
    <div id="sc-out" style="margin-top:14px"></div>`;
  };
  window.nxScenario = async () => {
    const today = new Date(), s90 = new Date(today.getTime() - 89 * 864e5);
    const iso = d => d.toISOString().slice(0, 10);
    try {
      await api(`/orgs/${S.org}/scenarios`, { method: "POST", body: JSON.stringify({
        name: $("#sc-name").value || "Scenario", period_start: iso(s90), period_end: iso(today),
        params: { price_pct: +$("#sc-price").value || 0, volume_pct: +$("#sc-vol").value || 0,
                  cost_pct: +$("#sc-cost").value || 0, marketing_pct: +$("#sc-mkt").value || 0 } }) });
      NX.scenarios();
    } catch (e) { alert(e.message); }
  };
  window.nxCompare = async () => {
    const ids = [...document.querySelectorAll(".sc-cmp:checked")].map(x => x.value);
    if (!ids.length) return alert("Select at least one scenario");
    const d = await api(`/orgs/${S.org}/scenarios/compare?ids=${ids.join(",")}`);
    const line = (label, fn) => row([label,
      d.base ? fn(d.base) : "—",
      ...d.scenarios.map(s => fn(s.results && s.results.result || {}))], false);
    $("#sc-out").innerHTML = `<div class="card"><h3>Comparison vs base</h3><div class="table-wrap"><table>
      ${row(["Metric", "Base", ...d.scenarios.map(s => esc(s.name))], true)}
      ${line("Revenue", r => money(r.revenue_cents ?? 0))}
      ${line("Gross profit", r => money(r.gross_profit_cents ?? 0))}
      ${line("EBITDA", r => money(r.ebitda_cents ?? 0))}
      ${line("EBITDA margin", r => (r.ebitda_margin_pct ?? "—") + "%")}
      ${line("Break-even revenue", r => r.break_even_revenue_cents != null ? money(r.break_even_revenue_cents) : "n/a")}
      </table></div></div>`;
  };

  /* ---------- Reports ---------- */
  NX.reports = async function () {
    P.innerHTML = `<h2 style="margin-bottom:12px">📄 Reports (permission-scoped CSV)</h2>
    <div class="grid cols3">
      ${[["pnl", "P&L", "Revenue → Net income (your scope)"],
         ["budget-variance", "Budget Variance", "Planned vs actual per department"],
         ["expenses", "Expenses", "90-day expense ledger"],
         ["ar-aging", "AR Aging", "Receivables with aging buckets"],
         ["ap-aging", "AP Aging", "Payables with aging buckets"]].map(r =>
        `<div class="card"><h3>${r[1]}</h3><p class="muted" style="font-size:12px">${r[2]}</p>
         <button class="btn" style="margin-top:10px" onclick="nxDl('${r[0]}')">Download CSV</button></div>`).join("")}
    </div>
    <p class="muted" style="margin-top:12px;font-size:12px">Managers receive department-scoped exports; consolidated exports require Top Manager or Owner. Exports are audit-logged.</p>`;
  };
  window.nxDl = async (name) => {
    const r = await fetch(`/api/orgs/${S.org}/reports/${name}.csv`,
      { headers: { Authorization: "Bearer " + S.token } });
    if (!r.ok) { alert((await r.json().catch(() => ({}))).detail || "Export failed"); return; }
    const b = await r.blob(), a = document.createElement("a");
    a.href = URL.createObjectURL(b); a.download = name + ".csv"; a.click();
  };

  /* ---------- Role-aware nav hiding (UX only; server enforces) ---------- */
  document.addEventListener("DOMContentLoaded", async () => {
    if (!S.token || !S.org) return;
    try {
      const me = await api("/auth/me");
      const m = (me.orgs || []).find(o => o.org_id === S.org);
      const canon = { cfo: "top_manager", analyst: "top_manager", accountant: "manager" }[(m || {}).role]
        || (m || {}).role || "viewer";
      const hide = [];
      if (canon === "manager") hide.push("dash-top", "dash-owner", "billing", "admin", "team");
      if (canon === "top_manager") hide.push("dash-owner", "billing", "admin");
      if (canon === "viewer") hide.push("dash-top", "dash-owner", "billing", "admin", "team", "scenarios");
      document.querySelectorAll(".nav-item").forEach(el => {
        if (hide.includes(el.dataset.page)) el.style.display = "none";
      });
      const badge = document.querySelector("#user-badge");
      if (badge && m) badge.textContent = canon.replace("_", " ");
    } catch (e) { /* token invalid — login flow handles it */ }
  });
})();
