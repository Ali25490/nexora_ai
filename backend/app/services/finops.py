"""NEXORA AI — three-role financial operations layer.

Role dashboards (Manager / Top Manager / Owner), AR/AP aging, approval engine
with delegated thresholds + self-approval prevention, saved what-if scenarios,
and permission-scoped CSV reports. Money is integer cents everywhere.

Documented definitions (spec §7):
- Revenue / Expenses: SUM(transactions.amount_cents) by kind over the period.
- Gross Profit   = Revenue − expenses whose category is a COGS category
                   (documented set below; configurable per deployment).
- Operating Profit = Revenue − ALL expenses.
- EBITDA         = Operating Profit (D&A is not tracked as a separate ledger
                   line in this build — stated honestly, per spec §7.1).
- Net Income     = Operating Profit (no interest/tax lines tracked yet).
"""
import csv, io, json
from datetime import date, datetime, timedelta
from typing import Optional, List
from sqlalchemy.orm import Session
from sqlalchemy import func
from fastapi import HTTPException
from .. import models
from ..security import has_permission, canonical_role
from . import finance as finsvc

COGS_CATEGORIES = {"cogs", "cost of goods sold", "cost of goods", "supplier costs",
                   "suppliers", "infrastructure"}


# ---------------- low-level helpers ----------------
def _sum_tx(db: Session, org_id: int, kind: str, s: date, e: date,
            department: Optional[str] = None) -> int:
    q = db.query(func.coalesce(func.sum(models.Transaction.amount_cents), 0)).filter(
        models.Transaction.org_id == org_id, models.Transaction.kind == kind,
        models.Transaction.date >= s, models.Transaction.date <= e)
    if department:
        q = q.filter(models.Transaction.department == department)
    return int(q.scalar() or 0)


def _cogs_tx(db: Session, org_id: int, s: date, e: date) -> int:
    rows = db.query(models.Transaction.category,
                    func.coalesce(func.sum(models.Transaction.amount_cents), 0)).filter(
        models.Transaction.org_id == org_id, models.Transaction.kind == "expense",
        models.Transaction.date >= s, models.Transaction.date <= e
    ).group_by(models.Transaction.category).all()
    return int(sum(t for c, t in rows if (c or "").strip().lower() in COGS_CATEGORIES))


def _tx_by_department(db: Session, org_id: int, s: date, e: date) -> dict:
    rows = db.query(models.Transaction.department, models.Transaction.kind,
                    func.coalesce(func.sum(models.Transaction.amount_cents), 0)).filter(
        models.Transaction.org_id == org_id,
        models.Transaction.date >= s, models.Transaction.date <= e
    ).group_by(models.Transaction.department, models.Transaction.kind).all()
    out = {}
    for dept, kind, total in rows:
        d = out.setdefault(dept or "General", {"revenue_cents": 0, "expense_cents": 0})
        d["revenue_cents" if kind == "revenue" else "expense_cents"] = int(total)
    for d in out.values():
        d["profit_cents"] = d["revenue_cents"] - d["expense_cents"]
    return out


