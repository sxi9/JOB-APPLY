"""Jinja filters for the dashboard templates."""

from __future__ import annotations

from datetime import UTC, datetime

STATUS_COLORS = {
    "new": "blue", "scored": "gray", "queued": "amber", "approved": "teal", "dry_run": "violet",
    "applied": "green", "failed": "red", "paused": "red", "skipped": "gray", "replied": "teal",
    "assessment": "amber", "interview": "green", "offer": "green", "rejected": "gray",
    "recruiter_question": "amber", "verification": "gray", "spam": "gray", "rejection": "gray", "other": "gray",
}


def ago(value: datetime | None) -> str:
    if not value:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    seconds = int((datetime.now(UTC) - value).total_seconds())
    if seconds < 0:
        return "soon"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds // size}{unit} ago"
    return "just now"


def dt(value: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return value.strftime(fmt) + " UTC" if value else "—"


def money(lo: float | None, hi: float | None, currency: str | None) -> str:
    if not lo and not hi:
        return ""

    def short(v: float) -> str:
        if v >= 100_000 and currency == "INR":
            return f"{v / 100_000:.1f}L"
        return f"{v / 1000:.0f}k" if v >= 1000 else f"{v:.0f}"

    rng = "–".join(short(v) for v in (lo, hi) if v)
    return f"{currency or ''} {rng}".strip()


def score_class(score: int | None) -> str:
    if score is None:
        return "score-none"
    return "score-high" if score >= 75 else "score-mid" if score >= 50 else "score-low"


def status_color(status) -> str:
    return STATUS_COLORS.get(getattr(status, "value", status), "gray")


def label(value) -> str:
    return str(getattr(value, "value", value)).replace("_", " ")


def register(env) -> None:
    env.filters.update(ago=ago, dt=dt, money=money, score_class=score_class, status_color=status_color, label=label)
