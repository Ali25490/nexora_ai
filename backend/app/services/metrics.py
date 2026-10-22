"""Real KPI calculations from DB. Money in cents."""
from datetime import date, timedelta
from collections import defaultdict
from sqlalchemy.orm import Session
from sqlalchemy import func
from .. import models

COMPLETED = "completed"


def _period_range(period: str):
    """period = YYYY-MM -> (start, end, prev_start, prev_end)"""
    y, m = map(int, period.split("-"))
    start = date(y, m, 1)
    end = date(y + (m == 12), (m % 12) + 1, 1) - timedelta(days=1)
    prev_end = start - timedelta(days=1)
    prev_start = date(prev_end.year, prev_end.month, 1)
    return start, end, prev_start, prev_end


def parse_range(start_s: str, end_s: str):
    return date.fromisoformat(start_s), date.fromisoformat(end_s)


def _sum_orders(db, org_id, start, end):
    q = (db.query(func.coalesce(func.sum(models.Order.total_cents), 0),
                  func.count(models.Order.id))
         .filter(models.Order.org_id == org_id,
                 models.Order.status == COMPLETED,
                 models.Order.created_date >= start,
                 models.Order.created_date <= end))
    rev, cnt = q.one()
    return int(rev), int(cnt)


def _new_customers(db, org_id, start, end):
    return (db.query(func.count(models.Customer.id))
            .filter(models.Customer.org_id == org_id,
                    func.date(models.Customer.created_at) >= start,
                    func.date(models.Customer.created_at) <= end).scalar())


def _churned(db, org_id, end):
    return (db.query(func.count(models.Customer.id))
            .filter(models.Customer.org_id == org_id,
                    models.Customer.is_active == False).scalar())  # noqa


def _total_customers(db, org_id):
    return db.query(func.count(models.Customer.id)).filter(models.Customer.org_id == org_id).scalar()


def kpi_overview(db: Session, org_id: int, start: date, end: date):
    days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)

    rev, orders = _sum_orders(db, org_id, start, end)
    prev_rev, prev_orders = _sum_orders(db, org_id, prev_start, prev_end)
    users_new = _new_customers(db, org_id, start, end)
    prev_users = _new_customers(db, org_id, prev_start, prev_end)
    total_users = _total_customers(db, org_id)
    churned = _churned(db, org_id, end)
    churn_rate = (churned / total_users * 100) if total_users else 0.0

    aov = (rev / orders) if orders else 0
    prev_aov = (prev_rev / prev_orders) if prev_orders else 0
    # Conversion: completed orders / new+returning active customers in period (documented proxy)
    # Conversion = share of active customers who placed >=1 completed order in period (documented)
    def _buyers(a, b):
        return (db.query(func.count(func.distinct(models.Order.customer_id)))
                .filter(models.Order.org_id == org_id, models.Order.status == COMPLETED,
                        models.Order.created_date >= a, models.Order.created_date <= b).scalar()) or 0
    sessions = _total_customers(db, org_id)  # denominator: all customers (active + churned)
    buyers = _buyers(start, end)
    conv = (buyers / sessions * 100) if sessions else 0.0
    prev_buyers = _buyers(prev_start, prev_end)
    prev_conv = (prev_buyers / sessions * 100) if sessions else 0.0

    def delta(cur, prev):
        abs_d = cur - prev
        pct = (abs_d / prev * 100) if prev else (100.0 if cur else 0.0)
        return round(pct, 1), abs_d

    dr = delta(rev, prev_rev)
    du = delta(users_new, prev_users)
    dc = delta(conv, prev_conv)
    da = delta(aov, prev_aov)
    churn_base = max(total_users - churned, 1)
    prev_churn_rate = (max(churned - 0, 0) / churn_base * 100)
    dch = delta(churn_rate, prev_churn_rate)

    return {
        "period": {"start": start.isoformat(), "end": end.isoformat(),
                   "prev_start": prev_start.isoformat(), "prev_end": prev_end.isoformat()},
        "revenue": {"value_cents": rev, "pct_change": dr[0], "abs_change_cents": dr[1]},
        "users": {"new": users_new, "total": total_users, "pct_change": du[0], "abs_change": du[1]},
        "conversion_rate": {"value": round(conv, 2), "orders": buyers, "base": sessions,
                            "pct_change": dc[0]},
        "avg_order_value": {"value_cents": int(aov), "orders": orders, "pct_change": da[0]},
        "churn_rate": {"value": round(churn_rate, 2), "churned": churned, "pct_change": dch[0],
                       "invert": True},
    }


