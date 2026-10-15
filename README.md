# NEXORA AI — Intelligent Business Analytics Platform

Full-stack SaaS: FastAPI backend + SQLAlchemy + vanilla-JS dark UI (per reference design).
**All metrics are computed from the database. No fake API responses in the app path.**

## Quick start (local)

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload          # http://localhost:8000
```

Open http://localhost:8000 → login **admin@nexora.ai / admin123** (demo org with 150 days
of deterministic sample data, seeded because `SEED_DEMO=1`). API docs: http://localhost:8000/docs

## Docker

```bash
docker compose up --build    # http://localhost:8000
```

## Tests

```bash
cd backend && python -m pytest ../tests -v
```

Covers: auth, RBAC (incl. org isolation 403), KPI math, P&L identities, what-if math,
forecast backtest vs naive baseline, anomaly detection, CSV import incl. row-level
validation, formula-injection guard, Ask-AI role restriction, tasks, alerts, audit.

## Architecture

```
frontend/  index.html + app.js — canvas charts, no build step
backend/app/
  main.py       — all REST endpoints (auth, kpi, anomalies, forecast, finance, import, ai, alerts, tasks, admin)
  models.py     — 25+ tables, money as integer cents, org-scoped
  security.py   — PBKDF2 hashing, JWT, role ranks, audit
  services/
    metrics.py  — KPI, channel, segment, explore, cohorts
    anomaly.py  — rolling z-score detection + factor analysis (correlation ≠ causation)
    forecast.py — linear trend + weekly seasonality, backtested MAPE vs naive baseline
    finance.py  — P&L, budget-vs-actual, 13-week cash flow, what-if, unit economics
    importer.py — CSV import: validation, dedup, formula-injection guard, batch history
    ai.py       — Ask AI: deterministic data-grounded copilot, role-aware
    seed.py     — plans, metric catalog, demo data
```

## Roles

owner > cfo > analyst > manager/accountant > viewer. Enforced on the backend
(`require_role`); financial AI answers are blocked for viewer/manager.

## Methodology notes (documented, per spec)

- Revenue = SUM(orders.total, status='completed'); refunds excluded from AOV numerator.
- Conversion = completed orders / active customers (proxy — configure per event in Metrics).
- Anomaly = rolling z-score (window 14, |z| ≥ 2.5); factors are correlations, never asserted as causes.
- Forecast = linear trend + weekly seasonality; quality = MAPE on held-out last 7 days vs naive baseline; UI refuses to forecast with <21 points.
- Cash flow = direct method, trailing 8-week averages; assumptions shown in UI.
- CAC/LTV formulas exposed in API response (`formulas` field); attribution = last-touch by order.channel.

## Known limitations (honest list)

- SQLite default; PostgreSQL supported via `DATABASE_URL` (compose file has commented block).
- Only CSV import implemented; XLSX/Postgres/GA4 connectors are architected (DataSource registry) but not yet connected — per spec §13 they count as done only after real testing.
- Ask AI is a deterministic grounded copilot, not a hosted LLM; plug a provider into `services/ai.py` behind the same access checks.
- Billing: plans/limits/subscription records implemented; payment provider NOT integrated (no fake charges) — needs real credentials.
- Background job queue (Celery/RQ), object storage, and error monitoring are not wired in this deliverable.
- Balance sheet ↔ cash flow full three-statement linkage is simplified to a documented P&L + direct-method cash forecast.
- Refresh token rotation, MFA, and rate limiting middleware are planned hardening steps.
