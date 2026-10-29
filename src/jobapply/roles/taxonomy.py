"""Curated career role taxonomy (data only) plus pure expansion/matching helpers.

`TAXONOMY` is a list of `Field`s. Each field has `subroles` (a `SubRole` carries title synonyms/keywords)
and an optional `exclude` list of title keywords that veto a match for that field (so "Security Guard" does
not read as Cybersecurity). Everything is lowercased on comparison.

Selection grammar used by the helpers:
  - "cybersecurity"       -> the whole field (all its sub-roles)
  - "cybersecurity:*"     -> the whole field (explicit form)
  - "cybersecurity:soc"   -> one sub-role

Helpers:
  - all_fields() / field(key) / subrole(key)
  - titles_for(selection) -> set[str]         union of title keywords the selection expands to
  - match_title(title, selection) -> bool      word-boundary match of those keywords against a job title,
                                               respecting each field's exclude list
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class SubRole:
    key: str
    label: str
    titles: tuple[str, ...]  # title synonyms / keywords that identify this sub-role


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    subroles: tuple[SubRole, ...]
    exclude: tuple[str, ...] = ()  # title keywords that veto a match for the whole field

    def all_titles(self) -> set[str]:
        return {t for sr in self.subroles for t in sr.titles}


def _f(key: str, label: str, subroles: list[SubRole], exclude: Iterable[str] = ()) -> Field:
    return Field(key=key, label=label, subroles=tuple(subroles), exclude=tuple(exclude))


def _s(key: str, label: str, titles: list[str]) -> SubRole:
    # Normalise once: lowercase, stripped, de-duplicated, order kept.
    seen: dict[str, None] = {}
    for t in titles:
        tt = " ".join(str(t).lower().split())
        if tt:
            seen.setdefault(tt, None)
    return SubRole(key=key, label=label, titles=tuple(seen))


# ---------------------------------------------------------------------------- the taxonomy

TAXONOMY: list[Field] = [
    # ======================================================= Cybersecurity (primary user's field) ===
    _f(
        "cybersecurity",
        "Cybersecurity",
        [
            _s("soc", "SOC analyst", [
                "soc analyst", "security operations", "security operations center",
                "security analyst", "soc engineer", "tier 1 analyst", "tier 2 analyst",
                "monitoring analyst", "information security analyst", "infosec analyst",
                "cyber security analyst", "cybersecurity analyst", "security operations analyst",
            ]),
            _s("threat_intel", "Threat intelligence", [
                "threat intelligence", "cyber threat intelligence", "cti", "threat intel",
                "threat intelligence analyst", "threat researcher", "threat research analyst",
            ]),
            _s("incident_response", "Incident response / DFIR", [
                "incident response", "incident responder", "dfir", "digital forensics",
                "forensics", "forensic analyst", "computer forensics", "csirt", "cirt",
                "incident handler", "security incident response",
            ]),
            _s("pentest", "Penetration testing / red team", [
                "penetration testing", "penetration tester", "pentester",
                "pen test", "pen tester", "red team", "red teamer", "offensive security",
                "ethical hacker", "ethical hacking", "exploit developer", "security researcher",
            ]),
            _s("blue_team", "Blue team", [
                "blue team", "blue teamer", "defensive security", "cyber defense",
                "cyber defence", "security defense analyst",
            ]),
            _s("grc", "GRC / compliance", [
                "grc", "governance risk and compliance", "governance risk compliance",
                "security compliance", "compliance analyst", "cybersecurity compliance",
                "risk and compliance", "it auditor", "security auditor", "it audit",
                "iso 27001", "soc 2", "security risk analyst", "information security risk",
                "third party risk",
            ]),
            _s("appsec", "Application / product security", [
                "application security", "product security", "appsec", "application security engineer",
                "product security engineer", "secure code", "software security", "sast", "dast",
                "api security", "devsecops",
            ]),
            _s("cloud_security", "Cloud security", [
                "cloud security", "cloud security engineer", "cloud security architect",
                "cspm", "aws security", "azure security", "gcp security", "kubernetes security",
                "container security",
            ]),
            _s("security_engineering", "Security engineering", [
                "security engineer", "security engineering", "information security engineer",
                "infosec engineer", "cyber security engineer", "cybersecurity engineer",
                "network security engineer",
            ]),
            _s("malware_analysis", "Malware analysis", [
                "malware analyst", "malware analysis", "malware researcher", "reverse engineer",
                "reverse engineering", "malware reverse engineer",
            ]),
            _s("vuln_management", "Vulnerability management", [
                "vulnerability management", "vulnerability analyst", "vulnerability assessment",
                "vuln management", "vulnerability engineer", "patch management",
                "vulnerability researcher",
            ]),
            _s("iam", "Identity & access management", [
                "identity and access management", "iam", "iam engineer", "identity engineer",
                "access management", "identity and access", "identity governance", "okta engineer",
                "pam engineer", "privileged access",
            ]),
            _s("osint", "OSINT", [
                "osint", "open source intelligence", "open-source intelligence", "osint analyst",
                "open source investigator",
            ]),
            _s("detection_engineering", "Detection engineering", [
                "detection engineering", "detection engineer", "detection and response",
                "siem engineer", "siem analyst", "security content engineer", "sigma rules",
            ]),
            _s("security_architecture", "Security architecture", [
                "security architect", "security architecture", "cybersecurity architect",
                "cyber security architect", "enterprise security architect",
                "information security architect",
            ]),
        ],
        exclude=[
            "physical security", "security guard", "security officer", "building security",
            "loss prevention", "national security guard", "security supervisor",
        ],
    ),
    # ======================================================= Software Engineering ===
    _f(
        "software_engineering",
        "Software Engineering",
        [
            _s("software", "Software engineer (general)", [
                "software engineer", "software developer", "sde", "swe", "programmer",
                "software development engineer",
            ]),
            _s("backend", "Backend", [
                "backend engineer", "backend developer", "back-end engineer", "back-end developer",
                "backend software engineer", "server-side engineer", "server side developer",
            ]),
            _s("frontend", "Frontend", [
                "frontend engineer", "frontend developer", "front-end engineer", "front-end developer",
                "ui engineer", "react developer", "angular developer", "vue developer",
                "javascript developer",
            ]),
            _s("fullstack", "Full-stack", [
                "full stack", "full-stack", "fullstack engineer", "full stack developer",
                "full stack engineer", "full-stack developer",
            ]),
            _s("mobile", "Mobile (iOS / Android)", [
                "mobile developer", "mobile engineer", "ios developer", "ios engineer",
                "android developer", "android engineer", "react native", "flutter developer",
            ]),
            _s("embedded", "Embedded", [
                "embedded engineer", "embedded software", "embedded systems", "firmware engineer",
                "firmware developer", "embedded developer",
            ]),
            _s("platform", "Platform", [
                "platform engineer", "platform developer", "developer platform engineer",
                "developer experience",
            ]),
            _s("api", "API", [
                "api engineer", "api developer", "integration engineer", "integrations engineer",
            ]),
        ],
    ),
    # ======================================================= DevOps / SRE / Infra ===
    _f(
        "devops",
        "DevOps / SRE / Infrastructure",
        [
            _s("devops", "DevOps", [
                "devops engineer", "devops", "ci/cd engineer", "release engineer", "build engineer",
            ]),
            _s("sre", "Site reliability", [
                "site reliability engineer", "sre", "reliability engineer", "production engineer",
            ]),
            _s("infra", "Infrastructure", [
                "infrastructure engineer", "infrastructure", "systems engineer",
                "cloud infrastructure engineer", "infra engineer",
            ]),
        ],
    ),
    # ======================================================= Data & AI ===
    _f(
        "data",
        "Data & AI",
        [
            _s("data_engineer", "Data engineer", [
                "data engineer", "data engineering", "etl developer", "big data engineer",
                "analytics engineer", "data platform engineer",
            ]),
            _s("data_scientist", "Data scientist", [
                "data scientist", "data science", "applied scientist",
            ]),
            _s("data_analyst", "Data / business analyst", [
                "data analyst", "business analyst", "bi analyst", "business intelligence",
                "analytics analyst", "reporting analyst",
            ]),
            _s("ml_engineer", "ML / AI engineer", [
                "machine learning engineer", "ml engineer", "ai engineer", "deep learning engineer",
                "nlp engineer", "computer vision engineer", "applied ml engineer",
            ]),
            _s("mlops", "MLOps", [
                "mlops engineer", "mlops", "ml platform engineer", "ml infrastructure",
                "machine learning platform",
            ]),
        ],
    ),
    # ======================================================= Product ===
    _f(
        "product",
        "Product Management",
        [
            _s("pm", "Product manager", [
                "product manager", "product management", "technical product manager", "tpm",
                "group product manager", "senior product manager",
            ]),
            _s("product_owner", "Product owner", [
                "product owner",
            ]),
            _s("product_analyst", "Product analyst", [
                "product analyst", "product operations",
            ]),
        ],
    ),
    # ======================================================= Design ===
    _f(
        "design",
        "Design",
        [
            _s("ux_ui", "UX / UI", [
                "ux designer", "ui designer", "ux/ui designer", "ui/ux designer", "user experience",
                "user interface designer", "ux/ui",
            ]),
            _s("product_design", "Product design", [
                "product designer", "product design", "senior product designer",
            ]),
            _s("research_visual", "UX research / visual", [
                "ux researcher", "user researcher", "interaction designer", "visual designer",
                "design researcher",
            ]),
        ],
    ),
    # ======================================================= QA / SDET ===
    _f(
        "qa",
        "QA / Test Engineering",
        [
            _s("qa", "QA engineer", [
                "qa engineer", "quality assurance", "qa analyst", "test engineer", "qa tester",
                "manual tester", "quality engineer",
            ]),
            _s("sdet", "SDET / automation", [
                "sdet", "software development engineer in test", "automation engineer",
                "test automation", "automation tester",
            ]),
        ],
    ),
    # ======================================================= IT Support / Sysadmin / Network ===
    _f(
        "it_ops",
        "IT Support / Sysadmin / Network",
        [
            _s("support", "IT support", [
                "it support", "help desk", "helpdesk", "desktop support", "technical support",
                "service desk", "it technician", "support engineer",
            ]),
            _s("sysadmin", "Sysadmin", [
                "system administrator", "sysadmin", "systems administrator", "it administrator",
                "windows administrator", "linux administrator",
            ]),
            _s("network", "Network", [
                "network engineer", "network administrator", "network admin", "noc engineer",
                "network operations",
            ]),
        ],
    ),
    # ======================================================= Cloud ===
    _f(
        "cloud",
        "Cloud Engineering",
        [
            _s("aws", "AWS", [
                "aws engineer", "aws cloud engineer", "aws solutions architect", "amazon web services",
                "aws developer",
            ]),
            _s("azure", "Azure", [
                "azure engineer", "azure cloud engineer", "azure administrator", "azure developer",
                "azure solutions architect",
            ]),
            _s("gcp", "GCP", [
                "gcp engineer", "google cloud engineer", "gcp cloud", "google cloud platform",
                "gcp developer",
            ]),
            _s("cloud_architect", "Cloud engineer / architect", [
                "cloud architect", "cloud engineer", "solutions architect", "cloud consultant",
                "cloud solutions architect",
            ]),
        ],
    ),
    # ======================================================= Project / Program management ===
    _f(
        "program",
        "Project / Program Management",
        [
            _s("project_manager", "Project manager", [
                "project manager", "pmp", "project management", "it project manager",
            ]),
            _s("program_manager", "Program manager", [
                "program manager", "technical program manager", "tpm", "delivery manager",
            ]),
            _s("scrum", "Scrum / agile", [
                "scrum master", "agile coach", "release train engineer",
            ]),
        ],
    ),
    # ======================================================= Sales ===
    _f(
        "sales",
        "Sales",
        [
            _s("sdr", "SDR / BDR", [
                "sales development representative", "sdr", "bdr", "business development representative",
                "lead generation",
            ]),
            _s("ae", "Account executive", [
                "account executive", "sales executive", "enterprise sales", "sales representative",
                "inside sales",
            ]),
            _s("sales_eng", "Sales / solutions engineer", [
                "sales engineer", "solutions engineer", "pre-sales", "presales", "solutions consultant",
            ]),
            _s("account_mgr", "Account manager", [
                "account manager", "key account manager", "strategic account manager",
            ]),
        ],
    ),
    # ======================================================= Marketing ===
    _f(
        "marketing",
        "Marketing",
        [
            _s("digital", "Digital / growth", [
                "digital marketing", "seo", "sem", "performance marketing", "growth marketing",
                "paid media", "marketing manager",
            ]),
            _s("content", "Content", [
                "content marketing", "content writer", "copywriter", "content strategist",
                "content manager",
            ]),
            _s("product_marketing", "Product marketing", [
                "product marketing", "pmm", "product marketing manager",
            ]),
            _s("social", "Social / community", [
                "social media", "social media manager", "community manager",
            ]),
        ],
    ),
    # ======================================================= Finance & Accounting ===
    _f(
        "finance",
        "Finance & Accounting",
        [
            _s("accountant", "Accountant", [
                "accountant", "accounting", "staff accountant", "accounts payable", "accounts receivable",
            ]),
            _s("financial_analyst", "Financial analyst", [
                "financial analyst", "fp&a", "finance analyst", "fpa analyst",
            ]),
            _s("controller", "Controller", [
                "controller", "financial controller", "finance controller",
            ]),
        ],
    ),
    # ======================================================= HR / People ===
    _f(
        "hr",
        "HR / People",
        [
            _s("recruiter", "Recruiting", [
                "recruiter", "talent acquisition", "technical recruiter", "sourcer",
                "recruiting coordinator",
            ]),
            _s("hr_generalist", "HR generalist / ops", [
                "hr generalist", "human resources", "hr manager", "people operations", "hrbp",
                "hr business partner",
            ]),
        ],
    ),
    # ======================================================= Customer Success & Support ===
    _f(
        "customer",
        "Customer Success & Support",
        [
            _s("csm", "Customer success", [
                "customer success", "customer success manager", "csm", "customer success engineer",
            ]),
            _s("support_agent", "Customer support", [
                "customer support", "support specialist", "customer service", "customer care",
            ]),
        ],
    ),
]


# ---------------------------------------------------------------------------- lookups

@lru_cache(maxsize=1)
def _field_index() -> dict[str, Field]:
    return {fld.key: fld for fld in TAXONOMY}


@lru_cache(maxsize=1)
def _subrole_index() -> dict[str, tuple[Field, SubRole]]:
    out: dict[str, tuple[Field, SubRole]] = {}
    for fld in TAXONOMY:
        for sr in fld.subroles:
            out[f"{fld.key}:{sr.key}"] = (fld, sr)
    return out


def all_fields() -> list[Field]:
    """Every field in the taxonomy, in display order."""
    return list(TAXONOMY)


def field(key: str) -> Field | None:
    """The field with this key, or None."""
    return _field_index().get((key or "").strip().lower())


def subrole(key: str) -> SubRole | None:
    """The sub-role for a "field:subrole" key, or None."""
    hit = _subrole_index().get((key or "").strip().lower())
    return hit[1] if hit else None


def _resolve(selection: Iterable[str]) -> list[tuple[Field, set[str]]]:
    """Group a selection into (field, titles chosen for that field). A bare field key or "field:*" picks
    the whole field; "field:subrole" picks one sub-role. Unknown keys are ignored."""
    chosen: dict[str, tuple[Field, set[str]]] = {}
    for raw in selection or []:
        key = (raw or "").strip().lower()
        if not key:
            continue
        fkey, _, skey = key.partition(":")
        fld = field(fkey)
        if fld is None:
            continue
        bucket = chosen.setdefault(fld.key, (fld, set()))[1]
        if not skey or skey == "*":
            bucket.update(fld.all_titles())
        else:
            hit = _subrole_index().get(f"{fld.key}:{skey}")
            if hit:
                bucket.update(hit[1].titles)
    return list(chosen.values())


def titles_for(selection: Iterable[str]) -> set[str]:
    """Union of title keywords a selection of field / "field:subrole" / "field:*" keys expands to."""
    out: set[str] = set()
    for _fld, titles in _resolve(selection):
        out |= titles
    return out


@lru_cache(maxsize=4096)
def _phrase_re(needle: str) -> re.Pattern[str]:
    # Word-boundary match of a whole phrase: "threat intelligence" must appear as those adjacent words.
    return re.compile(rf"(?<![\w&/+-]){re.escape(needle)}(?![\w&/+-])")


def _contains(title: str, needle: str) -> bool:
    return bool(needle) and _phrase_re(needle).search(title) is not None


def match_title(title: str, selection: Iterable[str]) -> bool:
    """True when the job title contains one of the title keywords the selection expands to, matched on word
    boundaries, unless the matching field's exclude list vetoes the title (e.g. "Security Guard")."""
    t = " ".join((title or "").lower().split())
    if not t:
        return False
    for fld, titles in _resolve(selection):
        if any(_contains(t, ex) for ex in fld.exclude):
            continue
        if any(_contains(t, keyword) for keyword in titles):
            return True
    return False