def _month_bounds(period: str):
    y, m = map(int, period.split("-"))
    s = date(y, m, 1)
    e = date(y + (1 if m == 12 else 0), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    return s, min(e, date.today()) if s <= date.today() else e


def _budget_utilization(db: Session, org_id: int, period: str,
                        department: Optional[str] = None) -> dict:
    q = db.query(models.Budget).filter(models.Budget.org_id == org_id,
                                       models.Budget.period == period)
    if department:
        q = q.filter(models.Budget.department == department)
    planned = sum(int(b.planned_cents or 0) for b in q.all())
    s, e = _month_bounds(period)
    actual = _sum_tx(db, org_id, "expense", s, e, department)
    variance = planned - actual
    return {"period": period, "department": department, "planned_cents": planned,
            "actual_cents": actual, "variance_cents": variance,
            "utilization_pct": round(actual / planned * 100, 1) if planned else None}


def _aging(invoices, today: date) -> dict:
    buckets = {"current": 0, "1_30": 0, "31_60": 0, "61_90": 0, "90_plus": 0}
    total = overdue = 0
    count = 0
    for inv in invoices:
        if inv.status in ("paid", "void"):
            continue
        amt = int(inv.amount_cents or 0)
        total += amt
        count += 1
        days = (today - inv.due_date).days if inv.due_date else 0
        if days <= 0:
            buckets["current"] += amt
        elif days <= 30:
            buckets["1_30"] += amt; overdue += amt
        elif days <= 60:
            buckets["31_60"] += amt; overdue += amt
        elif days <= 90:
            buckets["61_90"] += amt; overdue += amt
        else:
            buckets["90_plus"] += amt; overdue += amt
    return {"open_count": count, "total_cents": total, "overdue_cents": overdue,
            "buckets": buckets}


def ar_aging(db: Session, org_id: int) -> dict:
    return _aging(db.query(models.ARInvoice).filter_by(org_id=org_id).all(), date.today())


def ap_aging(db: Session, org_id: int) -> dict:
    return _aging(db.query(models.APInvoice).filter_by(org_id=org_id).all(), date.today())


def _pending_approvals(db: Session, org_id: int) -> List[models.ApprovalRequest]:
    return (db.query(models.ApprovalRequest)
            .filter(models.ApprovalRequest.org_id == org_id,
                    models.ApprovalRequest.status.in_(["pending", "info_requested"]))
            .order_by(models.ApprovalRequest.created_at.desc()).all())


def _req_dict(r: models.ApprovalRequest) -> dict:
    return {"id": r.id, "kind": r.kind, "title": r.title, "amount_cents": r.amount_cents,
            "department": r.department, "status": r.status, "requester_id": r.requester_id,
            "approver_id": r.approver_id,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "decided_at": r.decided_at.isoformat() if r.decided_at else None}


# ---------------- ROLE DASHBOARDS ----------------
def manager_dashboard(db: Session, org_id: int, user: models.User,
                      m: models.Membership) -> dict:
    """Manager: own-department operations only (spec §3, role 1)."""
    today = date.today()
    s = today - timedelta(days=29)
    dept = m.department or None
    revenue = _sum_tx(db, org_id, "revenue", s, today, dept)
    expenses = _sum_tx(db, org_id, "expense", s, today, dept)
    period = f"{today.year}-{today.month:02d}"
    budget = _budget_utilization(db, org_id, period, dept)
    soon = today + timedelta(days=14)
    ar_due = (db.query(models.ARInvoice)
              .filter(models.ARInvoice.org_id == org_id, models.ARInvoice.status == "open",
                      models.ARInvoice.due_date <= soon).all())
    ap_due = (db.query(models.APInvoice)
              .filter(models.APInvoice.org_id == org_id,
                      models.APInvoice.status.in_(["pending_approval", "approved"]),
                      models.APInvoice.due_date <= soon).all())
    if dept:
        ar_due = [i for i in ar_due if i.department in (dept, "", None)]
        ap_due = [i for i in ap_due if i.department in (dept, "", None)]
    tasks = (db.query(models.Task).filter(models.Task.org_id == org_id,
                                          models.Task.status.in_(["open", "in_progress"]))
             .order_by(models.Task.due_date.asc().nullslast()).limit(20).all())
    anomalies = (db.query(models.AnomalyFinding)
                 .filter(models.AnomalyFinding.org_id == org_id,
                         models.AnomalyFinding.status.in_(["new", "under_review", "investigating"]))
                 .order_by(models.AnomalyFinding.date.desc()).limit(10).all())
    my_requests = (db.query(models.ApprovalRequest)
                   .filter_by(org_id=org_id, requester_id=user.id)
                   .order_by(models.ApprovalRequest.created_at.desc()).limit(20).all())
    return {
        "role": "manager", "department": dept, "period_days": 30,
        "department_revenue_cents": revenue, "department_expenses_cents": expenses,
        "department_profit_cents": revenue - expenses,
        "budget": budget,
        "upcoming_ar": [{"id": i.id, "number": i.number, "amount_cents": i.amount_cents,
                         "due_date": i.due_date.isoformat()} for i in ar_due],
        "upcoming_ap": [{"id": i.id, "number": i.number, "amount_cents": i.amount_cents,
                         "due_date": i.due_date.isoformat(), "status": i.status} for i in ap_due],
        "open_tasks": [{"id": t.id, "title": t.title, "status": t.status, "priority": t.priority,
                        "due_date": t.due_date.isoformat() if t.due_date else None} for t in tasks],
        "anomalies": [{"id": a.id, "metric": a.metric, "date": a.date.isoformat(),
                       "deviation_pct": a.deviation_pct, "priority": a.priority,
                       "status": a.status} for a in anomalies],
        "my_requests": [_req_dict(r) for r in my_requests],
    }


def top_manager_dashboard(db: Session, org_id: int) -> dict:
    """Top Manager: consolidated multi-department control (spec §3, role 2)."""
    today = date.today()
    s = today - timedelta(days=29)
    revenue = _sum_tx(db, org_id, "revenue", s, today)
    expenses = _sum_tx(db, org_id, "expense", s, today)
    cogs = _cogs_tx(db, org_id, s, today)
    gross_profit = revenue - cogs
    operating_profit = revenue - expenses
    period = f"{today.year}-{today.month:02d}"
    cash = None
    try:
        cash = finsvc.cash_flow_forecast(db, org_id)
    except Exception:
        cash = None
    last_fc = (db.query(models.ForecastRun).filter_by(org_id=org_id)
               .order_by(models.ForecastRun.created_at.desc()).first())
    return {
        "role": "top_manager", "period_days": 30,
        "revenue_cents": revenue, "expenses_cents": expenses,
        "cogs_cents": cogs, "gross_profit_cents": gross_profit,
        "operating_profit_cents": operating_profit,
        "ebitda_cents": operating_profit,
        "ebitda_note": "EBITDA = Operating Profit (D&A not tracked as a separate ledger line in this build)",
        "budget": _budget_utilization(db, org_id, period),
        "departments": _tx_by_department(db, org_id, s, today),
        "ar": ar_aging(db, org_id), "ap": ap_aging(db, org_id),
        "cash_forecast": cash,
        "forecast_accuracy": {"mape": last_fc.mape, "baseline_mape": last_fc.baseline_mape,
                              "at": last_fc.created_at.isoformat()} if last_fc else None,
        "pending_approvals": [_req_dict(r) for r in _pending_approvals(db, org_id)],
        "high_anomalies": db.query(models.AnomalyFinding)
            .filter_by(org_id=org_id, priority="high").count(),
    }


def owner_dashboard(db: Session, org_id: int) -> dict:
    """Owner: complete oversight (spec §3, role 3)."""
    d = top_manager_dashboard(db, org_id)
    d["role"] = "owner"
    op = d["operating_profit_cents"]
    d["net_income_cents"] = op
    d["net_income_note"] = "Net Income = Operating Profit (no interest/tax lines tracked yet)"
    cash_bal = None
    if d.get("cash_forecast"):
        cash_bal = d["cash_forecast"].get("opening_cash_cents")
    ar_t = d["ar"]["total_cents"]
    ap_t = d["ap"]["total_cents"]
    d["cash_balance_cents"] = cash_bal
    d["working_capital_cents"] = (cash_bal or 0) + ar_t - ap_t
    burn = d["expenses_cents"] - d["revenue_cents"]
    d["cash_runway_months"] = (round(cash_bal / burn, 1)
                               if cash_bal and burn > 0 else None)
    d["scenarios_count"] = db.query(models.Scenario).filter_by(org_id=org_id).count()
    d["audit_recent"] = [
        {"id": a.id, "action": a.action, "entity": a.entity,
         "at": a.created_at.isoformat(), "user_id": a.user_id}
        for a in db.query(models.AuditLog).filter_by(org_id=org_id)
        .order_by(models.AuditLog.created_at.desc()).limit(15).all()]
    policy = get_policy(db, org_id)
    d["approval_policy"] = {"top_manager_limit_cents": policy.top_manager_limit_cents,
                            "currency": policy.currency}
    return d


# ---------------- APPROVAL ENGINE (spec §10) ----------------
def get_policy(db: Session, org_id: int) -> models.ApprovalPolicy:
    p = db.query(models.ApprovalPolicy).filter_by(org_id=org_id).first()
    if not p:
        p = models.ApprovalPolicy(org_id=org_id)
        db.add(p)
        db.flush()
    return p


def submit_request(db: Session, org_id: int, user: models.User, kind: str, title: str,
                   amount_cents: int, department: str, description: str) -> models.ApprovalRequest:
    if kind not in ("expense", "payment", "budget"):
        raise HTTPException(400, "kind must be expense|payment|budget")
    r = models.ApprovalRequest(org_id=org_id, kind=kind, title=title,
                               amount_cents=amount_cents, department=department or "",
                               description=description or "", requester_id=user.id)
    db.add(r)
    db.flush()
    db.add(models.ApprovalEvent(request_id=r.id, actor_id=user.id, action="submit",
                                note=f"amount_cents={amount_cents}"))
    return r


def act_on_request(db: Session, org_id: int, user: models.User, m: models.Membership,
                   req: models.ApprovalRequest, action: str, note: str) -> models.ApprovalRequest:
    if req.status not in ("pending", "info_requested"):
        raise HTTPException(409, "Request already decided")
    if action == "request_info":
        if not has_permission(m.role, "approve_within_limit"):
            raise HTTPException(403, "Your role cannot process approvals")
        req.status = "info_requested"
    elif action in ("approve", "reject"):
        if action == "approve" and req.requester_id == user.id:
            raise HTTPException(403, "Self-approval is not permitted")
        policy = get_policy(db, org_id)
        over_limit = int(req.amount_cents or 0) > int(policy.top_manager_limit_cents or 0)
        needed = "approve_unlimited" if over_limit else "approve_within_limit"
        if not has_permission(m.role, needed):
            raise HTTPException(403,
                f"Amount exceeds delegated limit (${policy.top_manager_limit_cents/100:,.0f}); "
                f"requires '{needed}' (Owner)")
        req.status = "approved" if action == "approve" else "rejected"
        req.approver_id = user.id
        req.decided_at = datetime.utcnow()
    else:
        raise HTTPException(400, "action must be approve|reject|request_info")
    db.add(models.ApprovalEvent(request_id=req.id, actor_id=user.id, action=action,
                                note=note or ""))
    db.add(models.Notification(user_id=req.requester_id,
                               title=f"Request #{req.id} {action}d".replace("request_infod", "needs info"),
                               body=note or ""))
    return req


# ---------------- SCENARIOS (spec §8 what-if) ----------------
def run_scenario(db: Session, org_id: int, user: models.User, name: str,
                 period_start: date, period_end: date, params: dict) -> models.Scenario:
    res = finsvc.what_if(db, org_id, period_start, period_end,
                         float(params.get("price_pct", 0)), float(params.get("volume_pct", 0)),
                         float(params.get("cost_pct", 0)), float(params.get("marketing_pct", 0)))
    try:
        json.dumps(res)
    except TypeError:
        res = {"raw": str(res)}
    sc = models.Scenario(org_id=org_id, name=name, params=params, results=res,
                         period_start=period_start, period_end=period_end,
                         created_by=user.id)
    db.add(sc)
    db.flush()
    return sc


def compare_scenarios(db: Session, org_id: int, ids: List[int]) -> dict:
    scs = (db.query(models.Scenario)
           .filter(models.Scenario.org_id == org_id, models.Scenario.id.in_(ids)).all())
    base = None
    if scs:
        full = finsvc.what_if(db, org_id, scs[0].period_start, scs[0].period_end, 0, 0, 0, 0)
        base = full.get("result", full) if isinstance(full, dict) else full
    return {"base": base,
            "scenarios": [{"id": s.id, "name": s.name, "params": s.params,
                           "results": s.results,
                           "period": [s.period_start.isoformat(), s.period_end.isoformat()]}
                          for s in scs]}


# ---------------- REPORTS (CSV, permission-scoped — spec §13) ----------------
def _csv(filename: str, header: list, rows: list) -> tuple:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return filename, buf.getvalue()


def report_csv(db: Session, org_id: int, name: str, m: models.Membership,
               department: Optional[str] = None) -> tuple:
    cr = canonical_role(m.role)
    forced_dept = m.department if cr == "manager" else department
    today = date.today()
    s90 = today - timedelta(days=90)

    if name == "expenses":
        q = db.query(models.Transaction).filter(models.Transaction.org_id == org_id,
                                                models.Transaction.kind == "expense",
                                                models.Transaction.date >= s90)
        if forced_dept:
            q = q.filter(models.Transaction.department == forced_dept)
        rows = [[t.date.isoformat(), t.department, t.category, f"{t.amount_cents/100:.2f}"]
                for t in q.order_by(models.Transaction.date).all()]
        return _csv("expenses.csv", ["date", "department", "category", "amount"], rows)

    if name == "budget-variance":
        period = f"{today.year}-{today.month:02d}"
        q = db.query(models.Budget).filter_by(org_id=org_id, period=period)
        if forced_dept:
            q = q.filter(models.Budget.department == forced_dept)
        rows = []
        for b in q.all():
            bs, be = _month_bounds(period)
            actual = _sum_tx(db, org_id, "expense", bs, be, b.department)
            rows.append([b.department, b.category, period, f"{b.planned_cents/100:.2f}",
                         f"{actual/100:.2f}", f"{(b.planned_cents-actual)/100:.2f}"])
        return _csv("budget_variance.csv",
                    ["department", "category", "period", "planned", "actual", "variance"], rows)

    if name == "pnl":
        if cr == "manager" and not m.department:
            raise HTTPException(403, "Manager has no assigned department for P&L scope")
        rev = _sum_tx(db, org_id, "revenue", s90, today, forced_dept)
        exp = _sum_tx(db, org_id, "expense", s90, today, forced_dept)
        cogs = _cogs_tx(db, org_id, s90, today)
        rows = [["period", f"{s90.isoformat()}..{today.isoformat()}"],
                ["scope", forced_dept or "company"],
                ["revenue", f"{rev/100:.2f}"],
                ["cogs", f"{cogs/100:.2f}"],
                ["gross_profit", f"{(rev-cogs)/100:.2f}"],
                ["operating_expenses", f"{(exp-cogs)/100:.2f}"],
                ["operating_profit", f"{(rev-exp)/100:.2f}"],
                ["ebitda", f"{(rev-exp)/100:.2f}"],
                ["net_income", f"{(rev-exp)/100:.2f}"]]
        return _csv("pnl.csv", ["line", "value"], rows)

    if name == "ar-aging":
        invs = db.query(models.ARInvoice).filter_by(org_id=org_id).all()
        if forced_dept:
            invs = [i for i in invs if i.department in (forced_dept, "", None)]
        rows = [[i.number, i.due_date.isoformat() if i.due_date else "", i.status,
                 f"{i.amount_cents/100:.2f}", i.department] for i in invs]
        ag = _aging(invs, today)
        rows += [[], ["bucket_current", f"{ag['buckets']['current']/100:.2f}"],
                 ["bucket_1_30", f"{ag['buckets']['1_30']/100:.2f}"],
                 ["bucket_31_60", f"{ag['buckets']['31_60']/100:.2f}"],
                 ["bucket_61_90", f"{ag['buckets']['61_90']/100:.2f}"],
                 ["bucket_90_plus", f"{ag['buckets']['90_plus']/100:.2f}"]]
        return _csv("ar_aging.csv", ["number", "due_date", "status", "amount", "department"], rows)

    if name == "ap-aging":
        invs = db.query(models.APInvoice).filter_by(org_id=org_id).all()
        if forced_dept:
            invs = [i for i in invs if i.department in (forced_dept, "", None)]
        rows = [[i.number, i.due_date.isoformat() if i.due_date else "", i.status,
                 f"{i.amount_cents/100:.2f}", i.department, i.category] for i in invs]
        ag = _aging(invs, today)
        rows += [[], ["bucket_current", f"{ag['buckets']['current']/100:.2f}"],
                 ["bucket_1_30", f"{ag['buckets']['1_30']/100:.2f}"],
                 ["bucket_31_60", f"{ag['buckets']['31_60']/100:.2f}"],
                 ["bucket_61_90", f"{ag['buckets']['61_90']/100:.2f}"],
                 ["bucket_90_plus", f"{ag['buckets']['90_plus']/100:.2f}"]]
        return _csv("ap_aging.csv",
                    ["number", "due_date", "status", "amount", "department", "category"], rows)

    raise HTTPException(404, "Unknown report. Use: pnl, budget-variance, expenses, ar-aging, ap-aging")
