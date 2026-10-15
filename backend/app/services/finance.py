"""Finance: budget vs actual, cash flow, what-if scenarios, unit economics, P&L."""
from datetime import date, timedelta
from collections import defaultdict
from sqlalchemy.orm import Session
from sqlalchemy import func
from .. import models
from .metrics import _sum_orders


def budget_vs_actual(db: Session, org_id: int, period: str):
    y, m = map(int, period.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    budgets = (db.query(models.Budget)
               .filter(models.Budget.org_id == org_id, models.Budget.period == period).all())
    actuals = (db.query(models.Transaction.department, models.Transaction.category,
                        func.sum(models.Transaction.amount_cents))
               .filter(models.Transaction.org_id == org_id, models.Transaction.kind == "expense",
                       models.Transaction.date >= start, models.Transaction.date <= end)
               .group_by(models.Transaction.department, models.Transaction.category).all())
    act_map = {(a[0], a[1]): int(a[2]) for a in actuals}
    rows = []
    for b in budgets:
        actual = act_map.get((b.department, b.category), 0)
        var = actual - b.planned_cents
        rows.append({"department": b.department, "category": b.category,
                     "planned_cents": b.planned_cents, "actual_cents": actual,
                     "variance_cents": var,
                     "variance_pct": round(var / b.planned_cents * 100, 1) if b.planned_cents else None,
                     "status": "over" if var > 0 else "under"})
    return {"period": period, "rows": rows,
            "total_planned_cents": sum(r["planned_cents"] for r in rows),
            "total_actual_cents": sum(r["actual_cents"] for r in rows)}


def pnl(db: Session, org_id: int, start: date, end: date):
    rev, orders = _sum_orders(db, org_id, start, end)
    exp_rows = (db.query(models.Transaction.category, func.sum(models.Transaction.amount_cents))
                .filter(models.Transaction.org_id == org_id, models.Transaction.kind == "expense",
                        models.Transaction.date >= start, models.Transaction.date <= end)
                .group_by(models.Transaction.category).all())
    expenses = {r[0]: int(r[1]) for r in exp_rows}
    cogs = expenses.get("COGS", 0)
    opex = sum(v for k, v in expenses.items() if k not in ("COGS",))
    gross = rev - cogs
    ebitda = gross - opex
    return {"revenue_cents": rev, "cogs_cents": cogs, "gross_profit_cents": gross,
            "opex_cents": opex, "ebitda_cents": ebitda,
            "expense_breakdown": expenses,
            "gross_margin_pct": round(gross / rev * 100, 1) if rev else None,
            "ebitda_margin_pct": round(ebitda / rev * 100, 1) if rev else None,
            "note": "Simplified single-step P&L. Balance sheet links are versioned in financial_models."}


def cash_flow_forecast(db: Session, org_id: int, weeks: int = 13, opening_cash_cents: int = 500_000_00):
    """13-week direct-method forecast from trailing averages. Documented assumptions."""
    today = date.today()
    past_start = today - timedelta(days=56)
    rev8, _ = _sum_orders(db, org_id, past_start, today)
    weekly_in = rev8 / 8
    exp8 = (db.query(func.coalesce(func.sum(models.Transaction.amount_cents), 0))
            .filter(models.Transaction.org_id == org_id, models.Transaction.kind == "expense",
                    models.Transaction.date >= past_start, models.Transaction.date <= today).scalar())
    weekly_out = int(exp8) / 8
    rows, cash, min_cash, gap_week = [], opening_cash_cents, opening_cash_cents, None
    for w in range(1, weeks + 1):
        cash = int(cash + weekly_in - weekly_out)
        min_cash = min(min_cash, cash)
        if cash < 0 and gap_week is None:
            gap_week = w
        rows.append({"week": w, "start": (today + timedelta(weeks=w - 1)).isoformat(),
                     "inflow_cents": int(weekly_in), "outflow_cents": int(weekly_out),
                     "closing_cash_cents": cash})
    return {"weeks": rows, "opening_cash_cents": opening_cash_cents,
            "min_cash_cents": min_cash,
            "cash_gap_warning": gap_week is not None,
            "gap_week": gap_week,
            "assumptions": "Inflows/outflows = trailing 8-week weekly averages; no seasonality or AR/AP aging applied."}


def what_if(db: Session, org_id: int, start: date, end: date, price_pct=0.0, volume_pct=0.0,
            cost_pct=0.0, marketing_pct=0.0):
    base = pnl(db, org_id, start, end)
    rev = base["revenue_cents"]
    new_rev = int(rev * (1 + price_pct / 100) * (1 + volume_pct / 100))
    new_cogs = int(base["cogs_cents"] * (1 + volume_pct / 100) * (1 + cost_pct / 100))
    mkt = base["expense_breakdown"].get("Marketing", 0)
    other_opex = base["opex_cents"] - mkt
    new_opex = int(mkt * (1 + marketing_pct / 100)) + other_opex
    gross = new_rev - new_cogs
    ebitda = gross - new_opex
    fixed = other_opex
    unit_contrib = (gross / max(1, int(new_rev / max(base.get("revenue_cents", 1), 1) * 1)))  # simplified
    be_rev = int(fixed / (gross / new_rev)) if new_rev and gross > 0 else None
    return {
        "base": base,
        "scenario": {"price_pct": price_pct, "volume_pct": volume_pct,
                     "cost_pct": cost_pct, "marketing_pct": marketing_pct},
        "result": {"revenue_cents": new_rev, "gross_profit_cents": gross,
                   "ebitda_cents": ebitda,
                   "ebitda_margin_pct": round(ebitda / new_rev * 100, 1) if new_rev else None,
                   "break_even_revenue_cents": be_rev,
                   "delta_revenue_cents": new_rev - rev,
                   "delta_ebitda_cents": ebitda - base["ebitda_cents"]},
        "note": "Linear assumptions; demand elasticity not modeled."}


def unit_economics(db: Session, org_id: int, start: date, end: date):
    rev, orders = _sum_orders(db, org_id, start, end)
    mkt = (db.query(func.coalesce(func.sum(models.Transaction.amount_cents), 0))
           .filter(models.Transaction.org_id == org_id, models.Transaction.kind == "expense",
                   models.Transaction.category == "Marketing",
                   models.Transaction.date >= start, models.Transaction.date <= end).scalar())
    new_cust = (db.query(func.count(models.Customer.id))
                .filter(models.Customer.org_id == org_id,
                        func.date(models.Customer.created_at) >= start,
                        func.date(models.Customer.created_at) <= end).scalar())
    total_cust = db.query(func.count(models.Customer.id)).filter_by(org_id=org_id).scalar() or 1
    months = max(1, (end - start).days // 30)
    arpu_m = rev / total_cust / months
    cogs = (db.query(func.coalesce(func.sum(models.Transaction.amount_cents), 0))
            .filter(models.Transaction.org_id == org_id, models.Transaction.category == "COGS",
                    models.Transaction.date >= start, models.Transaction.date <= end).scalar())
    gm = (rev - int(cogs)) / rev if rev else 0.5
    churned = db.query(func.count(models.Customer.id)).filter_by(org_id=org_id, is_active=False).scalar()
    churn_m = churned / total_cust / months
    ltv = (arpu_m * gm / churn_m) if churn_m > 0 else arpu_m * gm * 24
    cac = (int(mkt) / new_cust) if new_cust else None
    payback = (cac / (arpu_m * gm)) if (cac and arpu_m * gm > 0) else None
    return {
        "arpu_monthly_cents": int(arpu_m),
        "gross_margin_pct": round(gm * 100, 1),
        "monthly_churn_pct": round(churn_m * 100, 2),
        "ltv_cents": int(ltv),
        "cac_cents": int(cac) if cac is not None else None,
        "ltv_cac_ratio": round(ltv / cac, 2) if cac else None,
        "cac_payback_months": round(payback, 1) if payback else None,
        "formulas": {
            "CAC": "Marketing spend / new customers in period",
            "LTV": "ARPU_monthly * gross_margin / monthly_churn (fallback: 24-month horizon)",
            "CAC Payback": "CAC / (ARPU_monthly * gross_margin)"},
        "caveats": "CAC uses only category='Marketing' expenses; attribution model: last-touch by order.channel."}
