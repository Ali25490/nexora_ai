"""NEXORA AI — FastAPI application. All endpoints org-scoped + RBAC-checked."""
import os
from datetime import date, timedelta, datetime
from typing import Optional
from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session
from .db import Base, engine, get_db
from . import models
from .security import (hash_password, verify_password, create_token, get_current_user,
                       require_role, get_membership, audit, WRITE_ROLES, ADMIN_ROLES)
from .services import metrics as msvc, anomaly as asvc, forecast as fsvc
from .services import finance as finsvc, importer as isvc, ai as aisvc, seed as seedsvc

app = FastAPI(title="NEXORA AI API", version="1.0.0",
              description="Intelligent business analytics platform. All money values in cents.")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], allow_credentials=True)

ALL_ROLES = {"owner", "cfo", "analyst", "accountant", "manager", "viewer"}
READ_ROLES = ALL_ROLES


@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    seedsvc.ensure_plans(db)
    if os.getenv("SEED_DEMO", "1") == "1":
        seedsvc.create_demo_account(db)
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
    require_role(user, org_id, db, {"owner", "cfo", "analyst", "manager"})
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
    require_role(user, org_id, db, {"owner", "cfo", "accountant", "analyst"})
    s, e = _range(start, end)
    return finsvc.pnl(db, org_id, s, e)


@app.get("/api/orgs/{org_id}/finance/budget-vs-actual", tags=["finance"])
def bva(org_id: int, period: Optional[str] = None,
        user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, {"owner", "cfo", "accountant", "manager"})
    period = period or f"{date.today().year}-{date.today().month:02d}"
    return finsvc.budget_vs_actual(db, org_id, period)


@app.get("/api/orgs/{org_id}/finance/cash-flow", tags=["finance"])
def cashflow(org_id: int, opening_cash_cents: int = 500_000_00,
             user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, {"owner", "cfo", "accountant"})
    return finsvc.cash_flow_forecast(db, org_id, opening_cash_cents=opening_cash_cents)


class WhatIfIn(BaseModel):
    price_pct: float = 0
    volume_pct: float = 0
    cost_pct: float = 0
    marketing_pct: float = 0


@app.post("/api/orgs/{org_id}/finance/what-if", tags=["finance"])
def what_if(org_id: int, body: WhatIfIn, start: Optional[str] = None, end: Optional[str] = None,
            user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, {"owner", "cfo", "analyst"})
    s, e = _range(start, end)
    return finsvc.what_if(db, org_id, s, e, body.price_pct, body.volume_pct,
                          body.cost_pct, body.marketing_pct)


@app.get("/api/orgs/{org_id}/finance/unit-economics", tags=["finance"])
def unit_econ(org_id: int, start: Optional[str] = None, end: Optional[str] = None,
              user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, {"owner", "cfo", "analyst"})
    s, e = _range(start, end)
    return finsvc.unit_economics(db, org_id, s, e)


# ---------------- Import / Data Quality ----------------
@app.post("/api/orgs/{org_id}/import", tags=["data"])
async def import_file(org_id: int, kind: str = "orders", file: UploadFile = File(...),
                      user=Depends(get_current_user), db: Session = Depends(get_db)):
    require_role(user, org_id, db, {"owner", "cfo", "accountant", "analyst"})
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


# ---------------- Frontend (static SPA) ----------------
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
if os.path.isdir(FRONTEND_DIR):
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))
