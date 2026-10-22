"""Ask AI copilot: deterministic, data-grounded answers through backend tools.
Every answer is computed from org-scoped data the user is allowed to see.
No claim of causation — only measured changes and correlations."""
import re
from datetime import date, timedelta
from sqlalchemy.orm import Session
from . import metrics as msvc, finance as finsvc
from .. import models


def _d(x):
    return f"${x/100:,.0f}"


def ask(db: Session, org_id: int, question: str) -> dict:
    q = question.lower().strip()
    today = date.today()
    s30, s60 = today - timedelta(days=30), today - timedelta(days=60)

    if "cash" in q:
        cf = finsvc.cash_flow_forecast(db, org_id)
        msg = (f"Opening cash {_d(cf['opening_cash_cents'])}; projected 13-week minimum "
               f"{_d(cf['min_cash_cents'])}. " +
               (f"⚠️ Cash gap projected in week {cf['gap_week']}." if cf["cash_gap_warning"]
                else "No cash gap projected."))
        return {"answer": msg, "data": cf, "source": "cash_flow_forecast"}

    if "price" in q and ("%" in q or "increase" in q):
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", q)
        pct = float(m.group(1)) if m else 5.0
        r = finsvc.what_if(db, org_id, s30, today, price_pct=pct)
        return {"answer": (f"A {pct}% price increase on the last 30 days would move revenue by "
                           f"{_d(r['result']['delta_revenue_cents'])} and EBITDA by "
                           f"{_d(r['result']['delta_ebitda_cents'])} (linear assumptions, no elasticity)."),
                "data": r, "source": "what_if_simulator"}

    if "budget" in q:
        period = f"{today.year}-{today.month:02d}"
        b = finsvc.budget_vs_actual(db, org_id, period)
        over = [r for r in b["rows"] if r["status"] == "over"]
        if over:
            lines = "; ".join(f"{r['department']}/{r['category']} over by {_d(r['variance_cents'])}"
                              f" ({r['variance_pct']}%)" for r in over[:5])
            return {"answer": f"Departments over budget in {period}: {lines}.", "data": b,
                    "source": "budget_vs_actual"}
        return {"answer": f"No budget overruns detected for {period}.", "data": b,
                "source": "budget_vs_actual"}

    if "churn" in q:
        k = msvc.kpi_overview(db, org_id, s30, today)
        churned = (db.query(models.Customer)
                   .filter_by(org_id=org_id, is_active=False).limit(10).all())
        return {"answer": (f"Churn rate is {k['churn_rate']['value']}% "
                           f"({k['churn_rate']['churned']} churned customers). "
                           f"Highest-risk accounts are those inactive 60+ days."),
                "data": {"kpi": k["churn_rate"],
                         "churned": [c.name for c in churned]},
                "source": "customers"}

    if "channel" in q and any(w in q for w in ("highest", "best", "top", "conversion")):
        ch = msvc.revenue_by_channel(db, org_id, s30, today)
        if ch:
            top = ch[0]
            return {"answer": (f"Top channel last 30 days: {top['channel']} — "
                               f"{_d(top['revenue_cents'])} ({top['share_pct']}% share, "
                               f"{top['orders']} orders, AOV {_d(top['avg_order_cents'])})."),
                    "data": ch, "source": "orders_by_channel"}

    if "expense" in q or "cost" in q:
        cur = (db.query(models.Transaction.category,
                        __import__("sqlalchemy").func.sum(models.Transaction.amount_cents))
               .filter(models.Transaction.org_id == org_id,
                       models.Transaction.kind == "expense",
                       models.Transaction.date >= s30)
               .group_by(models.Transaction.category)
               .order_by(__import__("sqlalchemy").func.sum(models.Transaction.amount_cents).desc())
               .all())
        if cur:
            top = cur[0]
            return {"answer": (f"Largest expense category last 30 days: {top[0]} — {_d(int(top[1]))}. "
                               f"Full breakdown attached."), 
                    "data": [{"category": c, "amount_cents": int(a)} for c, a in cur],
                    "source": "transactions"}

    if "investigate" in q or "first" in q or "priority" in q:
        a = (db.query(models.AnomalyFinding)
             .filter(models.AnomalyFinding.org_id == org_id,
                     models.AnomalyFinding.status.in_(["new", "under_review", "action_required"]))
             .order_by(models.AnomalyFinding.priority == "high",
                       models.AnomalyFinding.date.desc()).first())
        if a:
            return {"answer": (f"Investigate first: {a.metric} anomaly on {a.date} — "
                               f"actual ${a.actual_value:,.0f} vs expected ${a.expected_value:,.0f} "
                               f"({a.deviation_pct}%, priority {a.priority})."),
                    "data": {"id": a.id, "factors": a.factors}, "source": "anomaly_findings"}
        return {"answer": "No open anomalies — nothing urgent to investigate.", "data": {},
                "source": "anomaly_findings"}

    if "revenue" in q:
        k = msvc.kpi_overview(db, org_id, s30, today)
        ch = msvc.revenue_by_channel(db, org_id, s30, today)
        prev_ch = msvc.revenue_by_channel(db, org_id, s60, s30)
        prev_map = {c["channel"]: c["revenue_cents"] for c in prev_ch}
        deltas = sorted(({"channel": c["channel"],
                          "delta_cents": c["revenue_cents"] - prev_map.get(c["channel"], 0)}
                         for c in ch), key=lambda x: x["delta_cents"])
        direction = "increased" if k["revenue"]["pct_change"] >= 0 else "declined"
        driver = deltas[-1] if k["revenue"]["pct_change"] >= 0 else (deltas[0] if deltas else None)
        msg = (f"Revenue {direction} {abs(k['revenue']['pct_change'])}% vs the prior 30 days "
               f"({_d(k['revenue']['value_cents'])}). ")
        if driver:
            msg += (f"Largest channel move: {driver['channel']} "
                    f"({_d(driver['delta_cents'])}). Correlation, not proven causation.")
        return {"answer": msg, "data": {"kpi": k, "channel_deltas": deltas},
                "source": "kpi_overview"}

    return {"answer": ("I can answer questions about revenue, channels, expenses, budgets, churn, "
                       "cash flow, price scenarios and investigation priorities — all computed "
                       "from your organization's data."),
            "data": {}, "source": "help"}
