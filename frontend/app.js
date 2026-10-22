/* NEXORA AI frontend — calls real backend API. All charts drawn on canvas.
   Fixes: responsive canvas (HiDPI + ResizeObserver), AI-insight state machine,
   run-detection debounce, donut layout, table overflow, link behavior. */
const S = { token: localStorage.getItem("nx_token"), org: +localStorage.getItem("nx_org") || null,
            start: null, end: null, running: false };
const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt$ = c => "$" + (c / 100).toLocaleString("en-US", { maximumFractionDigits: 0 });
const fmtPct = (v, invert) => {
  if (v == null || isNaN(v)) return '<span class="muted">n/a</span>';
  const up = v > 0, good = invert ? !up : up;
  return `<span class="${good ? "delta-up" : "delta-down"}">${up ? "▲" : "▼"} ${Math.abs(v)}%</span>`;
};

async function api(path, opts = {}) {
  opts.headers = { "Authorization": "Bearer " + S.token, "Content-Type": "application/json", ...(opts.headers || {}) };
  const r = await fetch("/api" + path, opts);
  if (r.status === 401) { logout(); throw new Error("Session expired — please sign in again"); }
  if (!r.ok) { const e = await r.json().catch(() => ({})); throw new Error(e.detail || `HTTP ${r.status}`); }
  return r.json();
}
const rangeQ = () => (S.start && S.end) ? `?start=${S.start}&end=${S.end}` : "";

function logout() { localStorage.clear(); location.reload(); }
$("#logout").onclick = logout;

/* ---------- Login ---------- */
$("#login-btn").onclick = async () => {
  $("#login-btn").disabled = true;
  try {
    const r = await fetch("/api/auth/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: $("#login-email").value, password: $("#login-pass").value }) });
    if (!r.ok) throw new Error((await r.json()).detail || "Login failed");
    const d = await r.json();
    localStorage.setItem("nx_token", d.token); localStorage.setItem("nx_org", d.org_id);
    location.reload();
  } catch (e) { $("#login-err").textContent = e.message; $("#login-btn").disabled = false; }
};

/* ---------- Navigation / layout controls ---------- */
document.querySelectorAll(".nav-item").forEach(el => el.onclick = () => {
  document.querySelectorAll(".nav-item").forEach(x => x.classList.remove("active"));
  el.classList.add("active");
  window.scrollTo(0, 0);
  renderPage(el.dataset.page);
});
$("#apply-range").onclick = () => {
  const s = $("#date-start").value, e = $("#date-end").value;
  if (s && e && s <= e) { S.start = s; S.end = e; }
  renderPage(document.querySelector(".nav-item.active").dataset.page);
};
$("#side-toggle").onclick = () => {
  $("#app").classList.toggle("collapsed");
  requestAnimationFrame(() => window.dispatchEvent(new Event("resize")));
};

/* ---------- Canvas helpers (HiDPI-aware, resizable) ---------- */
function fitCanvas(cv) {
  const dpr = window.devicePixelRatio || 1;
  const w = cv.clientWidth || cv.parentElement.clientWidth || 300;
  const h = cv.clientHeight || 180;
  if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
    cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
  }
  const c = cv.getContext("2d");
  c.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { c, W: w, H: h };
}
function observeResize(cv, redraw) {
  if (cv._ro) cv._ro.disconnect();
  let t;
  cv._ro = new ResizeObserver(() => { clearTimeout(t); t = setTimeout(redraw, 60); });
  cv._ro.observe(cv);
}

