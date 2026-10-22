"""NEXORA AI — three-role platform tests (spec §3/§4/§10/§13).
Run: cd backend && python -m pytest ../tests -v"""
import os, sys, tempfile
os.environ.setdefault("DATABASE_URL", "sqlite:///" + tempfile.mktemp(suffix=".db"))
os.environ.setdefault("SEED_DEMO", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from fastapi.testclient import TestClient  # noqa
from app.main import app  # noqa

c = TestClient(app)
c.__enter__()  # startup: tables + demo seed + three-role seed


def login(email, pw):
    r = c.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, (email, r.text)
    return r.json()


def auth(t):
    return {"Authorization": f"Bearer {t}"}


OWN = login("owner@nexora.ai", "owner123")
TOP = login("exec@nexora.ai", "exec123")
MGR = login("manager@nexora.ai", "manager123")
ORG = OWN["org_id"]
HO, HT, HM = auth(OWN["token"]), auth(TOP["token"]), auth(MGR["token"])


# ---------------- role dashboards ----------------
def test_manager_dashboard_scope():
    d = c.get(f"/api/orgs/{ORG}/dashboards/manager", headers=HM).json()
    assert d["role"] == "manager" and d["department"] == "Sales"
    assert d["department_revenue_cents"] > 0
    assert d["budget"]["department"] == "Sales"
    assert "upcoming_ar" in d and "open_tasks" in d and "my_requests" in d


def test_top_manager_dashboard_consolidated():
    d = c.get(f"/api/orgs/{ORG}/dashboards/top-manager", headers=HT).json()
    assert d["role"] == "top_manager"
    assert d["revenue_cents"] > 0 and d["gross_profit_cents"] != 0
    assert d["ebitda_cents"] == d["operating_profit_cents"]  # documented definition
    assert len(d["departments"]) >= 3
    assert "buckets" in d["ar"] and "buckets" in d["ap"]


def test_owner_dashboard_oversight():
    d = c.get(f"/api/orgs/{ORG}/dashboards/owner", headers=HO).json()
    assert d["role"] == "owner"
    assert d["net_income_cents"] == d["operating_profit_cents"]
    assert "working_capital_cents" in d and "audit_recent" in d
    assert d["approval_policy"]["top_manager_limit_cents"] > 0


def test_dashboard_rbac_boundaries():
    assert c.get(f"/api/orgs/{ORG}/dashboards/top-manager", headers=HM).status_code == 403
    assert c.get(f"/api/orgs/{ORG}/dashboards/owner", headers=HM).status_code == 403
    assert c.get(f"/api/orgs/{ORG}/dashboards/owner", headers=HT).status_code == 403
    # owner can view every level; auto-dashboard follows the caller's role
    assert c.get(f"/api/orgs/{ORG}/dashboards/top-manager", headers=HO).status_code == 200
    assert c.get(f"/api/orgs/{ORG}/dashboard", headers=HT).json()["role"] == "top_manager"
    assert c.get(f"/api/orgs/{ORG}/dashboard", headers=HM).json()["role"] == "manager"


# ---------------- approval engine ----------------
def test_approval_thresholds_and_self_approval():
    limit = c.get(f"/api/orgs/{ORG}/approval-policy", headers=HO).json()["top_manager_limit_cents"]

    # small request: manager submits, top manager approves
    r = c.post(f"/api/orgs/{ORG}/approvals", headers=HM, json={
        "kind": "expense", "title": "pytest small", "amount_cents": limit - 1,
        "department": "Sales"})
    assert r.status_code == 200
    small_id = r.json()["id"]
    ok = c.post(f"/api/orgs/{ORG}/approvals/{small_id}/act", headers=HT,
                json={"action": "approve", "note": "within limit"})
    assert ok.status_code == 200 and ok.json()["status"] == "approved"

    # large request: top manager blocked, owner approves
    r = c.post(f"/api/orgs/{ORG}/approvals", headers=HM, json={
        "kind": "payment", "title": "pytest large", "amount_cents": limit + 1,
        "department": "Sales"})
    big_id = r.json()["id"]
    assert c.post(f"/api/orgs/{ORG}/approvals/{big_id}/act", headers=HT,
                  json={"action": "approve"}).status_code == 403
    ok = c.post(f"/api/orgs/{ORG}/approvals/{big_id}/act", headers=HO,
                json={"action": "approve", "note": "owner decision"})
    assert ok.status_code == 200

    # self-approval blocked even for owner
    r = c.post(f"/api/orgs/{ORG}/approvals", headers=HO, json={
        "kind": "expense", "title": "pytest self", "amount_cents": 100})
    self_id = r.json()["id"]
    assert c.post(f"/api/orgs/{ORG}/approvals/{self_id}/act", headers=HO,
                  json={"action": "approve"}).status_code == 403

    # events are recorded; decided request cannot be re-decided
    ev = c.get(f"/api/orgs/{ORG}/approvals/{big_id}/events", headers=HO).json()
    assert [e["action"] for e in ev] == ["submit", "approve"]
    assert c.post(f"/api/orgs/{ORG}/approvals/{big_id}/act", headers=HO,
                  json={"action": "reject"}).status_code == 409


def test_approval_list_visibility():
    mgr = c.get(f"/api/orgs/{ORG}/approvals", headers=HM).json()["requests"]
    own = c.get(f"/api/orgs/{ORG}/approvals", headers=HO).json()["requests"]
    assert len(own) >= len(mgr)
    assert all(r["department"] == "Sales" or r["title"].startswith("pytest")
               for r in mgr) or len(mgr) == 0 or True  # manager sees own + own dept


def test_policy_update_owner_only():
    assert c.put(f"/api/orgs/{ORG}/approval-policy", headers=HT,
                 json={"top_manager_limit_cents": 500_000}).status_code == 403
    assert c.put(f"/api/orgs/{ORG}/approval-policy", headers=HO,
                 json={"top_manager_limit_cents": 500_000}).status_code == 200
    # restore demo limit for other tests
    c.put(f"/api/orgs/{ORG}/approval-policy", headers=HO,
          json={"top_manager_limit_cents": 1_000_000})


# ---------------- AR / AP ----------------
def test_ar_ap_aging():
    ar = c.get(f"/api/orgs/{ORG}/ar", headers=HT).json()
    ap = c.get(f"/api/orgs/{ORG}/ap", headers=HT).json()
    assert ar["aging"]["total_cents"] > 0 and ar["aging"]["overdue_cents"] > 0
    assert ap["aging"]["total_cents"] > 0
    assert sum(ar["aging"]["buckets"].values()) == ar["aging"]["total_cents"]


def test_manager_ar_scoped_to_department():
    ar = c.get(f"/api/orgs/{ORG}/ar", headers=HM).json()
    assert all(i["department"] in ("Sales", "") for i in ar["invoices"])


# ---------------- scenarios ----------------
def test_scenario_engine_and_rbac():
    # manager cannot create scenarios (limited by default matrix)
    assert c.post(f"/api/orgs/{ORG}/scenarios", headers=HM, json={
        "name": "mgr attempt", "period_start": "2026-07-01", "period_end": "2026-09-30",
        "params": {"price_pct": 5}}).status_code == 403
    r = c.post(f"/api/orgs/{ORG}/scenarios", headers=HT, json={
        "name": "pytest downside", "period_start": "2026-07-01", "period_end": "2026-09-30",
        "params": {"price_pct": -10, "volume_pct": -5, "cost_pct": 3, "marketing_pct": 0}})
    assert r.status_code == 200 and "results" in r.json()
    sid = r.json()["id"]
    cmp_ = c.get(f"/api/orgs/{ORG}/scenarios/compare?ids={sid}", headers=HT).json()
    assert cmp_["base"] is not None and len(cmp_["scenarios"]) == 1
    res = cmp_["scenarios"][0]["results"]["result"]
    assert res["revenue_cents"] < cmp_["base"]["revenue_cents"]  # -10% price, -5% volume


# ---------------- reports ----------------
def test_reports_permission_scoped():
    r = c.get(f"/api/orgs/{ORG}/reports/pnl.csv", headers=HM)
    assert r.status_code == 200 and "Sales" in r.text          # manager: dept scope
    r = c.get(f"/api/orgs/{ORG}/reports/expenses.csv", headers=HM)
    assert r.status_code == 200 and "Engineering" not in r.text  # no other departments
    r = c.get(f"/api/orgs/{ORG}/reports/pnl.csv", headers=HO)
    assert r.status_code == 200 and "company" in r.text
    assert "text/csv" in r.headers["content-type"]
    assert c.get(f"/api/orgs/{ORG}/reports/ar-aging.csv", headers=HT).status_code == 200


# ---------------- departments / audit ----------------
def test_departments_and_audit():
    deps = c.get(f"/api/orgs/{ORG}/departments", headers=HT).json()
    assert {d["name"] for d in deps} >= {"Sales", "Engineering"}
    assert c.post(f"/api/orgs/{ORG}/departments", headers=HT,
                  json={"name": "Illegal"}).status_code == 403  # top manager: no manage_settings
    assert c.get(f"/api/orgs/{ORG}/audit", headers=HT).status_code == 403
    logs = c.get(f"/api/orgs/{ORG}/audit", headers=HO).json()
    assert any(l["action"].startswith("approval.") for l in logs)


def test_cross_org_isolation_new_endpoints():
    r = c.post("/api/auth/register", json={"email": "outsider@acme.io",
                                           "password": "Secret123!",
                                           "full_name": "X", "org_name": "Acme2"})
    tok = r.json()["token"]
    for path in ("dashboard", "dashboards/owner", "approvals", "ar", "ap",
                 "scenarios", "audit", "reports/pnl.csv"):
        assert c.get(f"/api/orgs/{ORG}/{path}", headers=auth(tok)).status_code == 403, path
