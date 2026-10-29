"""Workday session plumbing in the shape the other assisted sites use (``<Site>Session``, ``throttled``).

There is no single Workday login: the candidate account is per tenant (``tenant.WorkdayTenant.vault_site``)
and the persistent headless profile (``sites.browser.launch_persistent(pw, user_id, "workday")``) keeps every
tenant's cookies between runs. ``WorkdaySession.check`` therefore never navigates: it reports ``ok`` and leaves
the per-tenant sign-in / create-account decision to ``account.WorkdayAccount`` on the job's own apply page.
The cooldown after a CAPTCHA / block is shared by all tenants (``user.preferences["workday_cooldown_until"]``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from jobapply.logs import get_logger
from jobapply.models import User, UserSite, utcnow
from jobapply.repo import UserScope
from jobapply.sites.workday.tenant import SITE

log = get_logger(__name__)

COOLDOWN_KEY = "workday_cooldown_until"
DEFAULT_COOLDOWN_HOURS = 6
BLOCK_STATUSES = (403, 429)


def cooldown_until(user: User) -> datetime | None:
    raw = (user.preferences or {}).get(COOLDOWN_KEY)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def throttled(user: User, site: UserSite | None = None, now: datetime | None = None) -> bool:
    """True while a challenge cooldown is active (or the site row says ``challenge`` with no cooldown yet)."""
    now = now or utcnow()
    until = cooldown_until(user)
    if until and until > now:
        return True
    if site is not None and site.login_status == "challenge":
        return until is None or until > now
    return False


class WorkdaySession:
    """The user's persistent Workday browser context plus the DB rows tracking its state."""

    def __init__(self, context: Any, user: User, session_db: Any, page: Any = None):
        self.context = context
        self.user = user
        self.db = session_db
        self.scope = UserScope(session_db, user)
        self._page = page

    @property
    def page(self) -> Any:
        if self._page is None:
            if self.context is None:
                raise RuntimeError("WorkdaySession has no browser context and no page")
            self._page = self.context.new_page()
        return self._page

    def _site(self) -> UserSite:
        site = self.scope.site(SITE)
        if site is None:
            site = UserSite(user_id=self.user.id, site=SITE, daily_cap=10, enabled=False, login_status="unknown")
            self.db.add(site)
        return site

    def _record(self, status: str) -> None:
        site = self._site()
        site.login_status = status
        if status == "ok":
            site.last_login_at = utcnow()
            self.clear_cooldown()
        self.db.add(site)
        if status == "challenge":
            self.set_cooldown()
        self.db.commit()
        log.info("workday session", user=self.user.username, status=status)

    def record_challenge(self) -> datetime:
        self._record("challenge")
        return cooldown_until(self.user) or utcnow()

    def record_ok(self) -> None:
        self._record("ok")

    def set_cooldown(self, hours: float = DEFAULT_COOLDOWN_HOURS) -> datetime:
        until = utcnow() + timedelta(hours=hours)
        prefs = dict(self.user.preferences or {})
        prefs[COOLDOWN_KEY] = until.isoformat()
        self.user.preferences = prefs
        self.db.add(self.user)
        return until

    def clear_cooldown(self) -> None:
        prefs = dict(self.user.preferences or {})
        if prefs.pop(COOLDOWN_KEY, None) is not None:
            self.user.preferences = prefs
            self.db.add(self.user)

    def check(self, page: Any | None = None, timeout_ms: int = 0) -> str:
        """``ok`` unless a cooldown is active (``challenge``). Nothing is loaded: accounts are per tenant."""
        if throttled(self.user, self.scope.site(SITE)):
            return "challenge"
        return "ok"


__all__ = ["SITE", "COOLDOWN_KEY", "DEFAULT_COOLDOWN_HOURS", "BLOCK_STATUSES", "WorkdaySession", "throttled",
           "cooldown_until"]
