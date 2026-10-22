"""NEXORA AI — FastAPI application. All endpoints org-scoped + RBAC-checked."""
import os
from datetime import date, timedelta, datetime
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from .db import Base, engine, get_db
from . import models
from .security import (hash_password, verify_password, create_token, get_current_user,
                       require_role, get_membership, audit, WRITE_ROLES, ADMIN_ROLES)
from .services import metrics as msvc, anomaly as asvc, forecast as fsvc
from .services import finance as finsvc, importer as isvc, ai as aisvc, seed as seedsvc
from .services import finops, seed_roles
from .security import canonical_role, require_perm, has_permission, PRIMARY_ROLES

app = FastAPI(title="NEXORA AI API", version="1.0.0",
              description="Intelligent business analytics platform. All money values in cents.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], allow_credentials=True)

ALL_ROLES = {"owner", "cfo", "analyst", "accountant", "manager", "top_manager", "viewer"}
READ_ROLES = ALL_ROLES


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    seedsvc.ensure_plans(db)
    if os.getenv("SEED_DEMO", "1") == "1":
        seedsvc.create_demo_account(db)
        seed_roles.seed_three_role_demo(db)
    db.close()


def _range(start: Optional[str], end: Optional[str]):
    e = date.fromisoformat(end) if end else date.today()
    s = date.fromisoformat(start) if start else e - timedelta(days=30)
    if s > e:
        raise HTTPException(400, "start must be <= end")
    return s, e


# ---------------- Auth ----------------
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    full_name: str = ""
    org_name: str = Field(min_length=2)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


@app.post("/api/auth/register", tags=["auth"])
def register(body: RegisterIn, db: Session = Depends(get_db)):
    if db.query(models.User).filter_by(email=body.email).first():
        raise HTTPException(409, "Email already registered")
    org = models.Organization(name=body.org_name)
    db.add(org); db.flush()
    user = models.User(email=body.email, password_hash=hash_password(body.password),
                       full_name=body.full_name)
    db.add(user); db.flush()
    db.add(models.Membership(org_id=org.id, user_id=user.id, role="owner"))
    plan = db.query(models.SubscriptionPlan).filter_by(code="starter").first()
    if plan:
        db.add(models.Subscription(org_id=org.id, plan_id=plan.id, status="trialing"))
    audit(db, org.id, user.id, "org.register", "organization", org.id)
    db.commit()
    return {"token": create_token(user.id), "user_id": user.id, "org_id": org.id}


@app.post("/api/auth/login", tags=["auth"])
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = db.query(models.User).filter_by(email=body.email).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid credentials")
    m = db.query(models.Membership).filter_by(user_id=user.id).first()
    return {"token": create_token(user.id), "user_id": user.id,
            "org_id": m.org_id if m else None, "role": m.role if m else None,
            "full_name": user.full_name}


@app.get("/api/auth/me", tags=["auth"])
def me(user: models.User = Depends(get_current_user), db: Session = Depends(get_db)):
    ms = db.query(models.Membership).filter_by(user_id=user.id).all()
    return {"id": user.id, "email": user.email, "full_name": user.full_name,
            "is_superadmin": user.is_superadmin,
            "orgs": [{"org_id": m.org_id, "role": m.role,
                      "org_name": db.get(models.Organization, m.org_id).name} for m in ms]}


# ---------------- KPI / Analytics ----------------
@app.get("/api/orgs/{org_id}/kpi", tags=["analytics"])
def kpi(org_id: int, start: Optional[str] = None, end: Optional[str] = None,
        user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    s, e = _range(start, end)
    return msvc.kpi_overview(db, org_id, s, e)


@app.get("/api/orgs/{org_id}/revenue/daily", tags=["analytics"])
def revenue_daily(org_id: int, start: Optional[str] = None, end: Optional[str] = None,
                  user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    s, e = _range(start, end)
    series = msvc.daily_series(db, org_id, s, e)
    anomalies = (db.query(models.AnomalyFinding)
                 .filter(models.AnomalyFinding.org_id == org_id,
                         models.AnomalyFinding.date >= s,
                         models.AnomalyFinding.date <= e).all())
    return {"series": series,
            "anomalies": [{"date": a.date.isoformat(), "actual": a.actual_value,
                           "expected": a.expected_value, "deviation_pct": a.deviation_pct}
                          for a in anomalies]}


@app.get("/api/orgs/{org_id}/revenue/by-channel", tags=["analytics"])
def by_channel(org_id: int, start: Optional[str] = None, end: Optional[str] = None,
               user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    s, e = _range(start, end)
    return msvc.revenue_by_channel(db, org_id, s, e)


@app.get("/api/orgs/{org_id}/users/by-segment", tags=["analytics"])
def by_segment(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    return msvc.users_by_segment(db, org_id)


@app.get("/api/orgs/{org_id}/explore", tags=["analytics"])
def explore(org_id: int, metric: str = "revenue", dimension: str = "channel",
            start: Optional[str] = None, end: Optional[str] = None,
            user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, {"owner", "cfo", "analyst", "manager", "top_manager"})
    s, e = _range(start, end)
    return {"metric": metric, "dimension": dimension,
            "rows": msvc.explore(db, org_id, metric, dimension, s, e)}


@app.get("/api/orgs/{org_id}/cohorts", tags=["analytics"])
def cohorts(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    return msvc.cohorts(db, org_id)


# ---------------- Anomalies ----------------
@app.post("/api/orgs/{org_id}/anomalies/run", tags=["anomalies"])
def run_anomalies(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, WRITE_ROLES)
    created = asvc.run_detection(db, org_id)
    audit(db, org_id, user.id, "anomaly.run", "anomaly_findings", meta={"created": len(created)})
    db.commit()
    return {"created": len(created)}


@app.get("/api/orgs/{org_id}/anomalies/status", tags=["anomalies"])
def anomaly_detection_status(org_id: int,
                             user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Lets the UI distinguish 'detection never ran' from 'ran, zero findings'."""
    require_role(user, org_id, db, READ_ROLES)
    last_run = (db.query(models.AuditLog)
                .filter_by(org_id=org_id, action="anomaly.run")
                .order_by(models.AuditLog.created_at.desc()).first())
    findings = db.query(models.AnomalyFinding).filter_by(org_id=org_id).count()
    return {"never_run": last_run is None, "findings": findings,
            "last_run_at": last_run.created_at.isoformat() if last_run else None}


@app.get("/api/orgs/{org_id}/anomalies", tags=["anomalies"])
def list_anomalies(org_id: int, status: Optional[str] = None,
                   user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    q = db.query(models.AnomalyFinding).filter_by(org_id=org_id)
    if status:
        q = q.filter_by(status=status)
    rows = q.order_by(models.AnomalyFinding.date.desc()).limit(100).all()
    return [{"id": a.id, "metric": a.metric, "date": a.date.isoformat(),
             "expected": a.expected_value, "actual": a.actual_value,
             "deviation_pct": a.deviation_pct, "priority": a.priority,
             "confidence": a.confidence, "status": a.status, "method": a.method,
             "assignee_id": a.assignee_id, "factors": a.factors} for a in rows]


class AnomalyPatch(BaseModel):
    status: Optional[str] = None
    assignee_id: Optional[int] = None


@app.patch("/api/orgs/{org_id}/anomalies/{aid}", tags=["anomalies"])
def patch_anomaly(org_id: int, aid: int, body: AnomalyPatch,
                  user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, WRITE_ROLES)
    a = db.query(models.AnomalyFinding).filter_by(id=aid, org_id=org_id).first()
    if not a:
        raise HTTPException(404, "Anomaly not found")
    if body.status:
        allowed = {"new", "under_review", "investigating", "action_required", "resolved", "dismissed"}
        if body.status not in allowed:
            raise HTTPException(400, f"status must be one of {sorted(allowed)}")
        a.status = body.status
    if body.assignee_id is not None:
        a.assignee_id = body.assignee_id
    audit(db, org_id, user.id, "anomaly.update", "anomaly_findings", aid)
    db.commit()
    return {"id": a.id, "status": a.status}


# ---------------- Forecasting ----------------
@app.get("/api/orgs/{org_id}/forecast/revenue", tags=["forecasting"])
def forecast_revenue(org_id: int, horizon: int = Query(30, ge=7, le=180),
                     user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    s, e = date.today() - timedelta(days=120), date.today()
    result = fsvc.forecast(db, org_id, s, e, horizon)
    if result.get("ok"):
        run = models.ForecastRun(org_id=org_id, metric="revenue", horizon_days=horizon,
                                 points=result["forecast"], mape=result.get("mape_pct"),
                                 baseline_mape=result.get("baseline_naive_mape_pct"))
        db.add(run); db.commit()
    return result


@app.get("/api/orgs/{org_id}/forecast/runs", tags=["forecasting"])
def forecast_runs(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    runs = (db.query(models.ForecastRun).filter_by(org_id=org_id)
            .order_by(models.ForecastRun.created_at.desc()).limit(20).all())
    return [{"id": r.id, "metric": r.metric, "horizon_days": r.horizon_days,
             "algorithm": r.algorithm, "mape": r.mape, "baseline_mape": r.baseline_mape,
             "created_at": r.created_at.isoformat()} for r in runs]


# ---------------- Finance ----------------
@app.get("/api/orgs/{org_id}/finance/pnl", tags=["finance"])
def pnl(org_id: int, start: Optional[str] = None, end: Optional[str] = None,
        user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, {"owner", "cfo", "accountant", "analyst", "top_manager"})
    s, e = _range(start, end)
    return finsvc.pnl(db, org_id, s, e)


@app.get("/api/orgs/{org_id}/finance/budget-vs-actual", tags=["finance"])
def bva(org_id: int, period: Optional[str] = None,
        user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, {"owner", "cfo", "accountant", "manager", "top_manager"})
    period = period or f"{date.today().year}-{date.today().month:02d}"
    return finsvc.budget_vs_actual(db, org_id, period)


@app.get("/api/orgs/{org_id}/finance/cash-flow", tags=["finance"])
def cashflow(org_id: int, opening_cash_cents: int = 500_000_00,
             user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, {"owner", "cfo", "accountant", "top_manager"})
    return finsvc.cash_flow_forecast(db, org_id, opening_cash_cents=opening_cash_cents)


class WhatIfIn(BaseModel):
    price_pct: float = 0
    volume_pct: float = 0
    cost_pct: float = 0
    marketing_pct: float = 0


@app.post("/api/orgs/{org_id}/finance/what-if", tags=["finance"])
def what_if(org_id: int, body: WhatIfIn, start: Optional[str] = None, end: Optional[str] = None,
            user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, {"owner", "cfo", "analyst", "top_manager"})
    s, e = _range(start, end)
    return finsvc.what_if(db, org_id, s, e, body.price_pct, body.volume_pct,
                          body.cost_pct, body.marketing_pct)


@app.get("/api/orgs/{org_id}/finance/unit-economics", tags=["finance"])
def unit_econ(org_id: int, start: Optional[str] = None, end: Optional[str] = None,
              user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, {"owner", "cfo", "analyst", "top_manager"})
    s, e = _range(start, end)
    return finsvc.unit_economics(db, org_id, s, e)


# ---------------- Import / Data Quality ----------------
@app.post("/api/orgs/{org_id}/import", tags=["data"])
async def import_file(org_id: int, kind: str = "orders", file: UploadFile = File(...),
                      user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, {"owner", "cfo", "accountant", "analyst", "top_manager"})
    if not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Only .csv supported in this build (xlsx adapter ready)")
    content = await file.read()
    try:
        batch = isvc.import_csv(db, org_id, file.filename, content, kind)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit(db, org_id, user.id, "data.import", "import_batches", batch.id,
          {"filename": file.filename, "kind": kind})
    db.commit()
    return {"batch_id": batch.id, "status": batch.status, "rows_ok": batch.rows_ok,
            "rows_failed": batch.rows_failed, "errors": batch.errors[:10]}


@app.get("/api/orgs/{org_id}/data-quality", tags=["data"])
def data_quality(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    return isvc.data_quality_report(db, org_id)


@app.get("/api/orgs/{org_id}/sources", tags=["data"])
def sources(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    rows = db.query(models.DataSource).filter_by(org_id=org_id).all()
    return [{"id": s.id, "name": s.name, "type": s.type, "status": s.status,
             "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else None}
            for s in rows]


class SourceIn(BaseModel):
    name: str
    type: str = "csv"
    config: dict = {}


@app.post("/api/orgs/{org_id}/sources", tags=["data"])
def create_source(org_id: int, body: SourceIn,
                  user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, WRITE_ROLES)
    s = models.DataSource(org_id=org_id, name=body.name, type=body.type, config=body.config)
    db.add(s)
    audit(db, org_id, user.id, "source.create", "data_sources", None, {"type": body.type})
    db.commit()
    return {"id": s.id, "status": s.status}


# ---------------- Ask AI ----------------
class AskIn(BaseModel):
    question: str = Field(min_length=3, max_length=1000)


@app.post("/api/orgs/{org_id}/ai/ask", tags=["ai"])
def ask_ai(org_id: int, body: AskIn,
           user=Depends(get_current_user), db: Session = Depends(get_db)):
    m = require_role(user, org_id, db, READ_ROLES)
    # access control: finance answers require finance-visible roles
    q = body.question.lower()
    if any(w in q for w in ("cash", "budget", "expense")) and m.role in ("viewer", "manager"):
        raise HTTPException(403, "Your role cannot access financial answers")
    audit(db, org_id, user.id, "ai.ask", "ai_copilot", meta={"q": body.question[:100]})
    db.commit()
    return aisvc.ask(db, org_id, body.question)


# ---------------- Alerts / Tasks / Metrics catalog ----------------
class AlertIn(BaseModel):
    name: str
    metric: str = "revenue"
    condition: str = "drops_below"
    threshold: float = 0
    check_frequency: str = "daily"


@app.get("/api/orgs/{org_id}/alerts", tags=["alerts"])
def list_alerts(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    return [{"id": a.id, "name": a.name, "metric": a.metric, "condition": a.condition,
             "threshold": a.threshold, "is_active": a.is_active}
            for a in db.query(models.Alert).filter_by(org_id=org_id).all()]


@app.post("/api/orgs/{org_id}/alerts", tags=["alerts"])
def create_alert(org_id: int, body: AlertIn,
                 user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, WRITE_ROLES)
    a = models.Alert(org_id=org_id, **body.model_dump())
    db.add(a); db.commit()
    return {"id": a.id}


class TaskIn(BaseModel):
    title: str
    description: str = ""
    priority: str = "medium"
    assignee_id: Optional[int] = None
    anomaly_id: Optional[int] = None
    due_date: Optional[str] = None


@app.get("/api/orgs/{org_id}/tasks", tags=["tasks"])
def list_tasks(org_id: int, status: Optional[str] = None,
               user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    q = db.query(models.Task).filter_by(org_id=org_id)
    if status:
        q = q.filter_by(status=status)
    return [{"id": t.id, "title": t.title, "status": t.status, "priority": t.priority,
             "assignee_id": t.assignee_id, "due_date": t.due_date.isoformat() if t.due_date else None,
             "anomaly_id": t.anomaly_id}
            for t in q.order_by(models.Task.created_at.desc()).limit(100).all()]


@app.post("/api/orgs/{org_id}/tasks", tags=["tasks"])
def create_task(org_id: int, body: TaskIn,
                user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, WRITE_ROLES)
    t = models.Task(org_id=org_id, title=body.title, description=body.description,
                    priority=body.priority, assignee_id=body.assignee_id,
                    anomaly_id=body.anomaly_id,
                    due_date=date.fromisoformat(body.due_date) if body.due_date else None)
    db.add(t)
    audit(db, org_id, user.id, "task.create", "tasks")
    db.commit()
    return {"id": t.id}


@app.patch("/api/orgs/{org_id}/tasks/{tid}", tags=["tasks"])
def patch_task(org_id: int, tid: int, body: dict,
               user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, WRITE_ROLES)
    t = db.query(models.Task).filter_by(id=tid, org_id=org_id).first()
    if not t:
        raise HTTPException(404, "Task not found")
    for k in ("status", "priority", "assignee_id", "actual_result", "expected_effect"):
        if k in body:
            setattr(t, k, body[k])
    audit(db, org_id, user.id, "task.update", "tasks", tid)
    db.commit()
    return {"id": t.id, "status": t.status}


@app.get("/api/metrics-catalog", tags=["metrics"])
def metrics_catalog(db: Session = Depends(get_db)):
    rows = db.query(models.MetricDefinition).all()
    return [{"key": m.key, "name": m.name, "formula": m.formula, "unit": m.unit,
             "owner": m.owner, "version": m.version} for m in rows]


# ---------------- Team / Subscription / Admin ----------------
@app.get("/api/orgs/{org_id}/team", tags=["team"])
def team(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, ADMIN_ROLES)
    ms = db.query(models.Membership).filter_by(org_id=org_id).all()
    return [{"user_id": m.user_id, "email": m.user.email, "name": m.user.full_name,
             "role": m.role, "department": m.department, "active": m.user.is_active}
            for m in ms]


class MemberIn(BaseModel):
    email: EmailStr
    role: str = "viewer"
    full_name: str = ""


@app.post("/api/orgs/{org_id}/team", tags=["team"])
def add_member(org_id: int, body: MemberIn,
               user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, ADMIN_ROLES)
    if body.role not in ALL_ROLES:
        raise HTTPException(400, "Invalid role")
    u = db.query(models.User).filter_by(email=body.email).first()
    if not u:
        u = models.User(email=body.email, password_hash=hash_password("ChangeMe123!"),
                        full_name=body.full_name)
        db.add(u); db.flush()
    if db.query(models.Membership).filter_by(org_id=org_id, user_id=u.id).first():
        raise HTTPException(409, "Already a member")
    db.add(models.Membership(org_id=org_id, user_id=u.id, role=body.role))
    audit(db, org_id, user.id, "team.add_member", "users", u.id, {"role": body.role})
    db.commit()
    return {"user_id": u.id, "role": body.role}


@app.get("/api/orgs/{org_id}/subscription", tags=["billing"])
def subscription(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, ADMIN_ROLES)
    sub = db.query(models.Subscription).filter_by(org_id=org_id).first()
    if not sub:
        return {"status": "none"}
    return {"status": sub.status, "plan": sub.plan.name, "price_cents": sub.plan.price_cents,
            "limits": sub.plan.limits, "modules": sub.plan.modules}


@app.get("/api/plans", tags=["billing"])
def plans(db: Session = Depends(get_db)):
    return [{"code": p.code, "name": p.name, "price_cents": p.price_cents,
             "limits": p.limits, "modules": p.modules}
            for p in db.query(models.SubscriptionPlan).all()]


@app.get("/api/admin/organizations", tags=["admin"])
def admin_orgs(user=Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.is_superadmin:
        raise HTTPException(403, "Superadmin only")
    orgs = db.query(models.Organization).all()
    return [{"id": o.id, "name": o.name,
             "members": db.query(models.Membership).filter_by(org_id=o.id).count(),
             "orders": db.query(models.Order).filter_by(org_id=o.id).count()} for o in orgs]


@app.get("/api/admin/audit", tags=["admin"])
def admin_audit(org_id: Optional[int] = None, limit: int = Query(50, le=200),
                user=Depends(get_current_user), db: Session = Depends(get_db)):
    if not user.is_superadmin:
        raise HTTPException(403, "Superadmin only")
    q = db.query(models.AuditLog)
    if org_id:
        q = q.filter_by(org_id=org_id)
    return [{"id": a.id, "org_id": a.org_id, "user_id": a.user_id, "action": a.action,
             "entity": a.entity, "at": a.created_at.isoformat()}
            for a in q.order_by(models.AuditLog.created_at.desc()).limit(limit).all()]


@app.get("/api/orgs/{org_id}/notifications", tags=["notifications"])
def notifications(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    get_membership(user, org_id, db)
    rows = (db.query(models.Notification).filter_by(user_id=user.id)
            .order_by(models.Notification.created_at.desc()).limit(20).all())
    return [{"id": n.id, "title": n.title, "is_read": n.is_read} for n in rows]


@app.get("/api/health", tags=["system"])
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}


# ---------------- Three-role platform (spec §3-§13) ----------------
def _canon(user, org_id, db) -> str:
    if user.is_superadmin:
        return "owner"
    m = get_membership(user, org_id, db)
    return canonical_role(m.role)


class DepartmentIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    budget_monthly_cents: int = 0
    manager_user_id: Optional[int] = None


@app.get("/api/orgs/{org_id}/departments", tags=["org"])
def list_departments(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    return [{"id": d.id, "name": d.name, "budget_monthly_cents": d.budget_monthly_cents,
             "manager_user_id": d.manager_user_id}
            for d in db.query(models.Department).filter_by(org_id=org_id).all()]


@app.post("/api/orgs/{org_id}/departments", tags=["org"])
def create_department(org_id: int, body: DepartmentIn,
                      user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_perm(user, org_id, db, "manage_settings")
    if db.query(models.Department).filter_by(org_id=org_id, name=body.name).first():
        raise HTTPException(409, "Department already exists")
    d = models.Department(org_id=org_id, **body.model_dump())
    db.add(d)
    audit(db, org_id, user.id, "department.create", "departments", None, {"name": body.name})
    db.commit()
    return {"id": d.id}


# ---- Role dashboards ----
@app.get("/api/orgs/{org_id}/dashboard", tags=["dashboards"])
def dashboard_auto(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Returns the dashboard matching the caller's canonical role."""
    m = require_role(user, org_id, db, READ_ROLES)
    cr = "owner" if user.is_superadmin else canonical_role(m.role)
    if cr == "owner":
        return finops.owner_dashboard(db, org_id)
    if cr == "top_manager":
        return finops.top_manager_dashboard(db, org_id)
    return finops.manager_dashboard(db, org_id, user, m)


@app.get("/api/orgs/{org_id}/dashboards/manager", tags=["dashboards"])
def dashboard_manager(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    m = require_role(user, org_id, db, READ_ROLES)
    return finops.manager_dashboard(db, org_id, user, m)


@app.get("/api/orgs/{org_id}/dashboards/top-manager", tags=["dashboards"])
def dashboard_top(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    if _canon(user, org_id, db) not in ("top_manager", "owner"):
        raise HTTPException(403, "Top Manager dashboard requires top_manager or owner role")
    return finops.top_manager_dashboard(db, org_id)


@app.get("/api/orgs/{org_id}/dashboards/owner", tags=["dashboards"])
def dashboard_owner(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    if _canon(user, org_id, db) != "owner":
        raise HTTPException(403, "Owner dashboard requires owner role")
    return finops.owner_dashboard(db, org_id)


# ---- Accounts receivable / payable ----
class ARIn(BaseModel):
    number: str = ""
    customer_id: Optional[int] = None
    amount_cents: int = Field(ge=0)
    issued_date: str
    due_date: str
    department: str = ""


class APIn(BaseModel):
    number: str = ""
    supplier_id: Optional[int] = None
    amount_cents: int = Field(ge=0)
    issued_date: str
    due_date: str
    department: str = ""
    category: str = "General"


def _dept_scope(m: models.Membership) -> Optional[str]:
    """Managers are limited to their own department's documents."""
    return m.department if canonical_role(m.role) == "manager" and m.department else None


@app.get("/api/orgs/{org_id}/ar", tags=["finance-ops"])
def list_ar(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    m = require_role(user, org_id, db, READ_ROLES)
    q = db.query(models.ARInvoice).filter_by(org_id=org_id)
    dept = _dept_scope(m)
    if dept:
        q = q.filter(models.ARInvoice.department.in_([dept, ""]))
    invs = q.order_by(models.ARInvoice.due_date).limit(500).all()
    return {"aging": finops.ar_aging(db, org_id) if not dept else finops._aging(invs, date.today()),
            "invoices": [{"id": i.id, "number": i.number, "amount_cents": i.amount_cents,
                          "issued_date": i.issued_date.isoformat(),
                          "due_date": i.due_date.isoformat(), "status": i.status,
                          "department": i.department} for i in invs]}


@app.post("/api/orgs/{org_id}/ar", tags=["finance-ops"])
def create_ar(org_id: int, body: ARIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_perm(user, org_id, db, "manage_invoices")
    i = models.ARInvoice(org_id=org_id, number=body.number, customer_id=body.customer_id,
                         amount_cents=body.amount_cents,
                         issued_date=date.fromisoformat(body.issued_date),
                         due_date=date.fromisoformat(body.due_date), department=body.department)
    db.add(i)
    audit(db, org_id, user.id, "ar.create", "ar_invoices", None, {"number": body.number})
    db.commit()
    return {"id": i.id}


@app.get("/api/orgs/{org_id}/ap", tags=["finance-ops"])
def list_ap(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    m = require_role(user, org_id, db, READ_ROLES)
    q = db.query(models.APInvoice).filter_by(org_id=org_id)
    dept = _dept_scope(m)
    if dept:
        q = q.filter(models.APInvoice.department.in_([dept, ""]))
    invs = q.order_by(models.APInvoice.due_date).limit(500).all()
    return {"aging": finops.ap_aging(db, org_id) if not dept else finops._aging(invs, date.today()),
            "invoices": [{"id": i.id, "number": i.number, "amount_cents": i.amount_cents,
                          "issued_date": i.issued_date.isoformat(),
                          "due_date": i.due_date.isoformat(), "status": i.status,
                          "department": i.department, "category": i.category} for i in invs]}


@app.post("/api/orgs/{org_id}/ap", tags=["finance-ops"])
def create_ap(org_id: int, body: APIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_perm(user, org_id, db, "manage_invoices")
    i = models.APInvoice(org_id=org_id, number=body.number, supplier_id=body.supplier_id,
                         amount_cents=body.amount_cents,
                         issued_date=date.fromisoformat(body.issued_date),
                         due_date=date.fromisoformat(body.due_date),
                         department=body.department, category=body.category)
    db.add(i)
    audit(db, org_id, user.id, "ap.create", "ap_invoices", None, {"number": body.number})
    db.commit()
    return {"id": i.id}


# ---- Approval engine (thresholds, stages, self-approval prevention) ----
class ApprovalIn(BaseModel):
    kind: str = "expense"
    title: str = Field(min_length=2, max_length=300)
    description: str = ""
    amount_cents: int = Field(ge=0)
    department: str = ""


class ApprovalActIn(BaseModel):
    action: str                     # approve / reject / request_info
    note: str = ""


@app.get("/api/orgs/{org_id}/approvals", tags=["workflow"])
def list_approvals(org_id: int, status: Optional[str] = None,
                   user=Depends(get_current_user), db: Session = Depends(get_db)):
    m = require_role(user, org_id, db, READ_ROLES)
    q = db.query(models.ApprovalRequest).filter_by(org_id=org_id)
    if status:
        q = q.filter_by(status=status)
    if canonical_role(m.role) == "manager":
        q = q.filter((models.ApprovalRequest.requester_id == user.id) |
                     (models.ApprovalRequest.department == (m.department or "\x00")))
    rows = q.order_by(models.ApprovalRequest.created_at.desc()).limit(200).all()
    policy = finops.get_policy(db, org_id)
    return {"policy": {"top_manager_limit_cents": policy.top_manager_limit_cents},
            "requests": [finops._req_dict(r) for r in rows]}


@app.post("/api/orgs/{org_id}/approvals", tags=["workflow"])
def submit_approval(org_id: int, body: ApprovalIn,
                    user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_perm(user, org_id, db, "submit_requests")
    r = finops.submit_request(db, org_id, user, body.kind, body.title,
                              body.amount_cents, body.department, body.description)
    audit(db, org_id, user.id, "approval.submit", "approval_requests", r.id)
    db.commit()
    return {"id": r.id, "status": r.status}


@app.post("/api/orgs/{org_id}/approvals/{rid}/act", tags=["workflow"])
def act_approval(org_id: int, rid: int, body: ApprovalActIn,
                 user=Depends(get_current_user), db: Session = Depends(get_db)):
    m = require_role(user, org_id, db, READ_ROLES)
    r = db.query(models.ApprovalRequest).filter_by(id=rid, org_id=org_id).first()
    if not r:
        raise HTTPException(404, "Request not found")
    finops.act_on_request(db, org_id, user, m, r, body.action, body.note)
    audit(db, org_id, user.id, f"approval.{body.action}", "approval_requests", rid,
          {"amount_cents": r.amount_cents})
    db.commit()
    return {"id": r.id, "status": r.status}


@app.get("/api/orgs/{org_id}/approvals/{rid}/events", tags=["workflow"])
def approval_events(org_id: int, rid: int,
                    user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    rows = (db.query(models.ApprovalEvent).filter_by(request_id=rid)
            .order_by(models.ApprovalEvent.created_at).all())
    return [{"id": e.id, "actor_id": e.actor_id, "action": e.action,
             "note": e.note, "at": e.created_at.isoformat()} for e in rows]


class PolicyIn(BaseModel):
    top_manager_limit_cents: int = Field(ge=0)
    currency: str = "USD"


@app.get("/api/orgs/{org_id}/approval-policy", tags=["workflow"])
def get_approval_policy(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    p = finops.get_policy(db, org_id)
    db.commit()
    return {"top_manager_limit_cents": p.top_manager_limit_cents, "currency": p.currency}


@app.put("/api/orgs/{org_id}/approval-policy", tags=["workflow"])
def put_approval_policy(org_id: int, body: PolicyIn,
                        user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_perm(user, org_id, db, "manage_settings")   # Owner only by default matrix
    p = finops.get_policy(db, org_id)
    p.top_manager_limit_cents = body.top_manager_limit_cents
    p.currency = body.currency
    audit(db, org_id, user.id, "approval.policy_update", "approval_policies", p.id,
          {"limit": body.top_manager_limit_cents})
    db.commit()
    return {"top_manager_limit_cents": p.top_manager_limit_cents}


# ---- Saved scenarios (what-if engine outputs, comparable) ----
class ScenarioIn(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    period_start: str
    period_end: str
    params: dict = {}


@app.get("/api/orgs/{org_id}/scenarios", tags=["modeling"])
def list_scenarios(org_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    return [{"id": s.id, "name": s.name, "params": s.params,
             "period": [s.period_start.isoformat(), s.period_end.isoformat()],
             "created_by": s.created_by, "created_at": s.created_at.isoformat()}
            for s in db.query(models.Scenario).filter_by(org_id=org_id)
            .order_by(models.Scenario.created_at.desc()).limit(50).all()]


@app.post("/api/orgs/{org_id}/scenarios", tags=["modeling"])
def create_scenario(org_id: int, body: ScenarioIn,
                    user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_perm(user, org_id, db, "create_scenarios")
    s = finops.run_scenario(db, org_id, user, body.name,
                            date.fromisoformat(body.period_start),
                            date.fromisoformat(body.period_end), body.params or {})
    audit(db, org_id, user.id, "scenario.create", "scenarios", s.id, {"name": body.name})
    db.commit()
    return {"id": s.id, "results": s.results}


@app.get("/api/orgs/{org_id}/scenarios/compare", tags=["modeling"])
def compare_scenarios(org_id: int, ids: str = Query(...),
                      user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, READ_ROLES)
    id_list = [int(x) for x in ids.split(",") if x.strip()][:10]
    return finops.compare_scenarios(db, org_id, id_list)


# ---- Permission-scoped CSV reports ----
@app.get("/api/orgs/{org_id}/reports/{name}.csv", tags=["reports"])
def download_report(org_id: int, name: str, department: Optional[str] = None,
                    user=Depends(get_current_user), db: Session = Depends(get_db)):
    m = require_perm(user, org_id, db, "export_reports")
    filename, text = finops.report_csv(db, org_id, name, m, department)
    audit(db, org_id, user.id, "report.export", "reports", None, {"name": name})
    db.commit()
    return PlainTextResponse(text, media_type="text/csv",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


# ---- Organization-wide audit (Owner; append-only) ----
@app.get("/api/orgs/{org_id}/audit", tags=["admin"])
def org_audit(org_id: int, limit: int = Query(100, le=500),
              user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_perm(user, org_id, db, "view_audit_org")
    rows = (db.query(models.AuditLog).filter_by(org_id=org_id)
            .order_by(models.AuditLog.created_at.desc()).limit(limit).all())
    return [{"id": a.id, "user_id": a.user_id, "action": a.action, "entity": a.entity,
             "entity_id": a.entity_id, "meta": a.meta, "at": a.created_at.isoformat()}
            for a in rows]


# ---------------- Frontend (static SPA) ----------------
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
