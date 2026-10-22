# NEXORA AI — Changelog & Bug-Fix Report

## Upgrade: three-role financial management platform (2026-10-16)

### Original layout/functional bugs (per spec §15) — status & root causes

The delivered archive (`nexora_ai_fixed.tar.gz`) already contained a partial fix pass;
each fix was verified against its root cause and preserved:

| Bug | Root cause | Fix (verified in code) |
|---|---|---|
| Dashboard clipped by viewport | `#main` allowed to grow past viewport; grid columns overflowed | `flex` shell + `#main{min-width:0;max-width:100%}`; `.cols{grid-template-columns:minmax(0,2fr) minmax(0,1fr)}` |
| Incorrect sidebar/content positioning | sidebar not a flex item, content overlapped | sticky flex sidebar (`flex:0 0 var(--sidebar-w)`) + fluid main |
| Cards with inconsistent widths | fixed grid tracks | `grid-template-columns:repeat(auto-fit,minmax(180px,1fr))` for KPIs |
| Misaligned donut + legend | absolute-positioned legend | `.seg-flex` flex layout, canvas + legend side by side |
| "Revenue by Channel" partially hidden | fixed-width bars overflowed card | `.bar-track{min-width:0}`, percentage widths, `.table-wrap{overflow-x:auto}` |
| "Recent Anomalies" table hidden | table wider than card | horizontal scroll *inside* card via `.table-wrap` |
| Incorrect chart resizing | canvas bitmap never re-fit on resize | HiDPI-aware `fitCanvas()` + `ResizeObserver` debounce |
| AI Insight wrong empty state | no distinction between "never ran" and "no findings" | `/anomalies/status` endpoint; 3-state UI (never_run / zero findings / finding) |
| Duplicate detection runs | no submission guard | `S.running` debounce on `runAnoms()` |
| Nonfunctional links | default anchor navigation | `go(page)` SPA routing, `return false` |

### Added in this upgrade

- **Three canonical roles** (spec §3): `manager` / `top_manager` / `owner`, legacy roles
  (`cfo`/`analyst`/`accountant`) mapped onto them in `security.canonical_role()`.
- **Server-side permission matrix** (`security.PERMISSIONS`, `require_perm()`): every
  protected operation checks capability server-side — never frontend state.
- **Role dashboards** (`services/finops.py`):
  - Manager: dept revenue/expenses/profit, budget utilization & variance, upcoming
    AR/AP, open tasks, assigned anomalies, own requests. Scoped to `membership.department`.
  - Top Manager: consolidated revenue, COGS, gross/operating profit, EBITDA (documented
    definition), budget vs actual, department comparison, 13-week cash forecast,
    forecast MAPE vs naive baseline, AR/AP aging, pending approvals.
  - Owner: everything above + net income, cash balance/runway, working capital,
    approval policy, recent audit events.
- **Approval engine** (spec §10): `ApprovalPolicy` (delegated Top-Manager limit, default
  $10,000), `ApprovalRequest` + immutable `ApprovalEvent` history, self-approval
  prevention, over-limit escalation to Owner, `request_info` state, notifications.
- **AR/AP ledgers**: `ARInvoice`, `APInvoice`, `Supplier` models, aging buckets
  (current/1-30/31-60/61-90/90+), overdue totals, manager department scoping.
- **Scenario engine** (spec §8): saved `Scenario` records whose `results` are computed
  by the existing what-if engine (never static values), comparison endpoint vs base.
- **Permission-scoped CSV reports** (spec §13): pnl, budget-variance, expenses,
  ar-aging, ap-aging. Managers get department-scoped exports; exports are audit-logged.
- **Org-wide audit endpoint** (Owner; append-only — no update/delete routes exist
  for `AuditLog` / `ApprovalEvent` anywhere in the app).
- **Demo seed** (`services/seed_roles.py`): departments with budgets, three demo users,
  60 days of departmental revenue, AR/AP invoices across aging buckets, sample requests.
- **Frontend** (`frontend/roles.js`): Manager/Top-Manager/Owner pages, approvals
  workflow UI with history, AR/AP aging, scenario builder + comparison, report
  downloads, role-aware nav hiding (UX only — server enforces).

### Demo accounts (SEED_DEMO=1)
| Role | Email | Password |
|---|---|---|
| Owner | owner@nexora.ai | owner123 |
| Top Manager | exec@nexora.ai | exec123 |
| Manager (Sales dept) | manager@nexora.ai | manager123 |
| Owner (legacy demo) | admin@nexora.ai | admin123 |

### Modified / new files
- Modified: `backend/app/security.py` (canonical roles + permission matrix),
  `backend/app/models.py` (+9 tables), `backend/app/main.py` (+~20 endpoints),
  `frontend/index.html` (nav + roles.js), `frontend/app.js` (page dispatch).
- New: `backend/app/services/finops.py`, `backend/app/services/seed_roles.py`,
  `frontend/roles.js`, `tests/test_roles.py`, `CHANGELOG.md`.

### Test results (actual)
`cd backend && python -m pytest ../tests -q` → **36 passed** (21 pre-existing API/finance/
import/AI tests + 15 new three-role tests covering dashboards, RBAC boundaries, approval
thresholds, self-approval, AR/AP aging, manager scoping, scenario RBAC + engine,
report scoping, departments, audit, cross-org isolation on every new endpoint).

### Honest limitations
- EBITDA = Operating Profit and Net Income = Operating Profit in this build: D&A,
  interest and tax are not separate ledger lines yet (documented in API responses).
- Full three-statement linkage (balance sheet ↔ cash flow) remains the documented
  simplified P&L + direct-method 13-week forecast.
- XLSX import adapter, payment-provider billing, and LLM-backed Ask-AI remain
  integration stubs — not claimed operational until connected with real credentials.
- Manager AI access: financial answers stay blocked for manager/viewer per the
  pre-existing policy; extend via the permission matrix when desired.
