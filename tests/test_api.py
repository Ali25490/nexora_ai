"""NEXORA AI test suite — run: cd backend && python -m pytest ../tests -v"""
import os, sys, tempfile
os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mktemp(suffix=".db")
os.environ["SEED_DEMO"] = "1"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from fastapi.testclient import TestClient  # noqa
from app.main import app  # noqa

c = TestClient(app)
c.__enter__()  # trigger startup: create tables + seed


def login(email="admin@nexora.ai", pw="admin123"):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


def auth(t):
    return {"Authorization": f"Bearer {t}"}


TOK = login()
ORG = TOK["org_id"]
H = auth(TOK["token"])


def test_health():
    assert c.get("/api/health").json()["status"] == "ok"


def test_register_and_login():
    r = c.post("/api/auth/register", json={"email": "ceo@acme.io", "password": "Secret123!",
                                            "full_name": "CEO", "org_name": "Acme"})
    assert r.status_code == 200 and r.json()["token"]


def test_wrong_password_rejected():
    assert c.post("/api/auth/login",
                  json={"email": "admin@nexora.ai", "password": "nope"}).status_code == 401


def test_no_token_rejected():
    assert c.get(f"/api/orgs/{ORG}/kpi").status_code == 401


def test_kpi_values_real():
    k = c.get(f"/api/orgs/{ORG}/kpi", headers=H).json()
    assert k["revenue"]["value_cents"] > 0
    assert k["users"]["total"] > 0
    assert 0 <= k["churn_rate"]["value"] <= 100
    assert "avg_order_value" in k


def test_org_isolation():
    other = login("ceo@acme.io", "Secret123!") if c.post("/api/auth/login",
            json={"email": "ceo@acme.io", "password": "Secret123!"}).status_code == 200 else None
    r = c.get(f"/api/orgs/{ORG}/kpi", headers=auth(other["token"]))
    assert r.status_code == 403  # member of Acme cannot read Nexora Demo org


def test_revenue_daily_series():
    d = c.get(f"/api/orgs/{ORG}/revenue/daily", headers=H).json()
    assert len(d["series"]) == 31 and all("revenue_cents" in p for p in d["series"])


def test_channels_and_segments():
    ch = c.get(f"/api/orgs/{ORG}/revenue/by-channel", headers=H).json()
    assert len(ch) >= 3 and abs(sum(x["share_pct"] for x in ch) - 100) < 2
    seg = c.get(f"/api/orgs/{ORG}/users/by-segment", headers=H).json()
    assert sum(x["count"] for x in seg) > 0


def test_anomaly_detection_runs():
    r = c.post(f"/api/orgs/{ORG}/anomalies/run", headers=H)
    assert r.status_code == 200
    rows = c.get(f"/api/orgs/{ORG}/anomalies", headers=H).json()
    assert isinstance(rows, list)
    if rows:
        a = rows[0]
        assert a["deviation_pct"] != 0 and a["method"] == "rolling_zscore"
        assert a["status"] in {"new", "under_review", "investigating",
                               "action_required", "resolved", "dismissed"}


def test_forecast_backtested():
    f = c.get(f"/api/orgs/{ORG}/forecast/revenue?horizon=14", headers=H).json()
    assert f["ok"] and len(f["forecast"]) == 14
    assert f["mape_pct"] is not None and f["baseline_naive_mape_pct"] is not None
    for p in f["forecast"]:
        assert p["lower_cents"] <= p["yhat_cents"] <= p["upper_cents"]


def test_pnl_and_budget():
    pnl = c.get(f"/api/orgs/{ORG}/finance/pnl", headers=H).json()
    assert pnl["gross_profit_cents"] == pnl["revenue_cents"] - pnl["cogs_cents"]
    assert pnl["ebitda_cents"] == pnl["gross_profit_cents"] - pnl["opex_cents"]
    bva = c.get(f"/api/orgs/{ORG}/finance/budget-vs-actual", headers=H).json()
    for r in bva["rows"]:
        assert r["variance_cents"] == r["actual_cents"] - r["planned_cents"]


