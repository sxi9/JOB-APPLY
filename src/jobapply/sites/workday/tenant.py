"""Workday tenant URLs.

A Workday career site lives at ``https://<tenant>.wd<N>.myworkdayjobs.com/<site>`` (optionally with a locale
segment, ``/en-US/<site>``). A job posting is ``.../<site>/job/<location>/<Title>_<ReqId>`` (older sites use
``.../<site>/details/<Title>_<ReqId>``), its apply flow ``.../job/.../apply`` and the method pages
``.../apply/applyManually`` / ``.../apply/autofillWithResume`` (all seen live on 2026-10-29).

The candidate account is per tenant: every external site of one tenant shares the same Candidate Home, so the
vault entry is keyed ``workday:<tenant>`` (kind ``login``); the site is recorded inside the secret for
reference only.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SITE = "workday"

_URL = re.compile(
    r"^(?P<scheme>https?)://(?P<tenant>[\w-]+)\.(?P<wd>wd\d+)\.myworkdayjobs\.com"
    r"(?:/(?P<locale>[a-z]{2}-[A-Z]{2}))?"
    r"(?:/(?P<site>[\w.-]+))?"
    r"(?P<rest>/.*)?$",
    re.I,
)
_JOB_PATH = re.compile(r"^/(?:job|details)/", re.I)
_APPLY_TAIL = re.compile(r"/apply(?:/[\w-]+)?/?$", re.I)
# Path segments that are never a site slug (the tenant root or a Workday system path).
_NOT_SITES = {"", "job", "details", "apply", "login", "wday"}


@dataclass(frozen=True)
class WorkdayTenant:
    tenant: str
    wd: str
    site: str
    locale: str | None = None
    job_path: str | None = None  # "/job/Bengaluru/Title_JR1" (no /apply tail), None for a site root URL

    @property
    def host(self) -> str:
        return f"{self.tenant}.{self.wd}.myworkdayjobs.com"

    @property
    def base_url(self) -> str:
        loc = f"/{self.locale}" if self.locale else ""
        return f"https://{self.host}{loc}/{self.site}"

    @property
    def job_url(self) -> str | None:
        return f"{self.base_url}{self.job_path}" if self.job_path else None

    @property
    def apply_url(self) -> str | None:
        return f"{self.job_url}/apply" if self.job_path else None

    @property
    def apply_manually_url(self) -> str | None:
        return f"{self.apply_url}/applyManually" if self.job_path else None

    @property
    def autofill_url(self) -> str | None:
        return f"{self.apply_url}/autofillWithResume" if self.job_path else None

    @property
    def candidate_home_url(self) -> str:
        return f"{self.base_url}/userHome"

    @property
    def login_url(self) -> str:
        return f"{self.base_url}/login"

    @property
    def vault_site(self) -> str:
        """Vault ``site`` of this tenant's candidate account (kind ``login``)."""
        return f"{SITE}:{self.tenant.lower()}"

    @property
    def req_id(self) -> str | None:
        """``JR104387`` from ``.../Associate--Information-Security-Analyst_JR104387``."""
        if not self.job_path:
            return None
        m = re.search(r"_([A-Za-z]*[\w-]*\d[\w-]*)$", self.job_path.rstrip("/"))
        return m.group(1) if m else None


def parse_tenant_url(url: str) -> WorkdayTenant | None:
    """``WorkdayTenant`` for a Workday career-site URL, or None for anything else."""
    m = _URL.match((url or "").strip())
    if not m:
        return None
    site = m.group("site") or ""
    rest = m.group("rest") or ""
    if site.lower() in _NOT_SITES:
        if site.lower() in ("job", "details"):
            # ``https://x.wd5.myworkdayjobs.com/job/...`` (no site segment): not a valid career-site URL.
            return None
        if not site:
            return None
    rest = rest.split("?", 1)[0].split("#", 1)[0]
    job_path: str | None = None
    if _JOB_PATH.match(rest):
        job_path = _APPLY_TAIL.sub("", rest.rstrip("/"))
    return WorkdayTenant(tenant=m.group("tenant"), wd=m.group("wd").lower(), site=site, locale=m.group("locale"),
                         job_path=job_path or None)


def is_workday_url(url: str) -> bool:
    return parse_tenant_url(url) is not None


__all__ = ["SITE", "WorkdayTenant", "parse_tenant_url", "is_workday_url"]
