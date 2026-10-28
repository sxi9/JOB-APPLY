"""Shared collector interface and the normalized job record from docs/SOURCES.md."""

from __future__ import annotations

import html
import re
from abc import ABC, abstractmethod
from datetime import UTC, date, datetime
from typing import Any, ClassVar

import httpx
from pydantic import BaseModel, Field

from jobapply.config import get_settings
from jobapply.logs import get_logger


class NormalizedJob(BaseModel):
    title: str
    company: str
    location: str = ""
    remote: bool = False
    posted_at: datetime | None = None
    url: str = ""
    apply_url: str = ""
    description: str = ""
    ats_type: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    currency: str | None = None
    source: str
    source_id: str
    raw: dict[str, Any] = Field(default_factory=dict, repr=False)


class CollectorError(RuntimeError):
    pass


class BaseCollector(ABC):
    """One adapter per source. `fetch()` does the network call, `parse()` is pure,
    so tests feed recorded fixtures through an `httpx.MockTransport`."""

    source: ClassVar[str]
    ats_type: ClassVar[str | None] = None

    def __init__(self, client: httpx.Client | None = None):
        self._client = client
        self.log = get_logger(f"jobapply.collectors.{self.source}")

    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            s = get_settings()
            self._client = httpx.Client(
                timeout=s.http_timeout,
                headers={"User-Agent": s.user_agent, "Accept": "application/json"},
                follow_redirects=True,
                transport=httpx.HTTPTransport(retries=2),
            )
        return self._client

    @property
    def key(self) -> str:
        """Unique name of this collector instance, e.g. `greenhouse:stripe`."""
        return self.source

    def get_json(self, url: str, **kwargs: Any) -> Any:
        resp = self.client.get(url, **kwargs)
        if resp.status_code >= 400:
            raise CollectorError(f"{self.key}: HTTP {resp.status_code} from {url}")
        return resp.json()

    @abstractmethod
    def collect(self) -> list[NormalizedJob]: ...


# ---------------------------------------------------------------- helpers

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"[ \t\r\f\v]+")
_NL = re.compile(r"\n\s*\n+")


def html_to_text(value: str | None) -> str:
    """Strip tags and entities. Handles double-escaped HTML (Greenhouse `content`)."""
    if not value:
        return ""
    text = html.unescape(value)
    if "&lt;" in value or "&gt;" in value:
        text = html.unescape(text)
    text = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", text)
    text = re.sub(r"(?i)<li[^>]*>", "\n- ", text)
    text = _TAG.sub("", text)
    text = _WS.sub(" ", text)
    return _NL.sub("\n\n", text).strip()


def parse_dt(value: Any) -> datetime | None:
    """ISO 8601 strings, dates, or epoch seconds/milliseconds to aware UTC."""
    if value in (None, "", 0):
        return None
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime(value.year, value.month, value.day)
    elif isinstance(value, int | float) or (isinstance(value, str) and value.isdigit()):
        num = float(value)
        dt = datetime.fromtimestamp(num / 1000 if num > 1e11 else num, UTC)
    elif isinstance(value, str):
        s = value.strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S", "%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%d"):
                try:
                    dt = datetime.strptime(value.strip(), fmt)
                    break
                except ValueError:
                    continue
            else:
                return None
    else:
        return None
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


_REMOTE = re.compile(r"\bremote\b|\banywhere\b|\bworldwide\b|work from home|\bwfh\b", re.I)


def looks_remote(*texts: str | None) -> bool:
    return any(t and _REMOTE.search(t) for t in texts)


_MONEY = re.compile(r"([$€£₹]|USD|EUR|GBP|INR)?\s*(\d[\d,]*(?:\.\d+)?)\s*([kKmM])?")
_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}


def parse_salary(text: str | None) -> tuple[float | None, float | None, str | None]:
    """'$211.4K - $290.6K' -> (211400, 290600, 'USD'). Returns Nones when unsure."""
    if not text:
        return None, None, None
    values: list[float] = []
    currency = None
    for sym, num, mult in _MONEY.findall(text):
        if not (sym or mult):
            continue
        amount = float(num.replace(",", ""))
        amount *= {"k": 1e3, "m": 1e6}.get(mult.lower(), 1) if mult else 1
        values.append(amount)
        currency = currency or _SYMBOL.get(sym, sym or None)
    if not values:
        return None, None, None
    return min(values), max(values), currency


def first(*values: Any) -> Any:
    return next((v for v in values if v), None)
