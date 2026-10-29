"""Deterministic fallbacks that need no LLM.

The pipeline must work end to end without any provider key: these functions score a posting by keyword
overlap with the profile and filters, answer the common closed form questions from the profile vault and
write a template cover letter. `FakeLLM` calls the same functions, so tests and the no-AI mode agree.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from jobapply.ai.base import ScoreResult
from jobapply.models import Job, User, utcnow
from jobapply.pipeline.filters import detect_seniority

# Tech words treated as "requirements" when they show up in a posting.
TECH = {
    "python", "go", "java", "kotlin", "rust", "typescript", "javascript", "react", "node", "django",
    "fastapi", "flask", "spring", "postgres", "postgresql", "mysql", "mongodb", "redis", "kafka", "rabbitmq",
    "aws", "gcp", "azure", "kubernetes", "docker", "terraform", "airflow", "spark", "sql", "graphql", "grpc",
    "linux", "ci/cd", "pytorch", "tensorflow", "llm", "ml", "machine learning",
}

SCORE_BASE = 30
COVERAGE_WEIGHT = 40  # share of the posting's requirements the profile covers
SKILL_BONUS = 3  # per matched skill, at most MAX_SKILL_BONUSES of them
MAX_SKILL_BONUSES = 5
ROLE_BONUS = 10
KEYWORD_BONUS = 4
SENIORITY_BONUS = 5
SENIORITY_PENALTY = 15
LOCATION_BONUS = 5
REMOTE_PENALTY = 10
EXCLUDE_PENALTY = 25
RECENT_DAY_BONUS = 5
RECENT_3DAY_BONUS = 2


def _profile_of(user: User | dict | None) -> dict[str, Any]:
    if user is None:
        return {}
    if isinstance(user, dict):
        return user
    return dict(user.profile or {})


def _filters_of(filters: User | dict | None) -> dict[str, Any]:
    if filters is None:
        return {}
    if isinstance(filters, dict):
        return filters
    return dict(filters.filters or {})


def _job_of(job: Job | dict) -> dict[str, Any]:
    if isinstance(job, dict):
        return job
    return {
        "title": job.title, "company": job.company, "location": job.location, "description": job.description,
        "remote": job.remote, "posted_at": job.posted_at, "first_seen_at": getattr(job, "first_seen_at", None),
    }


# "go" the language, not "go-to-market", "on the go" or "ready to go": the bare word only counts when it is not
# hyphenated and not part of an everyday phrase.
_GO_PHRASE = re.compile(
    r"\b(on the|ready to|able to|to|we|let'?s|lets|you|please|will|can|good to|way to|set to) go\b|"
    r"\bgo (to|for|live|ahead|through|beyond|above|the extra|out|on|further|faster|deep|above and beyond|big)\b"
)


def _term_in(term: str, text: str) -> bool:
    if not term:
        return False
    if term in ("go", "golang"):
        if re.search(r"\bgolang\b", text):
            return True
        return any(
            not _GO_PHRASE.search(text[max(0, m.start() - 12) : m.end() + 12])
            for m in re.finditer(r"(?<![\w+/-])go(?![\w+/-])", text)
        )
    return re.search(rf"(?<![\w+]){re.escape(term)}(?![\w+])", text) is not None


def _skill_names(profile: dict[str, Any]) -> dict[str, Any]:
    """Lowercased skill -> years (or None), blank names dropped. Only for matching; display uses the
    profile's own casing via `_skill_labels`."""
    skills = profile.get("skills") or {}
    if isinstance(skills, dict):
        return {str(k).strip().lower(): v for k, v in skills.items() if str(k).strip()}
    return {str(k).strip().lower(): None for k in skills if str(k).strip()}


def _skill_labels(profile: dict[str, Any]) -> list[str]:
    """Skill names as the user wrote them (`FastAPI`, `AWS`), blank names dropped."""
    skills = profile.get("skills") or {}
    return [str(k).strip() for k in skills if str(k).strip()]