/* ---------- Charts ---------- */
function lineChart(cv, pts, anomalies = [], expected = null) {
  const draw = () => {
    if (!cv.isConnected) return;
    const { c, W, H } = fitCanvas(cv);
    c.clearRect(0, 0, W, H);
    if (!pts || !pts.length) {
      c.fillStyle = "#8b93a7"; c.font = "13px sans-serif"; c.textAlign = "center";
      c.fillText("No data for the selected range", W / 2, H / 2); c.textAlign = "left";
      return;
    }
    const vals = pts.map(p => p.revenue_cents);
    const expVals = expected ? expected.filter(v => v != null) : [];
    const max = Math.max(...vals, ...expVals, 1);
    const padL = 52, padR = 14, padT = 14, padB = 28;
    const denom = Math.max(pts.length - 1, 1);
    const x = i => padL + (W - padL - padR) * i / denom;
    const y = v => H - padB - (H - padT - padB) * v / max;
    // gridlines + y labels
    c.strokeStyle = "#262d3d"; c.fillStyle = "#8b93a7"; c.font = "11px sans-serif"; c.lineWidth = 1;
    for (let g = 0; g <= 4; g++) {
      const v = max * g / 4, yy = y(v);
      c.beginPath(); c.moveTo(padL, yy); c.lineTo(W - padR, yy); c.stroke();
      c.textAlign = "right";
      c.fillText(v >= 1e5 ? "$" + Math.round(v / 100 / 1000) + "K" : "$" + Math.round(v / 100), padL - 6, yy + 4);
      c.textAlign = "left";
    }
    // expected series (dashed, distinguishable from history)
    if (expVals.length === pts.length) {
      c.beginPath(); c.setLineDash([5, 5]);
      expVals.forEach((v, i) => i ? c.lineTo(x(i), y(v)) : c.moveTo(x(0), y(v)));
      c.strokeStyle = "#4f8ef7"; c.lineWidth = 1.5; c.stroke(); c.setLineDash([]);
    }
    // area + line
    c.beginPath();
    pts.forEach((p, i) => i ? c.lineTo(x(i), y(p.revenue_cents)) : c.moveTo(x(i), y(p.revenue_cents)));
    c.strokeStyle = "#f2762e"; c.lineWidth = 2; c.stroke();
    c.lineTo(x(pts.length - 1), y(0)); c.lineTo(x(0), y(0)); c.closePath();
    c.fillStyle = "rgba(242,118,46,.12)"; c.fill();
    // anomaly markers (only real findings intersecting the series)
    (anomalies || []).forEach(a => {
      const i = pts.findIndex(p => p.date === a.date);
      if (i < 0) return;
      c.fillStyle = "rgba(229,83,75,.18)";
      c.fillRect(x(i) - 7, padT, 14, H - padT - padB);
      c.fillStyle = "#e5534b";
      c.beginPath(); c.arc(x(i), y(pts[i].revenue_cents), 4.5, 0, 7); c.fill();
    });
    // x labels
    c.fillStyle = "#8b93a7"; c.font = "10px sans-serif"; c.textAlign = "center";
    const step = Math.max(1, Math.ceil(pts.length / Math.max(2, Math.floor(W / 90))));
    for (let i = 0; i < pts.length; i += step) c.fillText(pts[i].date.slice(5), x(i), H - 8);
    c.textAlign = "left";
  };
  draw();
  observeResize(cv, draw);
}

function donutChart(cv, data) {
  const draw = () => {
    if (!cv.isConnected) return;
    const { c, W, H } = fitCanvas(cv);
    c.clearRect(0, 0, W, H);
    const total = data.reduce((s, d) => s + d.count, 0);
    const cx = W / 2, cy = H / 2, r = Math.min(W, H) / 2 - 6, ir = r * 0.58;
    if (!total) {
      c.strokeStyle = "#262d3d"; c.lineWidth = r - ir;
      c.beginPath(); c.arc(cx, cy, (r + ir) / 2, 0, 7); c.stroke();
      c.fillStyle = "#8b93a7"; c.font = "11px sans-serif"; c.textAlign = "center";
      c.fillText("No data", cx, cy + 4); c.textAlign = "left";
      return;
    }
    let a0 = -Math.PI / 2;
    data.forEach((d, i) => {
      const a1 = a0 + 2 * Math.PI * d.count / total;
      c.beginPath(); c.moveTo(cx, cy); c.arc(cx, cy, r, a0, a1); c.closePath();
      c.fillStyle = segColor(i); c.fill(); a0 = a1;
    });
    c.beginPath(); c.arc(cx, cy, ir, 0, 7); c.fillStyle = "#161b26"; c.fill();
    c.fillStyle = "#e6e9f0"; c.font = "bold 16px sans-serif"; c.textAlign = "center";
    c.fillText(total.toLocaleString(), cx, cy + 1);
    c.fillStyle = "#8b93a7"; c.font = "10px sans-serif";
    c.fillText("users", cx, cy + 15);
    c.textAlign = "left";
  };
  draw();
  observeResize(cv, draw);
}
const SEG_COLORS = ["#4f8ef7", "#f2762e", "#3fb96f", "#9d6bf3", "#e5c07b", "#8b93a7"];
const segColor = i => SEG_COLORS[i % SEG_COLORS.length];

function barsHTML(rows, labelKey, valKey, pctKey) {
  if (!rows || !rows.length) return `<div class="empty-state">No data for the selected range</div>`;
  const max = Math.max(...rows.map(r => r[valKey]), 1);
  return rows.map(r => `<div class="bar-row"><div class="lbl" title="${esc(r[labelKey])}">${esc(r[labelKey])}</div>
    <div class="bar-track"><div class="bar" style="width:${Math.max(r[valKey] / max * 100, 0.5)}%"></div></div>
    <div class="bar-val">${pctKey ? r[pctKey] + "%" : fmt$(r[valKey])}</div></div>`).join("");
}

