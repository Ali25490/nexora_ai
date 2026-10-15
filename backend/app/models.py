"""NEXORA AI — database models. Money stored as integer cents for precision."""
from datetime import datetime, date
from sqlalchemy import (Column, Integer, String, Boolean, DateTime, Date, ForeignKey,
                        Text, BigInteger, Float, UniqueConstraint, JSON)
from sqlalchemy.orm import relationship
from .db import Base


class Organization(Base):
    __tablename__ = "organizations"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    members = relationship("Membership", back_populates="org")


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(200), default="")
    is_superadmin = Column(Boolean, default=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    memberships = relationship("Membership", back_populates="user")


class Membership(Base):
    """Roles: owner, cfo, analyst, accountant, manager, viewer"""
    __tablename__ = "organization_memberships"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    role = Column(String(50), nullable=False, default="viewer")
    department = Column(String(120), default="")
    org = relationship("Organization", back_populates="members")
    user = relationship("User", back_populates="memberships")
    __table_args__ = (UniqueConstraint("org_id", "user_id"),)


class SubscriptionPlan(Base):
    __tablename__ = "subscription_plans"
    id = Column(Integer, primary_key=True)
    code = Column(String(50), unique=True)          # starter / growth / business
    name = Column(String(100))
    price_cents = Column(Integer, default=0)         # monthly
    limits = Column(JSON, default=dict)              # {"users":5,"sources":2,"ai_credits":1000,...}
    modules = Column(JSON, default=list)


class Subscription(Base):
    __tablename__ = "subscriptions"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    plan_id = Column(Integer, ForeignKey("subscription_plans.id"))
    status = Column(String(30), default="active")    # active/trialing/canceled/past_due
    started_at = Column(DateTime, default=datetime.utcnow)
    plan = relationship("SubscriptionPlan")


class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    amount_cents = Column(BigInteger, default=0)
    status = Column(String(30), default="open")      # open/paid/void
    issued_at = Column(DateTime, default=datetime.utcnow)


class DataSource(Base):
    __tablename__ = "data_sources"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    name = Column(String(200))
    type = Column(String(50), default="csv")         # csv/xlsx/postgres/mysql/rest/ga4/crm...
    status = Column(String(30), default="active")    # active/error/pending
    config = Column(JSON, default=dict)
    last_sync_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class ImportBatch(Base):
    __tablename__ = "import_batches"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    source_id = Column(Integer, ForeignKey("data_sources.id"), nullable=True)
    filename = Column(String(255))
    rows_total = Column(Integer, default=0)
    rows_ok = Column(Integer, default=0)
    rows_failed = Column(Integer, default=0)
    status = Column(String(30), default="done")      # done/partial/failed
    errors = Column(JSON, default=list)
    created_at = Column(DateTime, default=datetime.utcnow)


class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    name = Column(String(200))
    email = Column(String(255), default="")
    segment = Column(String(50), default="new")      # new/returning/enterprise/free/churned
    channel = Column(String(80), default="Direct")
    country = Column(String(80), default="US")
    is_paying = Column(Boolean, default=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"))
    channel = Column(String(80), default="Direct", index=True)
    status = Column(String(30), default="completed") # completed/refunded/canceled
    total_cents = Column(BigInteger, default=0)
    created_date = Column(Date, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    customer = relationship("Customer")


class Transaction(Base):
    """Financial ledger: revenue & expenses by category/department."""
    __tablename__ = "transactions"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    kind = Column(String(20), default="expense")     # revenue/expense
    category = Column(String(120), default="General")
    department = Column(String(120), default="General")
    amount_cents = Column(BigInteger, default=0)
    date = Column(Date, index=True)


class MetricDefinition(Base):
    __tablename__ = "metric_definitions"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)  # null = global
    key = Column(String(80), index=True)
    name = Column(String(200))
    description = Column(Text, default="")
    formula = Column(Text, default="")
    unit = Column(String(30), default="currency")    # currency/percent/count
    aggregation = Column(String(30), default="sum")
    owner = Column(String(120), default="")
    version = Column(Integer, default=1)
    __table_args__ = (UniqueConstraint("org_id", "key"),)


class Dashboard(Base):
    __tablename__ = "dashboards"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    name = Column(String(200))
    widgets = Column(JSON, default=list)             # [{type, metric, title, span}]
    is_shared = Column(Boolean, default=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)


class ForecastRun(Base):
    __tablename__ = "forecast_runs"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    metric = Column(String(80))
    horizon_days = Column(Integer, default=30)
    algorithm = Column(String(50), default="linear_seasonal")
    params = Column(JSON, default=dict)
    points = Column(JSON, default=list)              # [{date, yhat, lower, upper}]
    mape = Column(Float, nullable=True)
    baseline_mape = Column(Float, nullable=True)     # naive baseline comparison
    created_at = Column(DateTime, default=datetime.utcnow)


class AnomalyFinding(Base):
    __tablename__ = "anomaly_findings"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    metric = Column(String(80), default="revenue")
    date = Column(Date, index=True)
    expected_value = Column(Float, default=0)
    actual_value = Column(Float, default=0)
    deviation_pct = Column(Float, default=0)
    method = Column(String(80), default="rolling_zscore")
    priority = Column(String(20), default="medium")  # high/medium/low
    confidence = Column(Float, default=0)
    status = Column(String(30), default="new")       # new/under_review/investigating/action_required/resolved/dismissed
    assignee_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    factors = Column(JSON, default=list)             # [{factor, change_pct, note}]
    created_at = Column(DateTime, default=datetime.utcnow)


class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    title = Column(String(300))
    description = Column(Text, default="")
    status = Column(String(30), default="open")      # open/in_progress/done/canceled
    priority = Column(String(20), default="medium")
    assignee_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    due_date = Column(Date, nullable=True)
    anomaly_id = Column(Integer, ForeignKey("anomaly_findings.id"), nullable=True)
    expected_effect = Column(Text, default="")
    actual_result = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)


class Comment(Base):
    __tablename__ = "comments"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    entity = Column(String(40))                      # task/anomaly
    entity_id = Column(Integer)
    user_id = Column(Integer, ForeignKey("users.id"))
    body = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)


class Alert(Base):
    __tablename__ = "alerts"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    name = Column(String(200))
    metric = Column(String(80), default="revenue")
    condition = Column(String(20), default="drops_below")  # drops_below/exceeds/change_pct
    threshold = Column(Float, default=0)
    check_frequency = Column(String(20), default="daily")
    recipients = Column(JSON, default=list)
    is_active = Column(Boolean, default=True)
    silence_hours = Column(Integer, default=24)      # anti-flapping


class AlertEvent(Base):
    __tablename__ = "alert_events"
    id = Column(Integer, primary_key=True)
    alert_id = Column(Integer, ForeignKey("alerts.id"), index=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    message = Column(Text)
    value = Column(Float, default=0)
    fired_at = Column(DateTime, default=datetime.utcnow)


class Budget(Base):
    __tablename__ = "budgets"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), index=True)
    department = Column(String(120))
    category = Column(String(120), default="General")
    period = Column(String(7))                       # YYYY-MM
    planned_cents = Column(BigInteger, default=0)
    status = Column(String(30), default="approved")


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    org_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    action = Column(String(120))
    entity = Column(String(60), default="")
    entity_id = Column(Integer, nullable=True)
    meta = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)


class Notification(Base):
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), index=True)
    title = Column(String(300))
    body = Column(Text, default="")
    is_read = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
