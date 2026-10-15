"""Auth, RBAC, audit. PBKDF2 hashing (no native deps), JWT tokens."""
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

ROLE_RANK = {"viewer": 1, "accountant": 2, "manager": 3, "analyst": 4, "cfo": 5, "owner": 6}
WRITE_ROLES = {"owner", "cfo", "analyst"}
ADMIN_ROLES = {"owner", "cfo"}


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


def audit(db: Session, org_id: Optional[int], user_id: Optional[int], action: str,
          entity: str = "", entity_id: Optional[int] = None, meta: dict = None):
    db.add(models.AuditLog(org_id=org_id, user_id=user_id, action=action,
                           entity=entity, entity_id=entity_id, meta=meta or {}))