/* ---------- Pages ---------- */
const P = $("#page");
async function renderPage(page) {
  P.innerHTML = `<p class='muted'><span class="spin"></span> Loading…</p>`;
  try {
    await ({ overview, anomalies, forecast, finance, whatif, cash, explore, cohorts,
      ask, import: importPage, quality, alerts, tasks, metrics, team, billing, admin, "dash-manager": NX.dashManager, "dash-top": NX.dashTop,
      "dash-owner": NX.dashOwner, approvals: NX.approvals, arap: NX.arap,
      scenarios: NX.scenarios, reports: NX.reports }[page])();
    markRefreshed();
  } catch (e) {
    if (e.message !== "Session expired — please sign in again")
      P.innerHTML = `<div class="card"><p class="err">Failed to load: ${esc(e.message)}</p>
        <button class="btn ghost" style="margin-top:10px" onclick="retryPage()">Retry</button></div>`;
  }
}
window.retryPage = () => renderPage(document.querySelector(".nav-item.active").dataset.page);
function markRefreshed() {
  $("#refresh-note").textContent = "Updated " + new Date().toLocaleTimeString();
}

/* ----- AI Insight card: distinct states, no fabricated findings ----- */
function aiInsightHTML(anoms, detection) {
  const head = `<h3>✦ AI Insight ${anoms[0] ? '<span class="badge high">High impact</span>' : ""}</h3>`;
  if (anoms[0]) {
    const a = anoms[0];
    return head + `<b>Unusual revenue ${a.deviation_pct >= 0 ? "spike" : "drop"} on ${esc(a.date)}</b>
      <p class="muted" style="margin:8px 0">Revenue was ${a.deviation_pct}% vs expected
      (${"$" + Math.round(a.actual).toLocaleString()} vs ${"$" + Math.round(a.expected).toLocaleString()}).</p>
      <h3>Contributing factors <span class="muted" style="font-size:10px">correlations, not causation</span></h3>
      ${(a.factors || []).map(f => `<div class="bar-row"><div class="lbl" style="width:150px;flex-basis:150px">${esc(f.factor)}</div>
        <div class="bar-val">${f.change_pct != null ? (f.change_pct > 0 ? "+" : "") + f.change_pct + "%" : "—"}</div></div>`).join("") ||
        '<p class="muted" style="font-size:12px">No factor data for that day.</p>'}
      <div class="muted" style="font-size:12px;margin-top:8px">Detection confidence: ${Math.round(a.confidence * 100)}%</div>
      <div class="conf-bar"><div class="conf-fill" style="width:${Math.round(a.confidence * 100)}%"></div></div>
      <div class="row" style="margin-top:10px">
        <button class="btn ghost" onclick="mkTask(${a.id})">Create task</button>
        <button class="btn ghost" id="detect-btn" onclick="runAnoms()">Re-run detection</button>
      </div>`;
  }
  if (detection && detection.never_run) {
    return head + `<div class="empty-state">
      <p>Analysis has not been executed yet for this organization.</p>
      <button class="btn" style="margin-top:12px" id="detect-btn" onclick="runAnoms()">Detect anomalies</button></div>`;
  }
  return head + `<div class="empty-state">
    <p>Detection completed — no anomalies found in the current data.</p>
    <button class="btn ghost" style="margin-top:12px" id="detect-btn" onclick="runAnoms()">Re-run detection</button></div>`;
}