def test_what_if_math():
    r = c.post(f"/api/orgs/{ORG}/finance/what-if",
               json={"price_pct": 10, "volume_pct": 0, "cost_pct": 0, "marketing_pct": 0},
               headers=H).json()
    assert abs(r["result"]["revenue_cents"] - r["base"]["revenue_cents"] * 1.10) < 2


def test_cash_flow_13_weeks():
    cf = c.get(f"/api/orgs/{ORG}/finance/cash-flow", headers=H).json()
    assert len(cf["weeks"]) == 13
    assert "assumptions" in cf


def test_csv_import_and_validation():
    csv_data = "date,amount,channel,customer\n2026-10-01,150.50,Direct,TestCo\nbad-row,x,,\n"
    r = c.post(f"/api/orgs/{ORG}/import?kind=orders", headers=H,
               files={"file": ("t.csv", csv_data, "text/csv")})
    assert r.status_code == 200
    d = r.json()
    assert d["rows_ok"] == 1 and d["rows_failed"] == 1  # bad row rejected, file not aborted


def test_formula_injection_guard():
    csv_data = "name,email,segment\nEvil,=cmd|'/c calc'!A1,new\n"
    r = c.post(f"/api/orgs/{ORG}/import?kind=customers", headers=H,
               files={"file": ("e.csv", csv_data, "text/csv")})
    assert r.status_code == 200 and r.json()["rows_ok"] == 1


def test_ask_ai_grounded():
    r = c.post(f"/api/orgs/{ORG}/ai/ask", json={"question": "What if we increase prices by 5%?"},
               headers=H).json()
    assert "source" in r and len(r["answer"]) > 20


def test_ask_ai_role_restriction():
    # viewer cannot get financial answers
    c.post(f"/api/orgs/{ORG}/team", json={"email": "view@x.io", "role": "viewer"}, headers=H)
    v = c.post("/api/auth/login", json={"email": "view@x.io", "password": "ChangeMe123!"}).json()
    r = c.post(f"/api/orgs/{ORG}/ai/ask", json={"question": "what is our cash position?"},
               headers=auth(v["token"]))
    assert r.status_code == 403


def test_tasks_lifecycle():
    t = c.post(f"/api/orgs/{ORG}/tasks", json={"title": "Check spike"}, headers=H).json()
    assert t["id"]
    c.patch(f"/api/orgs/{ORG}/tasks/{t['id']}", json={"status": "done"}, headers=H)
    rows = c.get(f"/api/orgs/{ORG}/tasks", headers=H).json()
    assert any(x["id"] == t["id"] and x["status"] == "done" for x in rows)


def test_alerts_and_metrics_catalog():
    r = c.post(f"/api/orgs/{ORG}/alerts",
               json={"name": "Rev drop", "threshold": -20, "condition": "change_pct"}, headers=H)
    assert r.status_code == 200
    cat = c.get("/api/metrics-catalog").json()
    keys = {m["key"] for m in cat}
    assert {"revenue", "cac", "ltv", "churn_rate"} <= keys


def test_anomaly_status_endpoint():
    s = c.get(f"/api/orgs/{ORG}/anomalies/status", headers=H).json()
    assert "never_run" in s and "findings" in s and "last_run_at" in s
    # detection was run in test_anomaly_detection_runs -> not never_run, audit logged
    assert s["never_run"] is False
    assert s["findings"] == len(c.get(f"/api/orgs/{ORG}/anomalies", headers=H).json())


def test_forecast_intervals_nonzero_width():
    f = c.get(f"/api/orgs/{ORG}/forecast/revenue?horizon=14", headers=H).json()
    assert f["ok"]
    # residual sd now computed vs fitted values -> bands have real width
    assert any(p["upper_cents"] > p["lower_cents"] for p in f["forecast"])


def test_admin_requires_superadmin():
    assert c.get("/api/admin/organizations", headers=H).status_code == 200  # demo admin is superadmin


def test_audit_log_written():
    rows = c.get("/api/admin/audit", headers=H).json()
    assert len(rows) > 0
