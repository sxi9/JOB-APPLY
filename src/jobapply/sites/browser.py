"""Persistent browser profiles for assisted sites (LinkedIn, Naukri, ...).

One Chromium user-data directory per user per site keeps the login between runs, so the user signs
in once in a visible window and later runs reuse the cookies. `launch_persistent_context` needs the
full Chromium build (`uv run playwright install chromium`), not the headless shell; when the bundled
build is missing we fall back to the system Chrome (`channel="chrome"`).
"""

from __future__ import annotations

import json
import os
import random
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from jobapply.config import get_settings
from jobapply.logs import get_logger

log = get_logger(__name__)

# A current desktop Chrome on Windows. Used by the guest search client (sites.linkedin.search) so the
# plain-HTTP path advertises the same browser family as the real profile; the persistent context itself
# runs with the bundled Chromium's own user agent so navigator.userAgentData / Sec-CH-UA never contradict it.
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/143.0.0.0 Safari/537.36"
)

_LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-infobars",
]


def profile_dir(user_id: int, site: str) -> Path:
    """`<data_dir>/browser-profiles/<user_id>/<site>`, created on first use."""
    if not re.fullmatch(r"[a-z0-9_-]+", site):
        raise ValueError(f"bad site key {site!r}")
    path = get_settings().browser_profiles_dir / str(int(user_id)) / site
    path.mkdir(parents=True, exist_ok=True)
    return path


IMPORT_MARKER = ".imported-from.json"
# Session state only: cookies, web storage and preferences. Never Login Data (saved passwords), History,
# Web Data (autofill), Bookmarks or any cache.
_SESSION_FILES = ["Network/Cookies", "Network/Cookies-journal", "Preferences", "Secure Preferences"]
_SESSION_DIRS = ["Local Storage", "Session Storage"]


def chrome_user_data_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"


def chrome_profile_folder(profile_name: str, user_data: Path | None = None) -> str:
    """Folder (`Default`, `Profile 2`, ...) of the Chrome profile the user sees as `profile_name`."""
    user_data = user_data or chrome_user_data_dir()
    state = json.loads((user_data / "Local State").read_text(encoding="utf-8"))
    cache = state.get("profile", {}).get("info_cache", {})
    wanted = profile_name.strip().lower()
    hits = [folder for folder, info in cache.items() if str(info.get("name", "")).strip().lower() == wanted]
    if not hits:
        names = sorted(str(i.get("name", "")) for i in cache.values())
        raise ValueError(f"no Chrome profile named {profile_name!r}; profiles: {names}")
    if len(hits) > 1:
        raise ValueError(f"{len(hits)} Chrome profiles are named {profile_name!r} ({hits}); rename one first")
    return hits[0]


def import_chrome_session(user_id: int, site: str, profile_name: str, user_data: Path | None = None) -> Path:
    """Copy the signed-in session of one Chrome profile into the tool's own profile for `user_id`/`site`.

    Only session files are copied (see `_SESSION_FILES` / `_SESSION_DIRS`) plus the `os_crypt` entry of
    Local State that Chrome needs to decrypt them. The copy is launched with the installed Chrome
    (`channel="chrome"`), because cookies are app-bound encrypted and only that binary can read them.
    The user's real Chrome profile is never modified.
    """
    user_data = user_data or chrome_user_data_dir()
    folder = chrome_profile_folder(profile_name, user_data)
    src = user_data / folder
    dest = profile_dir(user_id, site)
    for child in dest.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    state = json.loads((user_data / "Local State").read_text(encoding="utf-8"))
    (dest / "Local State").write_text(json.dumps({"os_crypt": state.get("os_crypt", {})}), encoding="utf-8")
    target = dest / "Default"
    copied: list[str] = []
    for rel in _SESSION_FILES:
        f = src / rel
        if f.exists():
            (target / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, target / rel)
            copied.append(rel)
    for rel in _SESSION_DIRS:
        d = src / rel
        if d.is_dir():
            shutil.copytree(d, target / rel, dirs_exist_ok=True)
            copied.append(rel + "/")
    (dest / IMPORT_MARKER).write_text(
        json.dumps({"chrome_profile": profile_name, "folder": folder, "copied": copied,
                    "imported_at": datetime.now().astimezone().isoformat()}, indent=1), encoding="utf-8")
    log.info("chrome session imported", user_id=user_id, site=site, chrome_profile=profile_name, folder=folder,
             copied=copied)
    return dest