async function overview() {
  const [kpi, ch, seg, daily, anoms, detection] = await Promise.all([
    api(`/orgs/${S.org}/kpi${rangeQ()}`), api(`/orgs/${S.org}/revenue/by-channel${rangeQ()}`),
    api(`/orgs/${S.org}/users/by-segment`), api(`/orgs/${S.org}/revenue/daily${rangeQ()}`),
    api(`/orgs/${S.org}/anomalies`), api(`/orgs/${S.org}/anomalies/status`)]);
  const inRange = anoms.filter(a => a.date >= S.start && a.date <= S.end);
  const segTotal = seg.reduce((s, x) => s + x.count, 0);
  P.innerHTML = `
  <div class="grid kpis">
    ${kpiCard("Total Revenue", fmt$(kpi.revenue.value_cents), kpi.revenue.pct_change, `vs ${kpi.period.prev_start} – ${kpi.period.prev_end}`)}
    ${kpiCard("Users", kpi.users.total.toLocaleString(), kpi.users.pct_change, `${kpi.users.new} new this period`)}
    ${kpiCard("Conversion Rate", kpi.conversion_rate.value + "%", kpi.conversion_rate.pct_change, `${kpi.conversion_rate.orders} buyers / ${kpi.conversion_rate.base} customers`)}
    ${kpiCard("Avg. Order Value", fmt$(kpi.avg_order_value.value_cents), kpi.avg_order_value.pct_change, `${kpi.avg_order_value.orders} orders`)}
    ${kpiCard("Churn Rate", kpi.churn_rate.value + "%", kpi.churn_rate.pct_change, `${kpi.churn_rate.churned} churned (cumulative)`, true)}
  </div>
  <div class="grid cols" style="margin-top:14px">
    <div class="card"><h3>Revenue Over Time <span class="muted" style="font-size:11px">${S.start} – ${S.end}</span></h3>
      <div class="chart-box"><canvas id="rev-chart" aria-label="Revenue over time chart"></canvas></div>
      ${daily.series.some(p => p.revenue_cents > 0) ? "" : '<p class="muted" style="font-size:12px;margin-top:6px">No completed orders in this range — showing zero baseline, not missing data.</p>'}
    </div>
    <div class="card ai-card">${aiInsightHTML(inRange.length ? inRange : anoms.slice(0, 1), detection)}</div>
  </div>
  <div class="grid cols3" style="margin-top:14px">
    <div class="card"><h3>Revenue by Channel</h3>${barsHTML(ch, "channel", "revenue_cents", "share_pct")}</div>
    <div class="card"><h3>Users by Segment <span class="muted" style="font-size:11px">${segTotal.toLocaleString()} total</span></h3>
      <div class="seg-flex">
        <canvas id="seg-chart" aria-label="Users by segment donut chart"></canvas>
        <div class="seg-legend">${seg.map((s, i) => `<div class="bar-row"><span class="seg-dot" style="background:${segColor(i)}"></span>
          <div class="lbl" style="width:auto;flex:1;text-align:left">${esc(s.segment)}</div>
          <div class="bar-val" style="width:84px;flex-basis:84px">${s.count.toLocaleString()} · ${s.share_pct}%</div></div>`).join("") ||
          '<p class="muted">No customers yet</p>'}</div>
      </div></div>
    <div class="card"><h3>Recent Anomalies <a href="#anomalies" onclick="go('anomalies');return false">View all</a></h3>
      <div class="table-wrap"><table><tr><th>Metric</th><th>Date</th><th>Impact</th><th>Conf.</th></tr>
      ${anoms.slice(0, 6).map(a => `<tr class="clickable" onclick="go('anomalies')"><td>${esc(a.metric)}</td><td>${esc(a.date)}</td>
        <td><span class="badge ${esc(a.priority)}">${esc(a.priority)}</span></td><td>${Math.round(a.confidence * 100)}%</td></tr>`).join("") ||
        "<tr><td colspan=4 class='muted'>None detected yet</td></tr>"}</table></div></div>
  </div>`;
  lineChart($("#rev-chart"), daily.series, daily.anomalies);
  donutChart($("#seg-chart"), seg);
}
const kpiCard = (t, v, d, sub, invert) => `<div class="card"><h3>${t}</h3><div class="kpi-val">${v}</div>
  <div class="kpi-sub">${fmtPct(d, invert)} ${sub}</div></div>`;
window.go = p => { const el = document.querySelector(`[data-page="${p}"]`); if (el) el.click(); };
window.runAnoms = async () => {
  if (S.running) return;                       // prevent duplicate submissions
  S.running = true;
  const btn = document.getElementById("detect-btn") || document.getElementById("run-anoms-btn");
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Running…'; }
  try {
    const r = await api(`/orgs/${S.org}/anomalies/run`, { method: "POST" });
    if (btn) btn.innerHTML = `Done — ${r.created} new`;
    await overview();
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "Retry detection"; }
    const host = btn && btn.closest(".card");
    if (host) host.insertAdjacentHTML("beforeend",
      `<p class="err" style="margin-top:8px">Detection failed: ${esc(e.message)}</p>`);
  } finally { S.running = false; }
};
window.mkTask = async aid => {
  try {
    await api(`/orgs/${S.org}/tasks`, { method: "POST",
      body: JSON.stringify({ title: "Investigate revenue anomaly", anomaly_id: aid, priority: "high" }) });
    alert("Task created");
  } catch (e) { alert("Could not create task: " + e.message); }
};

async function anomalies() {
  const rows = await api(`/orgs/${S.org}/anomalies`);
  P.innerHTML = `<div class="card"><h3>Anomaly Detection
    <button class="btn" id="run-anoms-btn" onclick="runAnomsList()">Run detection</button></h3>
    ${rows.length ? `<div class="table-wrap"><table><tr><th>Metric</th><th>Date</th><th>Actual</th><th>Expected</th><th>Dev.</th><th>Priority</th><th>Status</th><th>Factors</th></tr>
    ${rows.map(a => `<tr><td>${esc(a.metric)}</td><td>${esc(a.date)}</td><td>$${Math.round(a.actual).toLocaleString()}</td>
      <td>$${Math.round(a.expected).toLocaleString()}</td><td>${a.deviation_pct}%</td>
      <td><span class="badge ${esc(a.priority)}">${esc(a.priority)}</span></td><td>${esc(a.status)}</td>
      <td class="muted" style="font-size:11px">${(a.factors || []).map(f => esc(f.factor) + (f.change_pct != null ? ` ${f.change_pct}%` : "")).join("; ")}</td></tr>`).join("")}
    </table></div>`
    : `<div class="empty-state"><p>No anomaly findings yet. Run detection to analyze revenue history.</p></div>`}</div>`;
}
window.runAnomsList = async () => {
  if (S.running) return;
  S.running = true;
  const btn = document.getElementById("run-anoms-btn");
  if (btn) { btn.disabled = true; btn.innerHTML = '<span class="spin"></span> Running…'; }
  try { await api(`/orgs/${S.org}/anomalies/run`, { method: "POST" }); await anomalies(); }
  catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = "Run detection"; }
    P.insertAdjacentHTML("beforeend", `<p class="err" style="margin-top:8px">Detection failed: ${esc(e.message)}</p>`);
  } finally { S.running = false; }
};

