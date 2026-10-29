"""Global runtime switches: environment defaults, overridable from the admin page (app_settings)."""

from __future__ import annotations

from sqlmodel import Session

from jobapply.config import get_settings
from jobapply.models import AppSetting


def get_value(session: Session, key: str, default: str | None = None) -> str | None:
    row = session.get(AppSetting, key)
    return row.value if row else default


def set_value(session: Session, key: str, value: str) -> None:
    row = session.get(AppSetting, key) or AppSetting(key=key)
    row.value = value
    session.add(row)


def kill_switch_on(session: Session) -> bool:
    """True when applying must stop. Either the env flag or the admin toggle turns it on."""
    return get_settings().kill_switch or get_value(session, "kill_switch") == "on"


def global_dry_run(session: Session) -> bool:
    """Dry run unless both the env flag and the admin toggle allow real submits."""
    return get_settings().dry_run or get_value(session, "dry_run", "on") != "off"


def collect_interval(session: Session) -> int:
    return int(get_value(session, "collect_interval_minutes") or get_settings().collect_interval_minutes)


def referral_send_enabled(session: Session) -> bool:
    """True only when referral outreach may actually be sent. OFF by default: both the env flag
    (`JOBAPPLY_REFERRAL_SEND`) and the admin toggle (`referral_send` app setting) stay on the safe side,
    so sending is enabled only when one of them is explicitly turned on. This gates `send_referral`; it
    never affects drafting, which is always allowed and contacts no one."""
    return bool(get_settings().referral_send) or get_value(session, "referral_send") == "on"
