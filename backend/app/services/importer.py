"""CSV/XLSX import with validation, formula-injection guard, dedup and error reporting."""
import csv, io, re
from datetime import datetime
from sqlalchemy.orm import Session
from .. import models

FORMULA_PREFIX = re.compile(r"^[=+\-@]")
ALLOWED_KINDS = {"orders", "expenses", "customers"}


def _clean_cell(v: str) -> str:
    v = (v or "").strip()
    if FORMULA_PREFIX.match(v):  # spreadsheet formula injection guard
        v = "'" + v
    return v


def _parse_date(s):
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    raise ValueError(f"unparseable date: {s!r}")


def _parse_money_to_cents(s):
    s = str(s).replace(" ", "").replace(",", ".").replace("$", "").strip()
    return int(round(float(s) * 100))


def import_csv(db: Session, org_id: int, filename: str, content: bytes,
               kind: str = "orders") -> models.ImportBatch:
    if kind not in ALLOWED_KINDS:
        raise ValueError(f"kind must be one of {sorted(ALLOWED_KINDS)}")
    if len(content) > 20 * 1024 * 1024:
        raise ValueError("file too large (max 20MB)")
    text = content.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    batch = models.ImportBatch(org_id=org_id, filename=filename, status="done")
    ok, failed, errors = 0, 0, []
    seen = set()
    for i, row in enumerate(reader, start=2):
        try:
            if kind == "orders":
                d = _parse_date(row.get("date") or row.get("created_date") or "")
                amt = _parse_money_to_cents(row.get("amount") or row.get("total") or "0")
                channel = _clean_cell(row.get("channel") or "Direct")
                cust_name = _clean_cell(row.get("customer") or row.get("customer_name") or f"Customer {i}")
                key = (d.isoformat(), amt, channel, cust_name)
                if key in seen:
                    raise ValueError("duplicate row")
                seen.add(key)
                cust = (db.query(models.Customer)
                        .filter_by(org_id=org_id, name=cust_name).first())
                if not cust:
                    cust = models.Customer(org_id=org_id, name=cust_name, channel=channel)
                    db.add(cust)
                    db.flush()
                db.add(models.Order(org_id=org_id, customer_id=cust.id, channel=channel,
                                    total_cents=amt, created_date=d,
                                    status="completed" if amt >= 0 else "refunded"))
            elif kind == "expenses":
                d = _parse_date(row.get("date") or "")
                amt = _parse_money_to_cents(row.get("amount") or "0")
                db.add(models.Transaction(
                    org_id=org_id, kind="expense",
                    category=_clean_cell(row.get("category") or "General"),
                    department=_clean_cell(row.get("department") or "General"),
                    amount_cents=abs(amt), date=d))
            else:  # customers
                name = _clean_cell(row.get("name") or "")
                if not name:
                    raise ValueError("missing name")
                db.add(models.Customer(
                    org_id=org_id, name=name,
                    email=_clean_cell(row.get("email") or ""),
                    segment=_clean_cell(row.get("segment") or "new"),
                    channel=_clean_cell(row.get("channel") or "Direct")))
            ok += 1
        except Exception as e:  # collect row-level errors, don't abort file
            failed += 1
            errors.append({"row": i, "error": str(e)[:200]})
            if len(errors) >= 50:
                errors.append({"row": -1, "error": "too many errors, truncated"})
                break
    batch.rows_total = ok + failed
    batch.rows_ok = ok
    batch.rows_failed = failed
    batch.errors = errors
    if failed and ok:
        batch.status = "partial"
    elif failed and not ok:
        batch.status = "failed"
    db.add(batch)
    db.commit()
    return batch


def data_quality_report(db: Session, org_id: int):
    issues = []
    total_orders = db.query(models.Order).filter_by(org_id=org_id).count()
    neg = db.query(models.Order).filter_by(org_id=org_id).filter(models.Order.total_cents < 0).count()
    if neg:
        issues.append({"type": "negative_amounts", "entity": "orders", "count": neg,
                       "priority": "medium", "recommendation": "verify refund handling methodology"})
    no_cust = db.query(models.Order).filter_by(org_id=org_id, customer_id=None).count()
    if no_cust:
        issues.append({"type": "missing_reference", "entity": "orders", "count": no_cust,
                       "priority": "high", "recommendation": "link orders to customers"})
    no_email = db.query(models.Customer).filter_by(org_id=org_id, email="").count()
    if no_email:
        issues.append({"type": "missing_values", "entity": "customers", "count": no_email,
                       "priority": "low", "recommendation": "enrich customer emails"})
    batches = db.query(models.ImportBatch).filter_by(org_id=org_id)\
        .order_by(models.ImportBatch.created_at.desc()).limit(10).all()
    return {"orders_total": total_orders,
            "completeness_pct": round(100 * (1 - (no_cust + no_email) / max(total_orders + 1, 1)), 1),
            "issues": issues,
            "recent_imports": [{"id": b.id, "filename": b.filename, "status": b.status,
                                "rows_ok": b.rows_ok, "rows_failed": b.rows_failed} for b in batches]}