# ------------------------------------------------------------------ scoring


def score_job_heuristic(user_profile: User | dict | None, filters: User | dict | None, job: Job | dict,
                        now: datetime | None = None) -> ScoreResult:
    """Keyword-overlap score, 0-100. Deterministic; used when no LLM answers and by `FakeLLM`."""
    profile, f, j = _profile_of(user_profile), _filters_of(filters), _job_of(job)
    title = (j.get("title") or "").lower()
    text = f"{title} {j.get('description') or ''}".lower()
    skills = _skill_names(profile)
    # Requirements: the tech vocabulary plus whatever the user lists as a skill.
    posting_terms = {t for t in TECH | set(skills) if _term_in(t, text)}
    matched = sorted(posting_terms & set(skills))
    missing = sorted(posting_terms - set(skills))
    coverage = len(matched) / len(posting_terms) if posting_terms else 0.0
    score = SCORE_BASE + COVERAGE_WEIGHT * coverage + SKILL_BONUS * min(len(matched), MAX_SKILL_BONUSES)
    notes: list[str] = []

    roles = [r.lower() for r in (f.get("roles") or profile.get("target_roles") or [])]
    if any(all(_term_in(w, title) for w in r.split()) for r in roles if r.strip()):
        score += ROLE_BONUS
        notes.append("title matches a target role")
    keywords = [k.lower() for k in (f.get("keywords_include") or []) if k]
    hits = [k for k in keywords if _term_in(k, text)]
    if hits:
        score += KEYWORD_BONUS * min(len(hits), 3)
    excluded = [k for k in (f.get("keywords_exclude") or []) if k and _term_in(k.lower(), title)]
    if excluded:
        score -= EXCLUDE_PENALTY
        notes.append(f"title contains excluded word {excluded[0]!r}")

    level = detect_seniority(title)
    allowed = [s.lower() for s in (f.get("seniority") or [])]
    if level and allowed:
        if level in allowed:
            score += SENIORITY_BONUS
        else:
            score -= SENIORITY_PENALTY
            notes.append(f"seniority {level} outside your range")

    location = (j.get("location") or "").lower()
    remote = bool(j.get("remote"))
    wants = [loc.lower() for loc in (f.get("locations") or []) if loc]
    if f.get("remote_only") and not remote:
        score -= REMOTE_PENALTY
        notes.append("not remote")
    elif wants:
        if (remote and "remote" in wants) or any(_term_in(loc, location) for loc in wants if loc != "remote"):
            score += LOCATION_BONUS
        else:
            score -= LOCATION_BONUS
            notes.append("location outside your list")

    when = j.get("posted_at") or j.get("first_seen_at")
    if isinstance(when, datetime):
        now = now or utcnow()
        if when.tzinfo is not None and now.tzinfo is None:
            when = when.replace(tzinfo=None)
        elif when.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        age = now - when
        if age <= timedelta(days=1):
            score += RECENT_DAY_BONUS
        elif age <= timedelta(days=3):
            score += RECENT_3DAY_BONUS

    score = max(0, min(100, int(score)))
    reason = (f"Matches {', '.join(matched)}" if matched else "Little overlap with the profile") + (
        f"; posting also wants {', '.join(missing)}" if missing else ""
    )
    if notes:
        reason += "; " + "; ".join(notes)
    return ScoreResult(score=score, reason=reason + ".", matched_skills=matched, missing_skills=missing)


# ------------------------------------------------------------------ form answers

_YES = {"yes", "y", "true", "1"}
_NO = {"no", "n", "false", "0"}


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    v = str(value or "").strip().lower()
    if v in _YES or v.startswith("yes"):
        return True
    if v in _NO or v.startswith("no"):
        return False
    return None


