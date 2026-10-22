"""Demo seed for the three-role platform: users, departments, budgets,
AR/AP invoices across aging buckets, approval policy + sample requests.
Idempotent — skipped entirely if manager@nexora.ai already exists."""
from datetime import date, datetime, timedelta
from sqlalchemy.orm import Session
from .. import models
from ..security import hash_password

DEPTS = [("Sales", 40_000_00), ("Marketing", 25_000_00), ("Operations", 35_000_00),
         ("Engineering", 60_000_00), ("G&A", 15_000_00)]


def seed_three_role_demo(db: Session):
    if db.query(models.User).filter_by(email="manager@nexora.ai").first():
        return
    admin = db.query(models.User).filter_by(email="admin@nexora.ai").first()
    if admin:
        mship = db.query(models.Membership).filter_by(user_id=admin.id).first()
        org_id = mship.org_id
    else:
        org = models.Organization(name="Nexora Demo")
        db.add(org); db.flush()
        org_id = org.id

    # --- departments ---
    dept_ids = {}
    for name, budget in DEPTS:
        d = db.query(models.Department).filter_by(org_id=org_id, name=name).first()
        if not d:
            d = models.Department(org_id=org_id, name=name, budget_monthly_cents=budget)
            db.add(d); db.flush()
        dept_ids[name] = d.id

    # --- three canonical role users ---
    def _user(email, pwd, name, role, dept=""):
        u = models.User(email=email, password_hash=hash_password(pwd), full_name=name)
        db.add(u); db.flush()
        db.add(models.Membership(org_id=org_id, user_id=u.id, role=role, department=dept))
        return u

    manager = _user("manager@nexora.ai", "manager123", "Demo Manager", "manager", "Sales")
    execu = _user("exec@nexora.ai", "exec123", "Demo Top Manager", "top_manager")
    owner = db.query(models.User).filter_by(email="owner@nexora.ai").first() or \
        _user("owner@nexora.ai", "owner123", "Demo Owner", "owner")

    # --- budgets: current + next month per department ---
    today = date.today()
    periods = [f"{today.year}-{today.month:02d}"]
    nm = date(today.year + (1 if today.month == 12 else 0),
              1 if today.month == 12 else today.month + 1, 1)
    periods.append(f"{nm.year}-{nm.month:02d}")
    for per in periods:
        for name, budget in DEPTS:
            if not db.query(models.Budget).filter_by(org_id=org_id, period=per,
                                                     department=name).first():
                db.add(models.Budget(org_id=org_id, department=name, period=per,
                                     planned_cents=budget, status="approved"))

    # --- departmental revenue ledger (orders are channel-scoped, not dept-scoped) ---
    import random
    rnd = random.Random(42)
    if not db.query(models.Transaction).filter_by(org_id=org_id, kind="revenue").first():
        for back in range(60):
            day = today - timedelta(days=back)
            for dept, base in (("Sales", 18_000), ("Marketing", 3_000),
                               ("Operations", 4_000), ("Engineering", 1_000)):
                amt = int(base * (0.8 + rnd.random() * 0.5)) * 100
                db.add(models.Transaction(org_id=org_id, kind="revenue",
                                          category="Sales", department=dept,
                                          amount_cents=amt, date=day))

    # --- suppliers ---
    suppliers = {}
    for sname in ("AWS", "WeWork", "Acme Ads", "Delta Logistics"):
        s = db.query(models.Supplier).filter_by(org_id=org_id, name=sname).first()
        if not s:
            s = models.Supplier(org_id=org_id, name=sname)
            db.add(s); db.flush()
        suppliers[sname] = s.id

    # --- AR invoices across aging buckets (tie to existing customers if present) ---
    customers = db.query(models.Customer).filter_by(org_id=org_id).limit(6).all()
    ar_spec = [(-10, 15, "open"), (-40, -10, "open"), (-70, -5, "open"),
               (-100, -30, "open"), (-130, -40, "open"), (-5, 20, "open"),
               (-60, -30, "paid")]
    for i, (issued_off, due_off, status) in enumerate(ar_spec, 1):
        num = f"AR-{1000+i}"
        if db.query(models.ARInvoice).filter_by(org_id=org_id, number=num).first():
            continue
        db.add(models.ARInvoice(
            org_id=org_id, number=num,
            customer_id=customers[i % len(customers)].id if customers else None,
            amount_cents=(5_000 + i * 1_750) * 100,
            issued_date=today + timedelta(days=issued_off),
            due_date=today + timedelta(days=due_off),
            status=status,
            paid_date=(today + timedelta(days=due_off - 2)) if status == "paid" else None,
            department="Sales"))

    # --- AP invoices ---
    ap_spec = [("AWS", -12, 10, "approved", "Engineering", "Infrastructure"),
               ("WeWork", -20, 5, "pending_approval", "G&A", "Rent"),
               ("Acme Ads", -45, -15, "approved", "Marketing", "Marketing"),
               ("Delta Logistics", -80, -50, "approved", "Operations", "Suppliers"),
               ("AWS", -75, -45, "paid", "Engineering", "Infrastructure")]
    for i, (sup, issued_off, due_off, status, dept, cat) in enumerate(ap_spec, 1):
        num = f"AP-{2000+i}"
        if db.query(models.APInvoice).filter_by(org_id=org_id, number=num).first():
            continue
        db.add(models.APInvoice(
            org_id=org_id, number=num, supplier_id=suppliers[sup],
            amount_cents=(3_000 + i * 2_250) * 100,
            issued_date=today + timedelta(days=issued_off),
            due_date=today + timedelta(days=due_off), status=status,
            paid_date=(today + timedelta(days=due_off - 1)) if status == "paid" else None,
            department=dept, category=cat))

    # --- approval policy + sample requests with immutable events ---
    if not db.query(models.ApprovalPolicy).filter_by(org_id=org_id).first():
        db.add(models.ApprovalPolicy(org_id=org_id, top_manager_limit_cents=10_000_00))
    samples = [
        ("expense", "New CRM seats for Sales", 4_800_00, "Sales", manager.id, "pending"),
        ("payment", "Q4 agency retainer", 26_000_00, "Marketing", manager.id, "pending"),
        ("budget", "Engineering budget revision +8%", 48_000_00, "Engineering", execu.id, "pending"),
    ]
    for kind, title, amt, dept, req_id, status in samples:
        if db.query(models.ApprovalRequest).filter_by(org_id=org_id, title=title).first():
            continue
        r = models.ApprovalRequest(org_id=org_id, kind=kind, title=title, amount_cents=amt,
                                   department=dept, requester_id=req_id, status=status)
        db.add(r); db.flush()
        db.add(models.ApprovalEvent(request_id=r.id, actor_id=req_id, action="submit",
                                    note=f"amount_cents={amt}"))
    db.commit()
