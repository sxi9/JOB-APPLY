"""Cookie-import login for the assisted sites, shared by the dashboard (Settings -> Sites, the onboarding
"Connect" step) and the CLI (`jobapply sites import-cookies` / `check`).

The user exports the site's cookies from their own logged-in browser (the common cookie-extension JSON) and
uploads the file; it is written under the git-ignored per-user data dir (`data/cookies/<user id>/<site>.json`),
loaded into that user's persistent headless browser profile through `sites.browser.import_cookies_file`
(only the site's own domains are kept, values are never logged) and, for the sites that have a session
checker, verified by opening the site headless. Nothing here ever types a credential.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jobapply.config import get_settings
from jobapply.logs import get_logger
from jobapply.sites import browser

log = get_logger(__name__)

# Cookie domains kept from an export, per site (the site's own `COOKIE_DOMAINS`).
COOKIE_DOMAINS: dict[str, tuple[str, ...]] = {
    "linkedin": ("linkedin.com",),
    "naukri": ("naukri.com",),
    "instahyre": ("instahyre.com",),
    "cutshort": ("cutshort.io",),
    "wellfound": ("wellfound.com", "angel.co"),
}
LABELS: dict[str, str] = {
    "linkedin": "LinkedIn", "naukri": "Naukri", "instahyre": "Instahyre", "cutshort": "Cutshort",
    "wellfound": "Wellfound",
}
# Sites whose package has `session.check_session(user_id)`: the import is verified by opening the site.
VERIFIED_SITES = ("naukri", "instahyre", "cutshort", "wellfound")
SITES = tuple(COOKIE_DOMAINS)


@dataclass
class ConnectResult:
    site: str
    count: int  # cookies loaded into the profile
    status: str  # ok | login | challenge | unknown (ok for LinkedIn, which only imports)

    @property
    def login_status(self) -> str:
        """The dashboard vocabulary for `UserSite.login_status` (login -> expired)."""
        return "expired" if self.status == "login" else self.status


def is_site(site: str) -> bool:
    return site in COOKIE_DOMAINS


def label(site: str) -> str:
    return LABELS.get(site, site)


def cookie_file(user_id: int, site: str) -> Path:
    """Where a user's cookie export for `site` lives (git-ignored, per user)."""
    return get_settings().data_dir / "cookies" / str(int(user_id)) / f"{site}.json"


def save_cookie_export(user_id: int, site: str, raw: str) -> Path:
    path = cookie_file(user_id, site)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(raw, encoding="utf-8")
    return path


def check_session_for(site: str, user_id: int, engine: Any = None) -> str:
    """Open the user's persistent profile for `site` headless and classify it: ok | login | challenge | unknown.
    The site's own checker records the result on the user's site row."""
    if site == "linkedin":
        from playwright.sync_api import sync_playwright

        from jobapply.db import session_scope
        from jobapply.models import User
        from jobapply.sites.linkedin.session import LinkedInSession

        with session_scope(engine) as db:
            user = db.get(User, int(user_id))
            if user is None:
                raise ValueError(f"no user with id {user_id}")
            with sync_playwright() as pw:
                context = browser.launch_persistent(pw, user.id, site, headless=True)
                try:
                    page = context.pages[0] if context.pages else context.new_page()
                    status = LinkedInSession(context, user, db).check(page)
                finally:
                    try:
                        context.close()
                    except Exception:  # noqa: BLE001
                        pass
        return "login" if status == "expired" else status
    if site == "naukri":
        from jobapply.db import session_scope
        from jobapply.sites.naukri.session import check_session

        with session_scope(engine) as db:
            return check_session(int(user_id), db)
    if site == "instahyre":
        from jobapply.sites.instahyre.session import check_session

        return check_session(int(user_id), engine=engine)
    if site == "cutshort":
        from jobapply.sites.cutshort.session import check_session

        return check_session(int(user_id), engine=engine)
    if site == "wellfound":
        from jobapply.sites.wellfound.session import check_session

        return check_session(int(user_id), engine=engine)
    raise ValueError(f"unknown site {site!r}")


def import_cookies_for(site: str, user_id: int, path: Path, *, verify: bool = True, engine: Any = None,
                       ) -> ConnectResult:
    """Load the cookie export at `path` into the user's `site` profile; verify the session afterwards for the
    sites that have a checker. Raises ValueError on an export with no cookies for the site."""
    if not is_site(site):
        raise ValueError(f"unknown site {site!r}")
    count = browser.import_cookies_file(int(user_id), site, Path(path), allowed=COOKIE_DOMAINS[site])
    status = "ok"
    if verify and site in VERIFIED_SITES:
        status = check_session_for(site, int(user_id), engine=engine)
    log.info("site connected", site=site, user_id=user_id, cookies=count, status=status)
    return ConnectResult(site, count, status)


def connect_site(user_id: int, site: str, raw: str, *, verify: bool = True, engine: Any = None) -> ConnectResult:
    """Dashboard flow: save the pasted / uploaded export under the user's data dir, then import it."""
    path = save_cookie_export(user_id, site, raw)
    return import_cookies_for(site, user_id, path, verify=verify, engine=engine)


__all__ = ["COOKIE_DOMAINS", "LABELS", "SITES", "VERIFIED_SITES", "ConnectResult", "check_session_for",
           "connect_site", "cookie_file", "import_cookies_for", "is_site", "label", "save_cookie_export"]