def _pick_option(options: list[str], answer: str) -> str | None:
    want = answer.strip().lower()
    for opt in options:
        if opt.strip().lower() == want:
            return opt
    as_bool = _as_bool(want)
    if as_bool is not None:
        for opt in options:
            if _as_bool(opt) is as_bool:
                return opt
    if len(want) > 2:
        for opt in options:  # "India" -> "India +91" before "British Indian Ocean Territory +246"
            if opt.strip().lower().startswith(want):
                return opt
    for opt in options:
        o = opt.strip().lower()
        if want and (want in o or o in want) and len(want) > 2:
            return opt
    return None


_NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
                 "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15}


def _years_asked(q: str) -> float | None:
    """The years a screening question asks for: "five (5) years", "3+ years", "two years"."""
    m = re.search(r"\((\d+(?:\.\d+)?)\)\s*\+?\s*years?|\b(\d+(?:\.\d+)?)\s*\+?\s*years?", q)
    if m:
        return float(m.group(1) or m.group(2))
    m = re.search(r"\b(" + "|".join(_NUMBER_WORDS) + r")\b\s*(?:\(\d+\))?\s*\+?\s*years?", q)
    return float(_NUMBER_WORDS[m.group(1)]) if m else None


def _as_number(value: str) -> float | None:
    m = re.search(r"\d+(?:\.\d+)?", value or "")
    return float(m.group(0)) if m else None


def _has_bool_options(options: list[str]) -> bool:
    return any(_as_bool(o) is not None for o in options)


def _country_of(profile: dict[str, Any]) -> str | None:
    """`country` when the profile has one, else the last comma segment of `location` ("Bengaluru, India")."""
    if profile.get("country"):
        return str(profile["country"])
    location = str(profile.get("location") or "")
    if "," in location:
        return location.rsplit(",", 1)[1].strip() or None
    return None


def _school_of(profile: dict[str, Any]) -> str | None:
    if profile.get("school"):
        return str(profile["school"])
    resume = profile.get("resume_json")
    edu = (resume.get("education") or []) if isinstance(resume, dict) else []
    for entry in edu:
        if isinstance(entry, dict) and entry.get("institution"):
            return str(entry["institution"])
    return None


_DEGREE_LEVELS: list[tuple[str, str]] = [
    (r"ph\.?\s?d|doctor(ate|al)?\b|d\.?phil", "doctor of philosophy"),
    (r"\bmba\b|m\.?b\.?a\b", "master of business"),
    (r"\bm\.?\s?(tech|e|sc|s|a|eng|com|ca)\b|master", "master"),
    (r"\bb\.?\s?(tech|e|sc|s|a|eng|com|ca)\b|bachelor|\bbtech\b|undergrad", "bachelor"),
    (r"associate|diploma", "associate"),
    (r"high school|secondary|12th|hsc", "high school"),
]


def _degree_of(profile: dict[str, Any], options: list[str] | None) -> str | None:
    """The profile's education as the form wants it: verbatim for a text box, the degree level ("bachelor")
    when a dropdown of levels has to be matched."""
    education = str(profile.get("education") or "").strip()
    if not education:
        return None
    if not options:
        return education
    for pattern, level in _DEGREE_LEVELS:
        if re.search(pattern, education.lower()):
            return level
    return education


