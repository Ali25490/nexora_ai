"""Demo data seeding: deterministic, clearly-marked sample data for evaluation."""
import random
from datetime import date, datetime, timedelta
from sqlalchemy.orm import Session
from .. import models
from ..security import hash_password

CHANNELS = [("Organic Search", 0.34), ("Paid Search", 0.18), ("Social Media", 0.14),
            ("Email", 0.10), ("Direct", 0.12), ("Referral", 0.07), ("Sales Team", 0.05)]
SEGMENTS = [("new", 0.30), ("returning", 0.28), ("active", 0.18), ("enterprise", 0.10),
            ("free", 0.09), ("churned", 0.05)]

PLANS = [
    {"code": "starter", "name": "Starter", "price_cents": 19900,
     "limits": {"users": 5, "sources": 2, "ai_credits": 1000, "history_months": 12},
     "modules": ["overview", "dashboards", "reports", "explore"]},
    {"code": "growth", "name": "Growth", "price_cents": 49900,
     "limits": {"users": 20, "sources": 10, "ai_credits": 10000, "history_months": 36},
     "modules": ["overview", "dashboards", "reports", "explore", "cohorts",
                  "anomalies", "forecasting", "alerts", "tasks"]},
    {"code": "business", "name": "Business", "price_cents": 99900,
     "limits": {"users": 100, "sources": 50, "ai_credits": 100000, "history_months": 120},
     "modules": ["*"]},
]

METRICS = [
    ("revenue", "Total Revenue", "SUM(orders.total) WHERE status='completed'", "currency"),
    ("gross_profit", "Gross Profit", "revenue - COGS", "currency"),
    ("ebitda", "EBITDA", "gross_profit - opex", "currency"),
    ("conversion_rate", "Conversion Rate", "distinct ordering customers / active customers", "percent"),
    ("churn_rate", "Churn Rate", "churned customers / total customers", "percent"),
    ("aov", "Average Order Value", "revenue / completed orders", "currency"),
    ("cac", "CAC", "marketing spend / new customers", "currency"),
    ("ltv", "LTV", "ARPU_monthly * gross_margin / monthly_churn", "currency"),
    ("budget_variance", "Budget Variance", "actual - planned per dept/category", "currency"),
]


def ensure_plans(db: Session):
    if not db.query(models.SubscriptionPlan).count():
        for p in PLANS:
            db.add(models.SubscriptionPlan(**p))
        db.commit()


def _wchoice(rnd, pairs):
    r = rnd.random()
    acc = 0
    for v, w in pairs:
        acc += w
        if r <= acc:
            return v
    return pairs[-1][0]


def seed_demo(db: Session, org_id: int, days: int = 150):
    """Idempotent: skips if org already has orders."""
    if db.query(models.Order).filter_by(org_id=org_id).count() > 0:
        return {"skipped": True}
    rnd = random.Random(42)
    today = date.today()
    start = today - timedelta(days=days)

    customers = []
    for i in range(90):
        seg = _wchoice(rnd, SEGMENTS)
        c = models.Customer(
            org_id=org_id, name=f"Customer {i+1:03d}",
            email=f"cust{i+1}@example.com", segment=seg,
            channel=_wchoice(rnd, CHANNELS), country=rnd.choice(["US", "DE", "GB", "FR"]),
            is_paying=seg not in ("free",), is_active=seg != "churned",
            created_at=datetime.combine(start + timedelta(days=rnd.randint(0, days)),
                                        datetime.min.time()))
        db.add(c)
        customers.append(c)
    db.flush()

    for d in range(days + 1):
        day = start + timedelta(days=d)
        weekly = 1.0 + 0.35 * (1 if day.weekday() in (4, 5) else 0) - 0.15 * (1 if day.weekday() == 0 else 0)
        trend = 1.0 + d / days * 0.6
        n_orders = max(1, int(rnd.gauss(7 * weekly * trend, 2)))
        spike = 1.45 if d == days - 3 else 1.0   # engineered anomaly
        for _ in range(n_orders):
            amt = int(rnd.lognormvariate(10.4, 0.5) * spike)
            db.add(models.Order(org_id=org_id, customer_id=rnd.choice(customers).id,
                                channel=_wchoice(rnd, CHANNELS), status="completed",
                                total_cents=amt, created_date=day))
        # expenses
        db.add(models.Transaction(org_id=org_id, kind="expense", category="COGS",
                                  department="Operations", amount_cents=int(n_orders * 9000),
                                  date=day))
        db.add(models.Transaction(org_id=org_id, kind="expense", category="Marketing",
                                  department="Marketing",
                                  amount_cents=int(rnd.gauss(12000, 3000)), date=day))
        if day.day == 1:
            db.add(models.Transaction(org_id=org_id, kind="expense", category="Payroll",
                                      department="Engineering", amount_cents=180_000_00, date=day))
            db.add(models.Transaction(org_id=org_id, kind="expense", category="Payroll",
                                      department="Sales", amount_cents=90_000_00, date=day))
            db.add(models.Transaction(org_id=org_id, kind="expense", category="Rent",
                                      department="G&A", amount_cents=25_000_00, date=day))

    period = f"{today.year}-{today.month:02d}"
    for dept, cat, planned in [("Marketing", "Marketing", 400_000_00),
                               ("Engineering", "Payroll", 170_000_00),
                               ("Sales", "Payroll", 100_000_00),
                               ("G&A", "Rent", 25_000_00)]:
        db.add(models.Budget(org_id=org_id, department=dept, category=cat,
                             period=period, planned_cents=planned))

    for key, name, formula, unit in METRICS:
        db.add(models.MetricDefinition(org_id=None, key=key, name=name,
                                       formula=formula, unit=unit, owner="NEXORA"))
    db.add(models.Dashboard(org_id=org_id, name="Executive Overview", is_shared=True, widgets=[
        {"type": "kpi", "metric": "revenue"}, {"type": "line", "metric": "revenue"},
        {"type": "bar", "metric": "channels"}, {"type": "donut", "metric": "segments"}]))
    db.add(models.Alert(org_id=org_id, name="Revenue drop > 20% week-over-week",
                        metric="revenue", condition="change_pct", threshold=-20,
                        check_frequency="daily"))
    db.commit()
    return {"skipped": False, "orders": db.query(models.Order).filter_by(org_id=org_id).count()}


def create_demo_account(db: Session):
    """Demo login for evaluation. Clearly documented in README."""
    user = db.query(models.User).filter_by(email="admin@nexora.ai").first()
    if user:
        return user
    org = models.Organization(name="Nexora Demo Inc.")
    db.add(org)
    db.flush()
    user = models.User(email="admin@nexora.ai", password_hash=hash_password("admin123"),
                       full_name="Admin User", is_superadmin=True)
    db.add(user)
    db.flush()
    db.add(models.Membership(org_id=org.id, user_id=user.id, role="owner"))
    plan = db.query(models.SubscriptionPlan).filter_by(code="business").first()
    if plan:
        db.add(models.Subscription(org_id=org.id, plan_id=plan.id, status="active"))
    db.commit()
    seed_demo(db, org.id)
    return user