async function forecast() {
  const f = await api(`/orgs/${S.org}/forecast/revenue?horizon=30`);
  if (!f.ok) { P.innerHTML = `<div class="card"><div class="empty-state"><p>${esc(f.message || "Forecast unavailable")}</p>
    <p class="muted" style="font-size:12px">History points available: ${f.history_points ?? 0} (need ≥ 21).</p></div></div>`; return; }
  const histPts = f.history.map(h => ({ date: h.date, revenue_cents: h.revenue_cents }));
  const fcPts = f.forecast.map(p => ({ date: p.date, revenue_cents: p.yhat_cents }));
  P.innerHTML = `<div class="card"><h3>Revenue Forecast — ${esc(f.algorithm)}</h3>
    <div class="row" style="margin-bottom:10px">
      <span class="badge">MAPE (backtest, held-out 7d): ${f.mape_pct ?? "n/a"}%</span>
      <span class="badge">Naive baseline MAPE: ${f.baseline_naive_mape_pct ?? "n/a"}%</span>
      <span class="badge ${f.beats_baseline ? "low" : "high"}">${f.beats_baseline ? "beats baseline" : "does NOT beat baseline"}</span>
    </div>
    <div class="chart-box"><canvas id="fc" aria-label="Revenue forecast chart"></canvas></div>
    <p class="muted" style="margin-top:8px;font-size:12px">${esc(f.caveat)} Dashed segment = forecast.</p></div>`;
  lineChart($("#fc"), [...histPts, ...fcPts]);
}

async function finance() {
  const [pnl, bva] = await Promise.all([api(`/orgs/${S.org}/finance/pnl${rangeQ()}`),
    api(`/orgs/${S.org}/finance/budget-vs-actual`)]);
  P.innerHTML = `<div class="grid cols">
  <div class="card"><h3>Income Statement (simplified)</h3>
    <div class="table-wrap"><table>${[["Revenue", pnl.revenue_cents], ["COGS", -pnl.cogs_cents], ["Gross Profit", pnl.gross_profit_cents],
      ["OpEx", -pnl.opex_cents], ["EBITDA", pnl.ebitda_cents]].map(([k, v]) =>
      `<tr><td>${k}</td><td style="text-align:right">${fmt$(v)}</td></tr>`).join("")}
    <tr><td>Gross margin</td><td style="text-align:right">${pnl.gross_margin_pct ?? "n/a"}%</td></tr>
    <tr><td>EBITDA margin</td><td style="text-align:right">${pnl.ebitda_margin_pct ?? "n/a"}%</td></tr></table></div>
    <p class="muted" style="font-size:12px;margin-top:8px">${esc(pnl.note)}</p></div>
  <div class="card"><h3>Budget vs Actual — ${esc(bva.period)}</h3>
    ${bva.rows.length ? `<div class="table-wrap"><table><tr><th>Dept</th><th>Category</th><th>Plan</th><th>Actual</th><th>Var</th><th></th></tr>
    ${bva.rows.map(r => `<tr><td>${esc(r.department)}</td><td>${esc(r.category)}</td><td>${fmt$(r.planned_cents)}</td>
      <td>${fmt$(r.actual_cents)}</td><td>${r.variance_pct ?? "—"}%</td>
      <td><span class="badge ${r.status === "over" ? "high" : "low"}">${r.status}</span></td></tr>`).join("")}</table></div>`
    : `<div class="empty-state">No budgets recorded for ${esc(bva.period)}</div>`}</div></div>`;
}

async function whatif() {
  P.innerHTML = `<div class="card"><h3>What-if Simulator</h3>
  <div class="row">${["price", "volume", "cost", "marketing"].map(k =>
    `<label class="muted">${k} % <input id="wi-${k}" type="number" value="${k === "price" ? 5 : 0}" style="width:70px;background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:6px"></label>`).join("")}
    <button class="btn" id="wi-run">Run scenario</button></div><div id="wi-out" style="margin-top:14px"></div></div>`;
  $("#wi-run").onclick = async () => {
    $("#wi-run").disabled = true;
    try {
      const body = { price_pct: +$("#wi-price").value || 0, volume_pct: +$("#wi-volume").value || 0,
                     cost_pct: +$("#wi-cost").value || 0, marketing_pct: +$("#wi-marketing").value || 0 };
      const r = await api(`/orgs/${S.org}/finance/what-if${rangeQ()}`, { method: "POST", body: JSON.stringify(body) });
      $("#wi-out").innerHTML = `<div class="table-wrap"><table><tr><th></th><th>Base</th><th>Scenario</th><th>Δ</th></tr>
        ${[["Revenue", r.base.revenue_cents, r.result.revenue_cents], ["EBITDA", r.base.ebitda_cents, r.result.ebitda_cents]]
          .map(([k, b, s]) => `<tr><td>${k}</td><td>${fmt$(b)}</td><td>${fmt$(s)}</td><td>${fmt$(s - b)}</td></tr>`).join("")}
        <tr><td>EBITDA margin</td><td>${r.base.ebitda_margin_pct ?? "n/a"}%</td><td>${r.result.ebitda_margin_pct ?? "n/a"}%</td><td></td></tr>
        <tr><td>Break-even revenue</td><td></td><td>${r.result.break_even_revenue_cents ? fmt$(r.result.break_even_revenue_cents) : "n/a"}</td><td></td></tr></table></div>
        <p class="muted" style="font-size:12px;margin-top:8px">${esc(r.note)}</p>`;
    } catch (e) {
      $("#wi-out").innerHTML = `<p class="err">Scenario failed: ${esc(e.message)}</p>`;
    } finally { $("#wi-run").disabled = false; }
  };
}