def heuristic_answer(question: str, options: list[str] | None, vault: User | dict | None) -> tuple[str | None, int]:
    """Answer the common closed questions from the profile. `(None, 0)` when the profile cannot answer."""
    profile = _profile_of(vault)
    q = re.sub(r"\s+", " ", (question or "").lower())
    answer: str | None = None
    confidence = 0

    years_q = re.search(r"(how many )?years?\b.*?(experience|exp\b|working)|experience.*?years?", q)
    skill_years = False
    for skill, years in _skill_names(profile).items():
        if years_q and skill and _term_in(skill, q) and years not in (None, ""):
            answer, confidence = f"{float(years):g}" if isinstance(years, (int, float)) else str(years), 95
            skill_years = True
            break
    if answer is None:
        # Every pattern is anchored to the thing it answers: a bare "expected", "mobile" or "availability"
        # would also match "expected graduation date", "mobile development" or "availability for interviews",
        # and a wrong profile value filled at high confidence is submitted without review.
        rules: list[tuple[str, Any, int]] = [
            (r"notice period|how soon can you|when can you (start|join)|earliest (start|joining)|"
             r"(start|joining) date|availab(le|ility) to (start|join)|date of joining",
             profile.get("notice_period"), 85),
            (r"current (ctc|salary|compensation|pay|package)", profile.get("current_ctc"), 90),
            (r"(expected|desired|target|preferred) (annual |monthly |base )?(ctc|salary|compensation|pay|package)|"
             r"(salary|compensation|ctc|pay) expectation",
             profile.get("expected_ctc"), 85),
            # Before work authorization: "will you require X to sponsor you for a work permit" asks about
            # sponsorship, while "right to work without sponsorship" asks about authorization.
            (r"(require|need|needs|necessary)\b.{0,60}\bsponsor", profile.get("needs_sponsorship"), 90),
            (r"authori[sz]ed|legally (allowed|able|permitted)|right to work|work (permit|authori[sz]ation|eligibility)",
             profile.get("work_authorization"), 85),
            (r"sponsor", profile.get("needs_sponsorship"), 90),
            (r"relocat", profile.get("willing_to_relocate"), 90),
            (r"linkedin", profile.get("linkedin"), 95),
            (r"github", profile.get("github"), 95),
            (r"portfolio|personal website|website url|\bwebsite\b\s*\*?$", profile.get("portfolio"), 90),
            (r"(total|overall)? ?years? of (professional |work |total )?experience|how many years",
             profile.get("years_experience"), 85),
            (r"telephone|(phone|mobile|cell|contact) ?(number|no\.?|#)|^(phone|mobile|cell ?phone)\s*\*?$",
             profile.get("phone"), 95),
            (r"e-?mail (address|id)\b|^(your |work |personal |contact )?e-?mail\s*\*?\??$|"
             r"what('s| is) your e-?mail\s*\??$",
             profile.get("email"), 95),
            (r"current (or (most recent|previous|last) )?(company|employer|organi[sz]ation)",
             profile.get("current_company"), 95),
            (r"current (or (most recent|previous|last) )?(title|role|designation|position|job title)",
             profile.get("current_title"), 95),
            (r"country (of residence|where you (currently )?(reside|live))|current country|which country|"
             r"^country\s*\*?$",
             _country_of(profile), 80),
            (r"\b(city|current location|your location|where (do you|are you) (live|based))\b|^location\s*\*?$",
             profile.get("location"), 85),
            (r"^(school|university|college|institution)\b|alma mater|where did you study|name of (your )?"
             r"(school|university|college)",
             _school_of(profile), 80),
            (r"\bdegree\b|highest (level of )?education|level of education|education level",
             _degree_of(profile, options), 75),
            # Marketing / messaging opt-ins default to no, like the mapper skips them when optional.
            (r"opt[- ]?in|receive (sms|whatsapp|text|marketing) (messages|communications|updates|notifications)|"
             r"marketing (emails|communications)",
             "No", 60),
        ]
        for pattern, value, conf in rules:
            if value not in (None, "") and re.search(pattern, q):
                answer, confidence = str(value), conf
                break
    if options and years_q and answer is not None and _has_bool_options(options):
        # "Do you possess five (5) years of experience in X?" with Yes/No options: compare the years we have.
        need = _years_asked(q)
        have = _as_number(answer)
        if need is not None and have is not None:
            answer = "Yes" if have >= need else "No"
            # A skill's own years are a fair basis; the total years are a guess that a human should check.
            confidence = 70 if skill_years else 45
    authorization_q = re.search(r"authori[sz]|legally|eligib|right to work", q) is not None
    if answer is None and re.search(r"\b(are you|do you|have you|can you|will you|would you)\b", q) and options:
        # Yes/no questions the profile does not cover: the work-authorization family defaults to
        # "authorized unless sponsorship is needed", but only when the profile says so explicitly.
        if authorization_q:
            needs = _as_bool(profile.get("needs_sponsorship"))
            if needs is not None:
                answer, confidence = ("No" if needs else "Yes"), 70
    if answer is None:
        return None, 0
    if authorization_q and not _country_matches_profile(q, profile):
        # "authorized to work in the United States?" for an "Indian citizen" profile: a legal attestation the
        # profile cannot back. Leave it to the LLM or a human.
        return None, 0
    if options:
        if authorization_q and _as_bool(answer) is None:
            # "Indian citizen" style values: a yes/no option list wants a boolean, and only an explicit
            # `needs_sponsorship` can supply one.
            needs = _as_bool(profile.get("needs_sponsorship"))
            if needs is None:
                return None, 0
            answer = "No" if needs else "Yes"
            confidence = min(confidence, 70)
        picked = _pick_option(options, answer)
        if picked is None:
            return None, 0
        return picked, confidence
    return answer, confidence


