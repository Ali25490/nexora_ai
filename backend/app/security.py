"""Auth, RBAC (three management levels), server-side permission matrix, audit.

PBKDF2 hashing (no native deps), JWT tokens.

Spec §3/§4: exactly three primary business roles — manager, top_manager, owner.
Legacy roles (cfo/analyst/accountant/viewer) are kept for backward compatibility
and mapped onto the three canonical levels. EVERY protected operation is checked
server-side via require_role() / require_perm() — never via frontend state.
"""
import hashlib, hmac, os, secrets
from datetime import datetime, timedelta
from typing import Optional
import jwt
from fastapi import Depends, HTTPException, Header
from sqlalchemy.orm import Session
from .db import get_db
from . import models

SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
ALGO = "HS256"
TOKEN_TTL_HOURS = 24

# ---- Three primary management levels (spec §3) ----
PRIMARY_ROLES = {"manager", "top_manager", "owner"}
# Legacy roles preserved so existing accounts/tests keep working; mapped to canonical levels.
LEGACY_ROLE_MAP = {"cfo": "top_manager", "analyst": "top_manager", "accountant": "manager"}
ALL_ROLES = PRIMARY_ROLES | set(LEGACY_ROLE_MAP) | {"viewer"}

ROLE_RANK = {"viewer": 1, "accountant": 2, "manager": 3, "analyst": 4,
             "cfo": 5, "top_manager": 5, "owner": 6}
WRITE_ROLES = {"owner", "cfo", "analyst", "top_manager"}
ADMIN_ROLES = {"owner", "cfo"}


def canonical_role(role: str) -> str:
    """Map any stored role to one of the three canonical levels (viewer = restricted read)."""
    if role in PRIMARY_ROLES:
        return role
    return LEGACY_ROLE_MAP.get(role, "viewer")


# ---- Server-side permission matrix (spec §4). Never trust role names alone on the client. ----
PERMISSIONS = {
    "view_assigned_department": {"manager", "top_manager", "owner"},
    "view_all_departments":     {"top_manager", "owner"},
    "view_consolidated_finances": {"top_manager", "owner"},
    "submit_requests":          {"manager", "top_manager", "owner"},
    "approve_within_limit":     {"top_manager", "owner"},
    "approve_unlimited":        {"owner"},
    "approve_department_budget": {"top_manager", "owner"},
    "approve_company_budget":   {"owner"},
    "run_forecasts":            {"top_manager", "owner"},
    "create_scenarios":         {"top_manager", "owner"},
    "manage_roles":             {"owner"},
    "manage_settings":          {"owner"},
    "manage_subscription":      {"owner"},
    "view_audit_org":           {"owner"},
    "export_reports":           {"manager", "top_manager", "owner"},
    "manage_invoices":          {"manager", "top_manager", "owner"},
    "view_department_pnl":      {"manager", "top_manager", "owner"},
}


def has_permission(role: str, capability: str) -> bool:
    return canonical_role(role) in PERMISSIONS.get(capability, set())


def hash_password(password: str, salt: Optional[str] = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 120_000)
    return f"{salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, _ = stored.split("$", 1)
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(password, salt), stored)


def create_token(user_id: int) -> str:
    payload = {"sub": str(user_id), "exp": datetime.utcnow() + timedelta(hours=TOKEN_TTL_HOURS)}
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGO)


def decode_token(token: str) -> int:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGO])
        return int(payload["sub"])
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")


def get_current_user(authorization: str = Header(default=""), db: Session = Depends(get_db)) -> models.User:
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token")
    user_id = decode_token(authorization[7:])
    user = db.get(models.User, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="User not found or disabled")
    return user


def get_membership(user: models.User, org_id: int, db: Session) -> models.Membership:
    m = (db.query(models.Membership)
         .filter_by(user_id=user.id, org_id=org_id).first())
    if not m:
        raise HTTPException(status_code=403, detail="No access to this organization")
    return m


def require_role(user: models.User, org_id: int, db: Session, allowed: set) -> models.Membership:
    if user.is_superadmin:
        m = db.query(models.Membership).filter_by(user_id=user.id, org_id=org_id).first()
        return m or models.Membership(org_id=org_id, user_id=user.id, role="owner")
    m = get_membership(user, org_id, db)
    if m.role not in allowed:
        raise HTTPException(status_code=403, detail=f"Role '{m.role}' not permitted for this action")
    return m


def require_perm(user: models.User, org_id: int, db: Session, capability: str) -> models.Membership:
    """Capability check against the server-side permission matrix (spec §4)."""
    m = require_role(user, org_id, db, ALL_ROLES)
    if not has_permission(m.role, capability):
        raise HTTPException(status_code=403,
                            detail=f"Role '{m.role}' lacks permission '{capability}'")
    return m


def audit(db: Session, org_id: Optional[int], user_id: Optional[int], action: str,
          entity: str = "", entity_id: Optional[int] = None, meta: dict = None):
    """Append-only audit trail. AuditLog has NO update/delete endpoints anywhere in the app."""
    db.add(models.AuditLog(org_id=org_id, user_id=user_id, action=action,
                           entity=entity, entity_id=entity_id, meta=meta or {}))