async function cash() {
  const cf = await api(`/orgs/${S.org}/finance/cash-flow`);
  P.innerHTML = `<div class="card"><h3>13-Week Cash Flow Forecast
    ${cf.cash_gap_warning ? `<span class="badge high">⚠ gap in week ${cf.gap_week}</span>` : `<span class="badge low">no gap projected</span>`}</h3>
    <div class="table-wrap"><table><tr><th>Week</th><th>Start</th><th>Inflow</th><th>Outflow</th><th>Closing cash</th></tr>
    ${cf.weeks.map(w => `<tr><td>${w.week}</td><td>${w.start}</td><td>${fmt$(w.inflow_cents)}</td>
      <td>${fmt$(w.outflow_cents)}</td><td>${fmt$(w.closing_cash_cents)}</td></tr>`).join("")}</table></div>
    <p class="muted" style="font-size:12px;margin-top:8px">${esc(cf.assumptions)}</p></div>`;
}

async function explore() {
  P.innerHTML = `<div class="card"><h3>Explore</h3><div class="row">
    <select id="ex-m"><option>revenue</option><option>orders</option><option>expenses</option></select>
    <select id="ex-d"><option>channel</option><option>date</option><option>segment</option></select>
    <button class="btn" id="ex-run">Run</button></div><div id="ex-out" style="margin-top:12px"></div></div>`;
  $("#ex-run").onclick = async () => {
    $("#ex-run").disabled = true;
    try {
      const r = await api(`/orgs/${S.org}/explore${rangeQ()}${rangeQ() ? "&" : "?"}metric=${$("#ex-m").value}&dimension=${$("#ex-d").value}`);
      $("#ex-out").innerHTML = r.rows.length ? `<div class="table-wrap"><table><tr><th>${esc(r.dimension)}</th><th>Value</th><th>Count</th></tr>
        ${r.rows.map(x => `<tr><td>${esc(x.key)}</td><td>${fmt$(x.value_cents)}</td><td>${x.count}</td></tr>`).join("")}</table></div>`
        : `<div class="empty-state">No rows for this metric / dimension in the selected range</div>`;
    } catch (e) {
      $("#ex-out").innerHTML = `<p class="err">${esc(e.message)}</p>`;
    } finally { $("#ex-run").disabled = false; }
  };
}

async function cohorts() {
  const c = await api(`/orgs/${S.org}/cohorts`);
  P.innerHTML = `<div class="card"><h3>Cohort Retention (% of cohort ordering again in month N)</h3>
    ${c.length ? `<div class="table-wrap"><table><tr><th>Cohort</th><th>Size</th>${[0, 1, 2, 3, 4, 5, 6].map(m => `<th>M${m}</th>`).join("")}</tr>
    ${c.map(r => `<tr><td>${esc(r.cohort)}</td><td>${r.size}</td>${r.retention.map(x =>
      `<td style="background:rgba(242,118,46,${Math.min(x.pct, 100) / 120})">${x.pct}%</td>`).join("")}</tr>`).join("")}</table></div>`
    : `<div class="empty-state">Not enough order history to build cohorts</div>`}</div>`;
}

async function ask() {
  P.innerHTML = `<div class="card"><h3>✦ Ask AI <span class="muted" style="font-size:11px">answers computed from your org's data</span></h3>
    <div class="chat-log" id="chat"></div><div class="row">
    <input id="q" style="flex:1;background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:10px"
      placeholder="Why did revenue change? Which channel is top? What if we raise prices 5%?">
    <button class="btn" id="ask-btn">Ask</button></div></div>`;
  const askIt = async () => {
    const q = $("#q").value; if (!q) return;
    $("#chat").insertAdjacentHTML("beforeend", `<div class="chat-msg user">${esc(q)}</div>`);
    $("#q").value = ""; $("#ask-btn").disabled = true;
    try {
      const r = await api(`/orgs/${S.org}/ai/ask`, { method: "POST", body: JSON.stringify({ question: q }) });
      $("#chat").insertAdjacentHTML("beforeend", `<div class="chat-msg">${esc(r.answer)}<div class="muted" style="font-size:11px">source: ${esc(r.source)}</div></div>`);
    } catch (e) {
      $("#chat").insertAdjacentHTML("beforeend", `<div class="chat-msg err">${esc(e.message)}</div>`);
    } finally { $("#ask-btn").disabled = false; }
    $("#chat").scrollTop = 1e6;
  };
  $("#ask-btn").onclick = askIt; $("#q").onkeydown = e => e.key === "Enter" && askIt();
}