_COUNTRY_Q = re.compile(
    r"\b(?:work|employ(?:ment|ed)?|job|position|role) (?:in|within|for) (?:the )?([a-z][a-z .'-]{1,40}?)"
    r"(?=[?.,;!]|\s(?:without|with|on|as|under|at)\b|$)"
)
_COUNTRY_ALIASES = {
    "usa": "united states", "u.s.": "united states", "u.s.a.": "united states", "us": "united states",
    "america": "united states", "uk": "united kingdom", "u.k.": "united kingdom", "britain": "united kingdom",
    "great britain": "united kingdom", "uae": "united arab emirates", "eu": "european union",
}


def _country_matches_profile(q: str, profile: dict[str, Any]) -> bool:
    """True when the question names no country, or the one it names shows up in the profile's location /
    work-authorization text (`india` in `Indian citizen`, `Bengaluru, India`)."""
    m = _COUNTRY_Q.search(q)
    if not m:
        return True
    country = m.group(1).strip().rstrip(".").strip()
    country = _COUNTRY_ALIASES.get(country, country)
    if re.match(r"(your|this|our|that) ", country) or country in ("country", "location"):
        return True  # "work in your current location?" names no country
    haystack = " ".join(
        str(profile.get(k) or "").lower() for k in ("location", "work_authorization", "country", "citizenship")
    )
    for alias, full in _COUNTRY_ALIASES.items():
        if full == country and re.search(rf"(?<![\w.]){re.escape(alias)}(?![\w.])", haystack):
            return True
    stem = country[:-1] if country.endswith("a") and len(country) > 4 else country  # india -> indian citizen
    return stem in haystack


# ------------------------------------------------------------------ cover letter


def cover_letter_template(user: User | dict | None, job: Job | dict, tone: str = "professional") -> str:
    """Three short paragraphs built only from profile facts."""
    p, j = _profile_of(user), _job_of(job)
    name = f"{p.get('first_name') or ''} {p.get('last_name') or ''}".strip() or "Candidate"
    skills = _skill_labels(p)  # the user's own casing: FastAPI, AWS, PostgreSQL
    top = ", ".join(s.title() if s.islower() else s for s in skills[:3]) or "my experience"
    greeting = "Hi" if tone == "casual" else "Dear"
    summary = (p.get("summary") or "").strip()
    years = p.get("years_experience")
    exp = f" with {years} years of experience" if years else ""
    return (
        f"{greeting} {j.get('company', '')} team,\n\n"
        f"I am applying for the {j.get('title', '')} role. {summary}\n\n"
        f"My work with {top} maps directly onto what the posting asks for{exp}, and I am used to owning "
        f"services end to end.\n\nI am available with a notice period of {p.get('notice_period', 'one month')} "
        f"and happy to talk whenever suits you.\n\nBest regards,\n{name}"
    ).replace("role. \n\n", "role.\n\n")
