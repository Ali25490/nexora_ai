"""Anomaly detection: rolling z-score vs trailing baseline, with contributing-factor analysis."""
from datetime import timedelta
from statistics import mean, pstdev
from collections import defaultdict
from sqlalchemy.orm import Session
from sqlalchemy import func
from .. import models
from .metrics import daily_series


def detect_revenue_anomalies(db: Session, org_id: int, window: int = 14, z_thresh: float = 2.5):
    series = daily_series(db, org_id, _min_date(db, org_id), _max_date(db, org_id))
    if len(series) < window + 3:
        return []
    findings = []
    vals = [p["revenue_cents"] for p in series]
    for i in range(window, len(series)):
        base = vals[i - window:i]
        mu, sd = mean(base), pstdev(base) or 1.0
        z = (vals[i] - mu) / sd
        if abs(z) >= z_thresh:
            d = series[i]["date"]
            findings.append({
                "date": d, "expected": mu, "actual": float(vals[i]),
                "deviation_pct": round((vals[i] - mu) / mu * 100, 1) if mu else 0.0,
                "z": round(z, 2), "confidence": min(0.99, abs(z) / (z_thresh * 2)),
            })
    return findings


def _min_date(db, org_id):
    r = db.query(func.min(models.Order.created_date)).filter_by(org_id=org_id).scalar()
    return r or __import__("datetime").date.today()


def _max_date(db, org_id):
    r = db.query(func.max(models.Order.created_date)).filter_by(org_id=org_id).scalar()
    return r or __import__("datetime").date.today()


def analyze_factors(db: Session, org_id: int, day) -> list:
    """Compare the anomalous day vs trailing 14-day average on available factors.
    NOTE: results are correlations, not proven causation."""
    day = day if hasattr(day, "month") else __import__("datetime").date.fromisoformat(str(day))
    start = day - timedelta(days=14)
    factors = []

    def avg(rows):
        return sum(rows) / len(rows) if rows else 0.0

    # orders count
    base = (db.query(models.Order.created_date, func.count())
            .filter(models.Order.org_id == org_id, models.Order.status == "completed",
                    models.Order.created_date >= start, models.Order.created_date < day)
            .group_by(models.Order.created_date).all())
    day_cnt = (db.query(func.count(models.Order.id))
               .filter(models.Order.org_id == org_id, models.Order.status == "completed",
                       models.Order.created_date == day).scalar())
    b = avg([int(r[1]) for r in base])
    if b:
        factors.append({"factor": "Orders count", "change_pct": round((day_cnt - b) / b * 100, 1)})

    # avg order value
    base_aov = (db.query(func.avg(models.Order.total_cents))
                .filter(models.Order.org_id == org_id, models.Order.status == "completed",
                        models.Order.created_date >= start, models.Order.created_date < day).scalar() or 0)
    day_aov = (db.query(func.avg(models.Order.total_cents))
               .filter(models.Order.org_id == org_id, models.Order.status == "completed",
                       models.Order.created_date == day).scalar() or 0)
    if base_aov:
        factors.append({"factor": "Avg Order Value", "change_pct": round((day_aov - base_aov) / base_aov * 100, 1)})

    # new customers
    base_new = (db.query(models.Customer)
                .filter(models.Customer.org_id == org_id,
                        func.date(models.Customer.created_at) >= start,
                        func.date(models.Customer.created_at) < day).count() / 14.0)
    day_new = (db.query(models.Customer)
               .filter(models.Customer.org_id == org_id,
                       func.date(models.Customer.created_at) == day).count())
    if base_new:
        factors.append({"factor": "New Customers", "change_pct": round((day_new - base_new) / base_new * 100, 1)})

    # channel mix: top channel that day vs baseline share
    ch = (db.query(models.Order.channel, func.sum(models.Order.total_cents))
          .filter(models.Order.org_id == org_id, models.Order.status == "completed",
                  models.Order.created_date == day)
          .group_by(models.Order.channel).order_by(func.sum(models.Order.total_cents).desc()).all())
    if ch:
        factors.append({"factor": f"Top channel: {ch[0][0]}", "change_pct": None,
                        "note": f"contributed {int(ch[0][1]) / 100:.2f} that day"})
    for f in factors:
        f.setdefault("note", "correlation, not proven causation")
    return factors


def run_detection(db: Session, org_id: int):
    """Detect + persist new anomaly findings (idempotent per org/date/metric)."""
    found = detect_revenue_anomalies(db, org_id)
    created = []
    for f in found:
        d = __import__("datetime").date.fromisoformat(f["date"])
        exists = (db.query(models.AnomalyFinding)
                  .filter_by(org_id=org_id, metric="revenue", date=d).first())
        if exists:
            continue
        dev = abs(f["deviation_pct"])
        priority = "high" if dev >= 30 else ("medium" if dev >= 15 else "low")
        a = models.AnomalyFinding(
            org_id=org_id, metric="revenue", date=d,
            expected_value=f["expected"] / 100, actual_value=f["actual"] / 100,
            deviation_pct=f["deviation_pct"], method="rolling_zscore",
            priority=priority, confidence=round(f["confidence"], 2),
            factors=analyze_factors(db, org_id, d))
        db.add(a)
        created.append(a)
    db.commit()
    return created