function importPage() {
  P.innerHTML = `<div class="card"><h3>Import CSV</h3>
    <p class="muted" style="font-size:12px;margin-bottom:10px">orders: date,amount,channel,customer · expenses: date,amount,category,department · customers: name,email,segment,channel</p>
    <div class="row"><select id="imp-kind"><option>orders</option><option>expenses</option><option>customers</option></select>
    <input type="file" id="imp-file" accept=".csv"><button class="btn" id="imp-btn">Upload</button></div>
    <div id="imp-out" style="margin-top:12px"></div></div>`;
  $("#imp-btn").onclick = async () => {
    const f = $("#imp-file").files[0]; if (!f) return;
    $("#imp-btn").disabled = true;
    try {
      const fd = new FormData(); fd.append("file", f);
      const r = await fetch(`/api/orgs/${S.org}/import?kind=${$("#imp-kind").value}`,
        { method: "POST", headers: { "Authorization": "Bearer " + S.token }, body: fd });
      const d = await r.json();
      $("#imp-out").innerHTML = r.ok ? `<p>Status: <b>${esc(d.status)}</b> — ok: ${d.rows_ok}, failed: ${d.rows_failed}</p>
        ${(d.errors || []).map(e => `<div class="err" style="font-size:12px">row ${e.row}: ${esc(e.error)}</div>`).join("")}`
        : `<p class="err">${esc(d.detail || "Import failed")}</p>`;
    } catch (e) {
      $("#imp-out").innerHTML = `<p class="err">${esc(e.message)}</p>`;
    } finally { $("#imp-btn").disabled = false; }
  };
}

async function quality() {
  const q = await api(`/orgs/${S.org}/data-quality`);
  P.innerHTML = `<div class="card"><h3>Data Quality — completeness ${q.completeness_pct}% (${q.orders_total} orders)</h3>
    <div class="table-wrap"><table><tr><th>Issue</th><th>Entity</th><th>Count</th><th>Priority</th><th>Recommendation</th></tr>
    ${q.issues.map(i => `<tr><td>${esc(i.type)}</td><td>${esc(i.entity)}</td><td>${i.count}</td>
      <td><span class="badge ${esc(i.priority)}">${esc(i.priority)}</span></td><td class="muted">${esc(i.recommendation)}</td></tr>`).join("") || "<tr><td colspan=5 class='muted'>No issues detected</td></tr>"}</table></div>
    <h3 style="margin-top:14px">Recent imports</h3>
    <div class="table-wrap"><table><tr><th>File</th><th>Status</th><th>OK</th><th>Failed</th></tr>
    ${q.recent_imports.map(b => `<tr><td>${esc(b.filename)}</td><td>${esc(b.status)}</td><td>${b.rows_ok}</td><td>${b.rows_failed}</td></tr>`).join("") || "<tr><td colspan=4 class='muted'>—</td></tr>"}</table></div></div>`;
}

async function alerts() {
  const rows = await api(`/orgs/${S.org}/alerts`);
  P.innerHTML = `<div class="card"><h3>Alerts</h3>
    <div class="row"><input id="al-name" placeholder="Alert name" style="background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px">
    <input id="al-th" type="number" placeholder="threshold %" style="width:110px;background:var(--surface2);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:8px">
    <button class="btn" id="al-add">Create</button></div>
    <div class="table-wrap"><table style="margin-top:12px"><tr><th>Name</th><th>Metric</th><th>Condition</th><th>Threshold</th><th>Active</th></tr>
    ${rows.map(a => `<tr><td>${esc(a.name)}</td><td>${esc(a.metric)}</td><td>${esc(a.condition)}</td><td>${a.threshold}</td><td>${a.is_active ? "✓" : "—"}</td></tr>`).join("")}</table></div></div>`;
  $("#al-add").onclick = async () => {
    if (!$("#al-name").value.trim()) return;
    try {
      await api(`/orgs/${S.org}/alerts`, { method: "POST",
        body: JSON.stringify({ name: $("#al-name").value.trim(), threshold: +$("#al-th").value || 0, condition: "change_pct" }) });
      alerts();
    } catch (e) { alert("Could not create alert: " + e.message); }
  };
}

