"""User accounts: password hashing, creation, authentication."""

from __future__ import annotations

import hashlib
import hmac
import secrets

from sqlmodel import Session, select

from jobapply.models import Role, User, UserSite

# Sites a user can enable. Auto-submit starts off everywhere; dry-run review comes first.
SITES: dict[str, dict] = {
    "greenhouse": {"daily_cap": 30, "kind": "ats"},
    "lever": {"daily_cap": 30, "kind": "ats"},
    "ashby": {"daily_cap": 30, "kind": "ats"},
    "workable": {"daily_cap": 30, "kind": "ats"},
    "smartrecruiters": {"daily_cap": 30, "kind": "ats"},
    "workday": {"daily_cap": 10, "kind": "account"},
    "linkedin": {"daily_cap": 15, "kind": "assisted"},
    "naukri": {"daily_cap": 25, "kind": "assisted"},
    "instahyre": {"daily_cap": 15, "kind": "assisted"},
    "cutshort": {"daily_cap": 15, "kind": "assisted"},
    "wellfound": {"daily_cap": 15, "kind": "assisted"},
    "email": {"daily_cap": 10, "kind": "email"},
}

_ITERATIONS = 240_000


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS).hex()
    return f"pbkdf2_sha256${_ITERATIONS}${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _algo, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    return hmac.compare_digest(candidate, digest)


def create_user(session: Session, username: str, password: str, *, email: str = "", role: Role = Role.user,
                profile: dict | None = None, filters: dict | None = None, preferences: dict | None = None) -> User:
    if session.exec(select(User).where(User.username == username)).first():
        raise ValueError(f"user {username!r} already exists")
    user = User(username=username, email=email, password_hash=hash_password(password), role=role,
                profile=profile or {}, filters=filters or {}, preferences=preferences or {})
    session.add(user)
    session.flush()
    for site, cfg in SITES.items():
        session.add(UserSite(user_id=user.id, site=site, daily_cap=cfg["daily_cap"],
                             enabled=cfg["kind"] in ("ats", "email"),
                             login_status="n/a" if cfg["kind"] == "ats" else "unknown"))
    return user


def authenticate(session: Session, username: str, password: str) -> User | None:
    user = session.exec(select(User).where(User.username == username)).first()
    if user and user.is_active and verify_password(password, user.password_hash):
        return user
    # Spend comparable time on unknown users.
    verify_password(password, hash_password("timing"))
    return None