def is_imported_profile(user_data: Path) -> bool:
    return (user_data / IMPORT_MARKER).exists()


_SAMESITE = {"lax": "Lax", "strict": "Strict", "none": "None", "no_restriction": "None",
             "unspecified": "Lax", "": "Lax"}


def _clean_domain(domain: str) -> str:
    """Strip anything but the hostname. A domain pasted through Markdown can arrive as
    `[www.linkedin.com](https://www.linkedin.com)`; keep the leading-dot flag and the host."""
    raw = str(domain or "").strip()
    dot = raw.startswith(".")
    m = re.search(r"([a-z0-9.-]+\.[a-z]{2,})", raw, re.I)
    host = m.group(1) if m else raw.lstrip(".")
    return ("." + host) if dot and not host.startswith(".") else host


def cookies_from_export(data: Any, allowed: tuple[str, ...] = ("linkedin.com",)) -> list[dict[str, Any]]:
    """Convert a browser cookie export (the common extension JSON: a list of
    {name,value,domain,path,expirationDate,secure,httpOnly,sameSite,session}) into the shape
    Playwright's add_cookies wants. Only cookies whose host ends with one of `allowed` are kept."""
    rows = data.get("cookies", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ValueError("cookie file must be a JSON array of cookies (or {cookies:[...]})")
    out: list[dict[str, Any]] = []
    for c in rows:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        domain = _clean_domain(c.get("domain", ""))
        host = domain.lstrip(".")
        if allowed and not any(host == a or host.endswith("." + a) for a in allowed):
            continue
        cookie: dict[str, Any] = {
            "name": str(c["name"]),
            "value": str(c.get("value", "")),
            "domain": domain,
            "path": str(c.get("path") or "/"),
            "secure": bool(c.get("secure", False)),
            "httpOnly": bool(c.get("httpOnly", False)),
            "sameSite": _SAMESITE.get(str(c.get("sameSite") or "").lower(), "Lax"),
        }
        exp = c.get("expirationDate") or c.get("expires")
        if exp and not c.get("session"):
            cookie["expires"] = int(float(exp))
        out.append(cookie)
    if not out:
        raise ValueError(f"no cookies for {allowed} found in the export")
    return out


def import_cookies_file(user_id: int, site: str, path: Path, allowed: tuple[str, ...] = ("linkedin.com",)) -> int:
    """Load a cookie export from `path` into the persistent profile for `user_id`/`site` via a short
    headless Playwright session, so the saved profile is logged in. Returns the number of cookies set.
    The file is read from disk by this process; its values are never logged or returned."""
    import json as _json

    from playwright.sync_api import sync_playwright

    data = _json.loads(Path(path).read_text(encoding="utf-8-sig"))
    cookies = cookies_from_export(data, allowed)
    with sync_playwright() as pw:
        context = launch_persistent(pw, user_id, site, headless=True)
        try:
            context.add_cookies(cookies)
        finally:
            context.close()
    log.info("cookies imported", user_id=user_id, site=site, count=len(cookies),
             names=sorted({c["name"] for c in cookies}))
    return len(cookies)


def machine_timezone() -> str:
    """IANA name of the machine's timezone, falling back to UTC when it cannot be resolved."""
    try:
        import tzlocal  # type: ignore[import-not-found]

        name = tzlocal.get_localzone_name()
        if name:
            return name
    except Exception:  # noqa: BLE001 - optional dependency
        pass
    key = getattr(datetime.now().astimezone().tzinfo, "key", None)
    if key:
        return str(key)
    return "UTC"


def launch_persistent(pw: Any, user_id: int, site: str, headless: bool | None = None,
                      viewport: tuple[int, int] = (1280, 900)):
    """Open (or reuse) the persistent profile for `user_id`/`site` and return the BrowserContext.

    `headless=None` reads `JOBAPPLY_<SITE>_HEADLESS` (LinkedIn: `linkedin_headless`), default visible.
    Headless runs use the full Chromium build in its new headless mode (`channel="chromium"`) rather than
    the separately fingerprinted headless shell, so headed and headless sessions share one binary and UA.
    """
    settings = get_settings()
    if headless is None:
        headless = bool(getattr(settings, f"{site}_headless", False))
    user_data = profile_dir(user_id, site)
    kwargs: dict[str, Any] = {
        "user_data_dir": str(user_data),
        "headless": headless,
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "locale": "en-US",
        "timezone_id": machine_timezone(),
        "args": list(_LAUNCH_ARGS),
        "ignore_default_args": ["--enable-automation"],
    }
    if is_imported_profile(user_data):
        # App-bound cookie encryption: only the installed Chrome can read a session copied from it.
        kwargs["channel"] = "chrome"
    elif settings.chromium_executable:
        kwargs["executable_path"] = settings.chromium_executable
    elif headless:
        kwargs["channel"] = "chromium"
    try:
        context = pw.chromium.launch_persistent_context(**kwargs)
    except Exception as exc:  # noqa: BLE001 - Playwright raises its own Error subclasses
        if "executable_path" in kwargs or "doesn't exist" not in str(exc).lower():
            raise
        # Only chromium_headless_shell is installed: use the system Chrome instead.
        log.warning("bundled chromium missing, using system chrome; run `uv run playwright install chromium`",
                    error=str(exc).splitlines()[0])
        kwargs["channel"] = "chrome"
        context = pw.chromium.launch_persistent_context(**kwargs)
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
    log.info("browser profile opened", user_id=user_id, site=site, headless=headless, profile=str(user_data))
    return context


def human_pause(page: Any, lo_ms: int = 400, hi_ms: int = 1500) -> int:
    """Sleep a random, jittered interval so actions do not fire at machine speed. Returns the ms slept."""
    lo, hi = sorted((max(0, int(lo_ms)), max(0, int(hi_ms))))
    ms = random.randint(lo, hi) if hi > lo else lo
    waiter = getattr(page, "wait_for_timeout", None)
    if callable(waiter):
        waiter(ms)
    else:
        time.sleep(ms / 1000)
    return ms


_HOURS_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_active_hours(spec: str) -> tuple[int, int]:
    """`"08:00-23:00"` -> start and end minutes since midnight."""
    m = _HOURS_RE.match(spec or "")
    if not m:
        raise ValueError(f"active hours must look like HH:MM-HH:MM, got {spec!r}")
    h1, m1, h2, m2 = (int(x) for x in m.groups())
    if not (0 <= h1 <= 24 and 0 <= h2 <= 24 and m1 < 60 and m2 < 60):
        raise ValueError(f"bad time in {spec!r}")
    return h1 * 60 + m1, h2 * 60 + m2


def is_active_hours(now: datetime | None = None, spec: str = "08:00-23:00") -> bool:
    """True when `now` (local time) falls inside the window. A window ending before it starts wraps midnight."""
    start, end = parse_active_hours(spec)
    now = now or datetime.now()
    cur = now.hour * 60 + now.minute
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


__all__ = ["profile_dir", "launch_persistent", "human_pause", "is_active_hours", "parse_active_hours",
           "machine_timezone", "DESKTOP_USER_AGENT", "import_chrome_session", "chrome_profile_folder",
           "is_imported_profile", "cookies_from_export", "import_cookies_file"]