async function tasks() {
  const rows = await api(`/orgs/${S.org}/tasks`);
  P.innerHTML = `<div class="card"><h3>Tasks &amp; Actions</h3>
    <div class="table-wrap"><table><tr><th>Title</th><th>Priority</th><th>Status</th><th>Due</th><th></th></tr>
    ${rows.map(t => `<tr><td>${esc(t.title)}</td><td><span class="badge ${esc(t.priority)}">${esc(t.priority)}</span></td>
      <td>${esc(t.status)}</td><td>${t.due_date || "—"}</td>
      <td>${t.status !== "done" ? `<button class="btn ghost" onclick="doneTask(${t.id})">Done</button>` : ""}</td></tr>`).join("") ||
      "<tr><td colspan=5 class='muted'>No tasks</td></tr>"}</table></div></div>`;
}
window.doneTask = async id => {
  try { await api(`/orgs/${S.org}/tasks/${id}`, { method: "PATCH", body: JSON.stringify({ status: "done" }) }); tasks(); }
  catch (e) { alert("Could not update task: " + e.message); }
};

async function metrics() {
  const rows = await api(`/metrics-catalog`);
  P.innerHTML = `<div class="card"><h3>Metrics Catalog</h3>
    <div class="table-wrap"><table><tr><th>Key</th><th>Name</th><th>Formula</th><th>Unit</th><th>Owner</th></tr>
    ${rows.map(m => `<tr><td>${esc(m.key)}</td><td>${esc(m.name)}</td><td class="muted" style="font-size:12px">${esc(m.formula)}</td><td>${esc(m.unit)}</td><td>${esc(m.owner)}</td></tr>`).join("")}</table></div></div>`;
}

async function team() {
  const rows = await api(`/orgs/${S.org}/team`);
  P.innerHTML = `<div class="card"><h3>Team Management</h3>
    <div class="table-wrap"><table><tr><th>Email</th><th>Name</th><th>Role</th><th>Active</th></tr>
    ${rows.map(u => `<tr><td>${esc(u.email)}</td><td>${esc(u.name)}</td><td>${esc(u.role)}</td><td>${u.active ? "✓" : "✗"}</td></tr>`).join("")}</table></div></div>`;
}

async function billing() {
  const [sub, plans] = await Promise.all([api(`/orgs/${S.org}/subscription`), api(`/plans`)]);
  P.innerHTML = `<div class="card"><h3>Current subscription: ${esc(sub.plan || "none")} (${esc(sub.status || "—")})</h3>
    <p class="muted" style="font-size:12px">Payment provider integration pending real credentials — no fake charges.</p></div>
    <div class="grid cols3" style="margin-top:14px">${plans.map(p => `<div class="card">
    <h3>${esc(p.name)} — $${p.price_cents / 100}/mo</h3>
    <div class="muted" style="font-size:12px">${Object.entries(p.limits).map(([k, v]) => `${esc(k)}: ${esc(v)}`).join(" · ")}</div></div>`).join("")}</div>`;
}

async function admin() {
  const [orgs, audit] = await Promise.all([api(`/admin/organizations`), api(`/admin/audit`)]);
  P.innerHTML = `<div class="card"><h3>Admin — Organizations</h3>
    <div class="table-wrap"><table><tr><th>ID</th><th>Name</th><th>Members</th><th>Orders</th></tr>
    ${orgs.map(o => `<tr><td>${o.id}</td><td>${esc(o.name)}</td><td>${o.members}</td><td>${o.orders}</td></tr>`).join("")}</table></div></div>
    <div class="card" style="margin-top:14px"><h3>Audit log</h3>
    <div class="table-wrap"><table><tr><th>When</th><th>User</th><th>Action</th><th>Entity</th></tr>
    ${audit.map(a => `<tr><td class="muted">${esc(a.at.slice(0, 19))}</td><td>${a.user_id ?? "—"}</td><td>${esc(a.action)}</td><td>${esc(a.entity)}</td></tr>`).join("")}</table></div></div>`;
}

/* ---------- Boot ---------- */
(async () => {
  if (!S.token) return;
  try {
    const me = await api(`/auth/me`);
    $("#login-wrap").classList.add("hidden"); $("#app").classList.remove("hidden");
    $("#user-badge").textContent = `${me.full_name || me.email} · ${me.orgs[0]?.role || ""}`;
    const sel = $("#org-select");
    sel.innerHTML = me.orgs.map(o => `<option value="${o.org_id}">${esc(o.org_name)}</option>`).join("");
    sel.value = S.org || me.orgs[0].org_id; S.org = +sel.value;
    sel.onchange = () => { S.org = +sel.value; localStorage.setItem("nx_org", S.org); renderPage("overview"); };
    const today = new Date(), d30 = new Date(today.getTime() - 30 * 864e5);
    S.end = today.toISOString().slice(0, 10); S.start = d30.toISOString().slice(0, 10);
    $("#date-start").value = S.start; $("#date-end").value = S.end;
    // mobile: start collapsed so content is never hidden behind the rail
    if (window.innerWidth <= 760) $("#app").classList.add("collapsed");
    renderPage("overview");
  } catch (e) { logout(); }
})();