def daily_series(db: Session, org_id: int, start: date, end: date):
    rows = (db.query(models.Order.created_date, func.sum(models.Order.total_cents))
            .filter(models.Order.org_id == org_id, models.Order.status == COMPLETED,
                    models.Order.created_date >= start, models.Order.created_date <= end)
            .group_by(models.Order.created_date).all())
    by_day = defaultdict(int)
    for d, s in rows:
        by_day[d] = int(s)
    series = []
    d = start
    while d <= end:
        series.append({"date": d.isoformat(), "revenue_cents": by_day.get(d, 0)})
        d += timedelta(days=1)
    return series


def revenue_by_channel(db: Session, org_id: int, start: date, end: date):
    rows = (db.query(models.Order.channel, func.sum(models.Order.total_cents), func.count())
            .filter(models.Order.org_id == org_id, models.Order.status == COMPLETED,
                    models.Order.created_date >= start, models.Order.created_date <= end)
            .group_by(models.Order.channel)
            .order_by(func.sum(models.Order.total_cents).desc()).all())
    total = sum(int(r[1]) for r in rows) or 1
    return [{"channel": r[0], "revenue_cents": int(r[1]), "orders": int(r[2]),
             "share_pct": round(int(r[1]) / total * 100, 1),
             "avg_order_cents": int(r[1]) // max(int(r[2]), 1)} for r in rows]


def users_by_segment(db: Session, org_id: int):
    rows = (db.query(models.Customer.segment, func.count())
            .filter(models.Customer.org_id == org_id)
            .group_by(models.Customer.segment).all())
    total = sum(int(r[1]) for r in rows) or 1
    return [{"segment": r[0], "count": int(r[1]),
             "share_pct": round(int(r[1]) / total * 100, 1)} for r in rows]


def explore(db: Session, org_id: int, metric: str, dimension: str, start: date, end: date):
    """Self-serve exploration: metric in {revenue, orders, expenses}, dimension in {date, channel, segment, department}."""
    dim_map = {"date": models.Order.created_date, "channel": models.Order.channel,
               "segment": models.Customer.segment}
    if metric in ("revenue", "orders"):
        dim = dim_map.get(dimension, models.Order.created_date)
        q = (db.query(dim, func.sum(models.Order.total_cents), func.count())
             .filter(models.Order.org_id == org_id, models.Order.status == COMPLETED,
                     models.Order.created_date >= start, models.Order.created_date <= end))
        if dimension == "segment":
            q = q.join(models.Customer, models.Order.customer_id == models.Customer.id)
        rows = q.group_by(dim).all()
        return [{"key": str(r[0]), "value_cents": int(r[1] or 0), "count": int(r[2])} for r in rows]
    if metric == "expenses":
        rows = (db.query(models.Transaction.category, func.sum(models.Transaction.amount_cents), func.count())
                .filter(models.Transaction.org_id == org_id, models.Transaction.kind == "expense",
                        models.Transaction.date >= start, models.Transaction.date <= end)
                .group_by(models.Transaction.category).all())
        return [{"key": r[0], "value_cents": int(r[1]), "count": int(r[2])} for r in rows]
    return []


def cohorts(db: Session, org_id: int):
    """Monthly cohorts by first order month; retention = repeat orders in later months."""
    orders = (db.query(models.Order.customer_id, models.Order.created_date)
              .filter(models.Order.org_id == org_id, models.Order.status == COMPLETED).all())
    by_cust = defaultdict(list)
    for cid, d in orders:
        by_cust[cid].append(d)
    cohorts_map = defaultdict(lambda: defaultdict(int))
    sizes = defaultdict(int)
    for cid, dates in by_cust.items():
        first = min(dates)
        key = f"{first.year}-{first.month:02d}"
        sizes[key] += 1
        months = {(d.year - first.year) * 12 + (d.month - first.month) for d in dates}
        for m in months:
            cohorts_map[key][m] += 1
    out = []
    for key in sorted(sizes):
        size = sizes[key]
        out.append({"cohort": key, "size": size,
                    "retention": [{"month": m, "pct": round(cohorts_map[key].get(m, 0) / size * 100, 1)}
                                  for m in range(0, 7)]})
    return out
