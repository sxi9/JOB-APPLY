"""Naukri apply: one-click **Apply**, the post-apply **chatbot** questionnaire, **Apply on company site**
routing and the **profile refresh** (Naukri ranks recently-updated profiles higher).

Runs inside the user's persistent, logged-in Naukri profile (always headless on this machine). A Naukri
job page has one primary control:

- ``button#apply-button`` "Apply"                      one click submits the application (no review step);
                                                        a chatbot drawer may then ask questions one at a time.
- ``button#company-site-button`` "Apply on company site"  external: the result carries the resolved URL and
                                                        the detected ATS so the pipeline can hand it to the
                                                        ATS filler (like ``sites.linkedin.resolve``).
- ``#already-applied`` "Applied"                        nothing to do.
- "Login to apply" / "Register to apply"                the session is gone.

Because the Apply click *is* the submit, the safety gates all sit in front of it: ``apply_job`` clicks only
when ``dry_run`` is False **and** ``auto_submit`` is True; otherwise it stops on the job page, screenshots it
and reports ``dry_run``. A CAPTCHA / OTP / block page pauses with ``login_status="challenge"`` and is never
retried here. A chatbot question nobody can answer confidently stops the loop with ``needs_human`` (the
application itself may already be registered on Naukri: the reason says so).

Result vocabulary (``ApplyResult.status`` / the dict from ``apply_job``), mapped onto the pipeline's
``applied | dry_run | paused | failed`` by ``ApplyResult.to_fill_result``:

    applied      Apply clicked and Naukri confirmed it (chatbot finished or no chatbot)
    dry_run      nothing clicked (global dry run, or the site's auto_submit is off)
    external     "Apply on company site": ``apply_url`` / ``ats_type`` filled when resolvable
    needs_human  the chatbot asked something that needs the user (pipeline: paused)
    paused       session expired / challenge / no confirmation after the click (pipeline: paused)
    skipped      already applied
    failed       no apply control, navigation failure
"""

from __future__ import annotations

import html as html_lib
import random
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from jobapply.collectors.detector import detect_from_url
from jobapply.config import get_settings
from jobapply.forms.profiles import VaultView, resolve_key
from jobapply.forms.runner import FillResult, FormRunner
from jobapply.logs import get_logger
from jobapply.models import Application, AppStatus, Job, User, UserSite, utcnow
from jobapply.sites.linkedin.easy_apply import key_for_label as _linkedin_key_for_label
from jobapply.sites.linkedin.easy_apply import skill_years
from jobapply.sites.naukri.session import (
    PROFILE_URL,
    SITE,
    NaukriSession,
    classify_page,
    throttled,
)

log = get_logger(__name__)

PROFILE_ORIGIN = "builtin:naukri"
DEFAULT_COOLDOWN_HOURS = 6
MAX_QUESTIONS = 15
MIN_ANSWER_CONFIDENCE = 60
_RATE_LIMIT_STATUSES = (403, 406, 429)

STATUS_APPLIED = "applied"
STATUS_DRY_RUN = "dry_run"
STATUS_EXTERNAL = "external"
STATUS_NEEDS_HUMAN = "needs_human"
STATUS_PAUSED = "paused"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"
STATUSES = (STATUS_APPLIED, STATUS_DRY_RUN, STATUS_EXTERNAL, STATUS_NEEDS_HUMAN, STATUS_PAUSED, STATUS_SKIPPED,
            STATUS_FAILED)

# Naukri's React markup carries hashed class names (styles_apply-button__uJI3x); the ids are stable.
DEFAULT_SELECTORS: dict[str, str] = {
    "apply_button": "button#apply-button, button[class*='apply-button']",
    "external_button": "button#company-site-button, a#company-site-button, button[class*='company-site-button'], "
                       "a[class*='company-site-button']",
    "already_applied": "button#already-applied, button[class*='already-applied'], span[class*='already-applied']",
    "login_button": "a#login_Layer, #login_Layer, a[href*='nlogin/login']",
    "chatbot": "div.chatbot_Drawer, div[class*='chatbot_Drawer'], div[class*='chatbot_DrawerContentWrapper'], "
               "div[class*='chatbot_Container']",
    "bot_message": ".botMsg, [class*='botMsg'], .chatbot_MessageContainer .msg, [class*='botItem']",
    "chat_input": "div.textArea[contenteditable='true'], div[class*='textArea'][contenteditable='true'], "
                  "div[id^='userInput'], textarea[class*='chatbot'], div[class*='chatbot'] textarea, "
                  "div[class*='chatbot'] input[type='text']",
    "chat_send": "div.sendMsg, div[class*='sendMsg'], div[id^='sendMsg'], button.sendMsg, button[class*='sendMsg']",
    "chat_save": "div[class*='ssrc__btn-save'], button[class*='save'], div[class*='sendMsg']",
    "chat_radio": "input[type='radio']",
    "chat_checkbox": "input[type='checkbox']",
    "apply_success": "[class*='apply-message'], [class*='applied-message'], [class*='successfully-applied']",
    "captcha": "iframe[src*='captcha'], iframe[src*='recaptcha'], .g-recaptcha, #captcha",
    "profile_headline_edit": "#lazyResumeHead .edit, .resumeHeadline .edit, [data-ga-track*='Resume Headline'] .edit, "
                             "span.edit.icon",
    "profile_headline_textarea": "textarea#resumeHeadlineTxt, textarea[name='resumeHeadline']",
    "profile_save": "form button[type='submit'], button.btn-dark-ot, button:has-text('Save')",
    "profile_resume_input": "input#attachCV, input[type='file'][accept*='pdf'], input[type='file']",
    "profile_toast": ".toast-msg, [class*='toast'], [class*='success-msg']",
}

_SUCCESS_TEXT = re.compile(
    r"successfully applied|application (?:has been )?(?:sent|submitted)|applied successfully|"
    r"thank you for applying|you have applied", re.I)
_NEEDS_QUESTION = re.compile(r"\?|\b(please|enter|select|choose|share|mention|provide|what|which|how|are you|do you|"
                             r"have you|can you|will you|would you)\b", re.I)
_LOGIN_TEXT = re.compile(r"login to apply|register to apply|login and apply", re.I)
_EXTERNAL_TEXT = re.compile(r"company site|company website|apply on (?:the )?(?:company|employer)", re.I)
_APPLIED_TEXT = re.compile(r"^\s*applied\s*$", re.I)
_YES_NO = ("yes", "no")

# ------------------------------------------------------------------ pure parsing of the job page


_CONTROL = re.compile(r"<(button|a)\b([^>]*)>(.*?)</\1>", re.I | re.S)
_ID = re.compile(r'\bid="([^"]+)"', re.I)
_CLASS = re.compile(r'\bclass="([^"]+)"', re.I)
_HREF = re.compile(r'\bhref="([^"]+)"', re.I)
_TAGS = re.compile(r"<[^>]+>")


def _strip(text: str) -> str:
    return re.sub(r"\s+", " ", html_lib.unescape(_TAGS.sub(" ", text or ""))).strip()


def classify_apply_control(html: str) -> tuple[str, str | None]:
    """``(kind, url)`` from a saved job page: ``apply`` | ``external`` | ``applied`` | ``login`` | ``none``.

    ``url`` is the external target when the control is an ``<a href>`` (or carries ``data-url``); an external
    ``<button>`` whose URL only appears on click comes back ``("external", None)``.
    """
    html = html or ""
    for _tag, attrs, inner in _CONTROL.findall(html):
        ident = (m.group(1) if (m := _ID.search(attrs)) else "").lower()
        classes = (m.group(1) if (m := _CLASS.search(attrs)) else "").lower()
        text = _strip(inner)
        blob = f"{ident} {classes}"
        if "already-applied" in blob or (ident == "apply-button" and _APPLIED_TEXT.match(text)):
            return "applied", None
        if "company-site-button" in blob or (ident in ("apply-button", "") and _EXTERNAL_TEXT.search(text)
                                             and "apply" in blob + text.lower()):
            href = m.group(1) if (m := _HREF.search(attrs)) else None
            data = re.search(r'data-(?:url|href|apply-url)="([^"]+)"', attrs, re.I)
            url = html_lib.unescape(href or (data.group(1) if data else "") or "").strip()
            return "external", (url if url.startswith("http") else None)
        if ident == "apply-button" or "apply-button" in classes:
            if _LOGIN_TEXT.search(text):
                return "login", None
            if _EXTERNAL_TEXT.search(text):
                return "external", None
            return "apply", None
        if ident in ("login_layer", "register_layer") and _LOGIN_TEXT.search(text):
            return "login", None
    if _LOGIN_TEXT.search(_strip(html)):
        return "login", None
    return "none", None


# ------------------------------------------------------------------ result


@dataclass
class ApplyResult:
    url: str
    status: str = STATUS_FAILED
    reason: str = ""
    screenshot: str | None = None
    apply_url: str | None = None
    ats_type: str | None = None
    answers: dict[str, str] = field(default_factory=dict)
    questions: list[dict[str, Any]] = field(default_factory=list)
    needs_human: list[str] = field(default_factory=list)
    clicked_apply: bool = False
    submitted: bool = False
    login_status: str | None = None  # "login" | "challenge" when the session itself stopped the run

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_fill_result(self) -> FillResult:
        """The pipeline's ``FillResult`` so ``pipeline.apply.record_outcome`` can store the outcome."""
        r = FillResult(url=self.url, profile_origin=PROFILE_ORIGIN, screenshot_path=self.screenshot,
                       needs_human=list(self.needs_human))
        if self.status == STATUS_APPLIED:
            r.submitted = True
        elif self.status == STATUS_DRY_RUN:
            pass
        elif self.status in (STATUS_NEEDS_HUMAN, STATUS_PAUSED, STATUS_SKIPPED):
            r.paused_reason = self.reason or self.status
        elif self.status == STATUS_EXTERNAL:
            r.paused_reason = self.reason or "apply on company site"
        else:
            r.error = self.reason or "naukri apply failed"
        return r


# ------------------------------------------------------------------ question answering


QUESTIONS_FILE = Path(__file__).with_name("questions.yaml")


@dataclass
class QuestionMap:
    """``questions.yaml``: question substring -> vault key, plus selector overrides."""

    keys: list[tuple[re.Pattern[str], str]] = field(default_factory=list)
    selectors: dict[str, str] = field(default_factory=dict)


def load_question_map(path: Path | None = None) -> QuestionMap:
    """Read ``questions.yaml`` (utf-8). A missing or broken file yields an empty map (logged), never an error."""
    p = path or QUESTIONS_FILE
    out = QuestionMap()
    if not p.exists():
        return out
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - a broken yaml must not stop the run
        log.warning("naukri questions.yaml not loaded", path=str(p), error=str(exc)[:200])
        return out
    for needle, key in (data.get("questions") or {}).items():
        needle, key = str(needle).strip(), str(key or "").strip()
        if needle and key and key != "skip":
            out.keys.append((re.compile(re.escape(needle), re.I), key))
    for k, v in (data.get("selectors") or {}).items():
        if isinstance(v, str) and v.strip():
            out.selectors[str(k)] = v.strip()
    return out


def key_for_question(question: str, extra: list[tuple[re.Pattern[str], str]] | None = None) -> str | None:
    q = (question or "").strip()
    for rx, key in extra or []:
        if rx.search(q):
            return key
    return _linkedin_key_for_label(q)


def _as_text(value: Any) -> str:
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return "" if value is None else str(value)


class QuestionAnswerer:
    """Vault / skills first, then the answer source (bank, heuristics, LLM via ``forms.runner.AnswerSource``)."""

    def __init__(self, vault: VaultView, answers: Any = None, qmap: QuestionMap | None = None,
                 min_confidence: int = MIN_ANSWER_CONFIDENCE):
        self.vault = vault
        self.answers = answers
        self.extra_keys = list(qmap.keys) if qmap else []
        self.min_confidence = min_confidence

    def answer(self, question: str, options: list[str] | None = None) -> tuple[str | None, str, int]:
        """``(answer, source, confidence)``; answer None when nobody can answer it confidently."""
        profile = self.vault.profile or {}
        years = skill_years(question, profile.get("skills"))
        if years is not None:
            return self._fit(years, options, "builtin", 100)
        key = key_for_question(question, self.extra_keys)
        if key:
            value = _as_text(resolve_key(key, self.vault).value)
            if key == "derived.work_authorized" and not options:
                value = _as_text(profile.get("work_authorization"))
            if value:
                fitted = self._fit(value, options, "builtin", 100)
                if fitted[0] is not None:
                    return fitted
        if self.answers is not None and question:
            try:
                answer, conf = self.answers.answer(question, options)
            except Exception as exc:  # noqa: BLE001 - LLM outage: leave it for a human
                log.warning("naukri answer source failed", error=str(exc)[:160])
                answer, conf = None, 0
            if answer not in (None, "") and conf >= self.min_confidence:
                return self._fit(str(answer), options, "ai", int(conf))
        return None, "none", 0

    @staticmethod
    def _fit(value: str, options: list[str] | None, source: str, conf: int) -> tuple[str | None, str, int]:
        if not options:
            return value, source, conf
        pick = FormRunner._pick_option(options, value)
        return (pick, source, conf) if pick is not None else (None, source, 0)


# ------------------------------------------------------------------ the runner

_CHAT_STATE_JS = """
(sel) => {
  const vis = (e) => !!e && e.getClientRects().length > 0;
  const drawer = Array.from(document.querySelectorAll(sel.chatbot)).find(vis);
  if (!drawer) return null;
  const msgs = Array.from(drawer.querySelectorAll(sel.bot_message)).filter(vis);
  const last = msgs.length ? msgs[msgs.length - 1] : null;
  const labelOf = (el) => {
    const l = (el.id && drawer.querySelector('label[for="' + el.id + '"]')) || el.closest('label');
    const t = l ? l.innerText : (el.getAttribute('aria-label') || el.value || '');
    return (t || '').trim();
  };
  const radios = Array.from(drawer.querySelectorAll(sel.chat_radio)).filter(vis);
  const checks = Array.from(drawer.querySelectorAll(sel.chat_checkbox)).filter(vis);
  const input = Array.from(drawer.querySelectorAll(sel.chat_input)).find(vis);
  const send = Array.from(drawer.querySelectorAll(sel.chat_send)).find(vis);
  return {
    question: last ? last.innerText.trim() : '',
    options: radios.map(labelOf).filter(Boolean),
    multi: checks.map(labelOf).filter(Boolean),
    has_input: !!input,
    has_send: !!send,
    text: drawer.innerText.slice(0, 4000),
  };
}
"""

PauseFn = Any


def _human_pause(page: Any, lo_ms: int, hi_ms: int) -> None:
    from jobapply.sites.browser import human_pause

    human_pause(page, lo_ms, hi_ms)


class NaukriApplyRunner:
    """Drives one job page. ``run(page, job_url, submit=..., screenshot_path=...)`` -> ``ApplyResult``."""

    def __init__(self, vault: VaultView, answers: Any = None, *, selectors: dict[str, str] | None = None,
                 qmap: QuestionMap | None = None, min_answer_confidence: int = MIN_ANSWER_CONFIDENCE,
                 max_questions: int = MAX_QUESTIONS, pause: PauseFn | None = None):
        self.sel = {**DEFAULT_SELECTORS, **(qmap.selectors if qmap else {}), **(selectors or {})}
        self.answerer = QuestionAnswerer(vault, answers, qmap, min_answer_confidence)
        self.max_questions = max_questions
        self._pause = pause or _human_pause

    # -- page helpers

    def _visible(self, page: Any, key: str):
        selector = self.sel.get(key, "")
        if not selector:
            return None
        try:
            loc = page.locator(selector)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception as exc:  # noqa: BLE001 - detached nodes mid-transition
            log.debug("locator failed", key=key, error=str(exc)[:120])
        return None

    @staticmethod
    def _text_of(loc: Any) -> str:
        try:
            return (loc.inner_text() or "").strip()
        except Exception:  # noqa: BLE001
            return ""

    def _page_text(self, page: Any) -> str:
        try:
            return page.evaluate("() => (document.body && document.body.innerText || '').slice(0, 20000)") or ""
        except Exception:  # noqa: BLE001
            return ""

    def _screenshot(self, page: Any, path: Path | None, result: ApplyResult) -> None:
        if not path or result.screenshot:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(path), full_page=True)
            result.screenshot = str(path)
        except Exception as exc:  # noqa: BLE001
            log.warning("screenshot failed", error=str(exc)[:200])

    def _challenge(self, page: Any) -> str:
        if self._visible(page, "captcha") is not None:
            return "captcha on page"
        state = classify_page(page)
        if state == "challenge":
            return "security check / captcha page"
        return ""

    def _wait_for_control(self, page: Any, timeout_ms: int = 15_000) -> None:
        sels = ", ".join(self.sel[k] for k in ("apply_button", "external_button", "already_applied", "login_button"))
        try:
            page.locator(sels).first.wait_for(state="visible", timeout=timeout_ms)
        except Exception:  # noqa: BLE001 - classified from whatever rendered
            pass

    def _control(self, page: Any) -> tuple[str, Any]:
        """``(kind, locator)`` for the live page: applied | external | apply | login | none."""
        if (loc := self._visible(page, "already_applied")) is not None:
            return "applied", loc
        if (loc := self._visible(page, "external_button")) is not None:
            return "external", loc
        if (loc := self._visible(page, "apply_button")) is not None:
            text = self._text_of(loc)
            if _LOGIN_TEXT.search(text):
                return "login", loc
            if _EXTERNAL_TEXT.search(text):
                return "external", loc
            if _APPLIED_TEXT.match(text):
                return "applied", loc
            return "apply", loc
        if (loc := self._visible(page, "login_button")) is not None:
            return "login", loc
        return "none", None

    # -- external

    def _external_url(self, page: Any, loc: Any, allow_click: bool) -> str | None:
        """The company-site URL: an ``href`` / data attribute first; a click (popup capture) only when allowed."""
        for attr in ("href", "data-url", "data-href", "data-apply-url"):
            try:
                value = loc.get_attribute(attr)
            except Exception:  # noqa: BLE001
                value = None
            if value and str(value).startswith("http"):
                return str(value)
        if not allow_click:
            return None
        try:
            with page.context.expect_page(timeout=15_000) as popup_info:
                loc.click(timeout=10_000)
            popup = popup_info.value
            try:
                popup.wait_for_load_state("domcontentloaded", timeout=15_000)
            except Exception:  # noqa: BLE001
                pass
            url = popup.url or ""
            try:
                popup.close()
            except Exception:  # noqa: BLE001
                pass
            return url if url and url != "about:blank" else None
        except Exception as exc:  # noqa: BLE001 - no popup: same-tab navigation?
            log.debug("no popup from the company-site button", error=str(exc)[:160])
            try:
                url = page.url
                return url if url.startswith("http") and "naukri.com" not in url else None
            finally:
                pass

    # -- chatbot

    def _chat_state(self, page: Any) -> dict[str, Any] | None:
        try:
            return page.evaluate(_CHAT_STATE_JS, self.sel)
        except Exception as exc:  # noqa: BLE001
            log.debug("chat state failed", error=str(exc)[:120])
            return None

    def _confirmed(self, page: Any, chat_text: str = "") -> bool:
        if self._visible(page, "apply_success") is not None or self._visible(page, "already_applied") is not None:
            return True
        return bool(_SUCCESS_TEXT.search(chat_text or "") or _SUCCESS_TEXT.search(self._page_text(page)))

    def _drawer(self, page: Any):
        return self._visible(page, "chatbot")

    def _submit_answer(self, page: Any, state: dict[str, Any], answer: str) -> bool:
        drawer = self._drawer(page)
        if drawer is None:
            return False
        if state.get("options") or state.get("multi"):
            kind = "chat_radio" if state.get("options") else "chat_checkbox"
            inputs = drawer.locator(self.sel[kind])
            hit = None
            for i in range(inputs.count()):
                el = inputs.nth(i)
                try:
                    ident = el.get_attribute("id") or ""
                    label = drawer.locator(f"label[for='{ident}']").first if ident else None
                    text = self._text_of(label) if label is not None and label.count() else ""
                    text = text or (el.get_attribute("aria-label") or el.get_attribute("value") or "")
                except Exception:  # noqa: BLE001
                    text = ""
                if text.strip().lower() == answer.strip().lower():
                    hit = (el, label if label is not None and label.count() else None)
                    break
            if hit is None:
                return False
            el, label = hit
            try:
                (label or el).click(timeout=5000)
            except Exception:  # noqa: BLE001
                el.check(timeout=5000)
            self._pause(page, 300, 700)
            save = self._visible(page, "chat_save") or self._visible(page, "chat_send")
            if save is not None:
                save.click(timeout=5000)
            return True
        if state.get("has_input"):
            box = drawer.locator(self.sel["chat_input"]).first
            try:
                box.click(timeout=5000)
                box.fill(answer, timeout=5000)
            except Exception:  # noqa: BLE001 - contenteditable that rejects fill: type it
                box.click(timeout=5000)
                page.keyboard.type(answer)
            self._pause(page, 300, 700)
            send = self._visible(page, "chat_send")
            if send is not None:
                send.click(timeout=5000)
            else:
                page.keyboard.press("Enter")
            return True
        return False

    def _chatbot(self, page: Any, result: ApplyResult, screenshot_path: Path | None) -> None:
        """Answer the questionnaire one question at a time. Sets result.status (applied / needs_human / paused)."""
        previous = None
        repeats = 0
        for _ in range(self.max_questions):
            blocked = self._challenge(page)
            if blocked:
                result.status, result.reason, result.login_status = STATUS_PAUSED, blocked, "challenge"
                self._screenshot(page, screenshot_path, result)
                return
            state = self._chat_state(page)
            if state is None:
                result.status = STATUS_APPLIED if self._confirmed(page) else STATUS_PAUSED
                if result.status == STATUS_PAUSED:
                    result.reason = "apply clicked, chatbot closed without a confirmation: verify on Naukri"
                return
            if self._confirmed(page, state.get("text", "")):
                result.status = STATUS_APPLIED
                return
            question = (state.get("question") or "").strip()
            options = state.get("options") or state.get("multi") or None
            askable = bool(question) and (state.get("has_input") or bool(options))
            if not askable:
                # greeting / typing indicator: give the next message a moment
                self._pause(page, 1200, 2200)
                sig = (question, tuple(options or ()))
                repeats = repeats + 1 if sig == previous else 0
                previous = sig
                if repeats >= 2:
                    result.status = STATUS_APPLIED if self._confirmed(page, state.get("text", "")) else STATUS_PAUSED
                    if result.status == STATUS_PAUSED:
                        result.reason = "apply clicked, chatbot shows no question and no confirmation: verify on Naukri"
                        self._screenshot(page, screenshot_path, result)
                    return
                continue
            sig = (question, tuple(options or ()))
            if sig == previous:
                repeats += 1
                if repeats >= 2:
                    result.status = STATUS_NEEDS_HUMAN
                    result.reason = f"chatbot did not accept the answer to: {question[:120]}"
                    result.needs_human.append(question[:120])
                    self._screenshot(page, screenshot_path, result)
                    return
            else:
                repeats = 0
            previous = sig
            answer, source, conf = self.answerer.answer(question, options)
            result.questions.append({"question": question, "options": options or [], "answer": answer,
                                     "source": source, "confidence": conf})
            if answer is None:
                result.status = STATUS_NEEDS_HUMAN
                result.reason = (f"chatbot question needs you: {question[:120]} "
                                 "(the Apply click is already registered on Naukri; finish the chat by hand)")
                result.needs_human.append(question[:120])
                self._screenshot(page, screenshot_path, result)
                log.info("naukri chatbot question left for a human", question=question[:120])
                return
            if not self._submit_answer(page, state, answer):
                result.status = STATUS_NEEDS_HUMAN
                result.reason = f"could not enter the answer to: {question[:120]}"
                result.needs_human.append(question[:120])
                self._screenshot(page, screenshot_path, result)
                return
            result.answers[question[:120]] = answer
            self._pause(page, 900, 1800)
        result.status = STATUS_NEEDS_HUMAN
        result.reason = f"more than {self.max_questions} chatbot questions"
        self._screenshot(page, screenshot_path, result)

    # -- entry point

    def run(self, page: Any, job_url: str, submit: bool = False, screenshot_path: Path | None = None,
            dry_reason: str = "dry run") -> ApplyResult:
        result = ApplyResult(url=job_url)
        try:
            resp = page.goto(job_url, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001 - timeout / navigation failure
            result.status, result.reason = STATUS_FAILED, f"job page did not load: {str(exc).splitlines()[0][:160]}"
            return result
        status = getattr(resp, "status", None)
        if status in _RATE_LIMIT_STATUSES:
            result.status, result.reason, result.login_status = STATUS_PAUSED, f"naukri answered {status}", "challenge"
            return result
        self._pause(page, 600, 1400)
        blocked = self._challenge(page)
        if blocked:
            result.status, result.reason, result.login_status = STATUS_PAUSED, blocked, "challenge"
            self._screenshot(page, screenshot_path, result)
            return result
        self._wait_for_control(page)
        if classify_page(page) == "login":
            result.status, result.reason, result.login_status = STATUS_PAUSED, "naukri login required", "login"
            self._screenshot(page, screenshot_path, result)
            return result
        kind, control = self._control(page)
        if kind == "login":
            result.status, result.reason, result.login_status = STATUS_PAUSED, "naukri login required", "login"
            self._screenshot(page, screenshot_path, result)
            return result
        if kind == "applied":
            result.status, result.reason = STATUS_SKIPPED, "already applied on Naukri"
            return result
        if kind == "none":
            result.status, result.reason = STATUS_FAILED, "no Apply control on the job page (closed or expired?)"
            self._screenshot(page, screenshot_path, result)
            return result
        if kind == "external":
            url = self._external_url(page, control, allow_click=submit)
            result.status = STATUS_EXTERNAL
            result.apply_url = url
            hit = detect_from_url(url) if url else None
            result.ats_type = hit.ats if hit else None
            result.reason = ("apply on company site" + (f": {url}" if url else
                                                        " (URL is revealed only by clicking; not clicked)"))
            self._screenshot(page, screenshot_path, result)
            return result
        # kind == "apply": the click submits, so every gate stops here.
        if not submit:
            result.status, result.reason = STATUS_DRY_RUN, f"{dry_reason}: Apply not clicked"
            self._screenshot(page, screenshot_path, result)
            return result
        try:
            control.click(timeout=10_000)
        except Exception as exc:  # noqa: BLE001
            result.status, result.reason = STATUS_FAILED, f"Apply click failed: {str(exc).splitlines()[0][:160]}"
            return result
        result.clicked_apply = True
        self._pause(page, 1500, 2500)
        blocked = self._challenge(page)
        if blocked:
            result.status, result.reason, result.login_status = STATUS_PAUSED, blocked, "challenge"
            self._screenshot(page, screenshot_path, result)
            return result
        if self._drawer(page) is not None:
            self._chatbot(page, result, screenshot_path)
        elif self._confirmed(page):
            result.status = STATUS_APPLIED
        else:
            # Give a slow confirmation / late chatbot one more chance.
            self._pause(page, 1500, 2500)
            if self._drawer(page) is not None:
                self._chatbot(page, result, screenshot_path)
            elif self._confirmed(page):
                result.status = STATUS_APPLIED
            else:
                result.status = STATUS_PAUSED
                result.reason = "Apply clicked but no confirmation seen: verify on Naukri"
        if result.status == STATUS_APPLIED:
            result.submitted = True
            result.reason = result.reason or "applied on Naukri"
        self._screenshot(page, screenshot_path, result)
        return result


# ------------------------------------------------------------------ uniform entry point


def _job_of(db: Any, application: Application) -> Job:
    job = db.get(Job, application.job_id)
    if job is None:
        raise ValueError(f"application {application.id} has no job")
    return job


def apply_job(session: NaukriSession, application: Application, *, dry_run: bool, auto_submit: bool,
              llm: Any = None, answers: Any = None, screenshot_path: Path | None = None,
              selectors: dict[str, str] | None = None, pause: PauseFn | None = None) -> dict[str, Any]:
    """Apply to one application's job inside ``session`` (a ``NaukriSession``: page + user + db).

    Never clicks Apply when ``dry_run`` is True or ``auto_submit`` is False (the click is the submit on
    Naukri). Returns ``{"status", "screenshot", "reason", "apply_url", "ats_type", ...}`` (see the module
    docstring for the vocabulary). The caller owns the kill switch / cap / active-hours checks
    (``apply_naukri_user`` does them).
    """
    from jobapply.forms.runner import AnswerSource, vault_view_for

    job = _job_of(session.db, application)
    vault = vault_view_for(session.user, application)
    answers = answers if answers is not None else AnswerSource(session.db, llm, session.user, job,
                                                               message=application.message or "")
    runner = NaukriApplyRunner(vault, answers, selectors=selectors, qmap=load_question_map(), pause=pause)
    if screenshot_path is None:
        s = get_settings()
        screenshot_path = s.screenshots_dir / str(session.user.id) / (
            f"{application.id}-{utcnow().strftime('%Y%m%d%H%M%S')}.png")
    submit = (not dry_run) and bool(auto_submit)
    dry_reason = "global dry run" if dry_run else "auto_submit off for naukri"
    result = runner.run(session.page, job.apply_url or job.url, submit=submit, screenshot_path=screenshot_path,
                        dry_reason=dry_reason)
    log.info("naukri apply", user=session.user.username, job=job.id, status=result.status, reason=result.reason,
             submit=submit)
    return result.as_dict()


# ------------------------------------------------------------------ profile refresh


def refresh_profile(user_id: int, dry_run: bool = True, session_db: Any = None, *, page: Any = None,
                    resume_path: str | None = None, screenshot_path: Path | None = None,
                    selectors: dict[str, str] | None = None, pause: PauseFn | None = None) -> dict[str, Any]:
    """Open the profile page and re-save the resume headline (and re-upload the resume when ``resume_path`` is
    given) so Naukri's "last updated" moves. ``dry_run`` only navigates and screenshots.

    Returns ``{"status": "dry_run" | "refreshed" | "paused" | "failed" | "skipped", "screenshot", "reason", ...}``.

    This path mutates the account (headline save, resume upload), so it sits behind the same switches as an
    apply run: the kill switch skips it and the global dry-run switch downgrades it to a dry run. The switches
    are read from ``session_db`` (or a fresh DB session when none is given).
    """
    from jobapply.switches import global_dry_run, kill_switch_on

    sel = {**DEFAULT_SELECTORS, **(selectors or {})}
    pause_fn = pause or _human_pause
    out: dict[str, Any] = {"status": STATUS_FAILED, "screenshot": None, "reason": "", "headline_saved": False,
                           "resume_uploaded": False, "login_status": None}
    if session_db is not None:
        killed, forced_dry = kill_switch_on(session_db), global_dry_run(session_db)
    else:
        from jobapply.db import session_scope

        with session_scope() as db:
            killed, forced_dry = kill_switch_on(db), global_dry_run(db)
    if killed:
        log.warning("kill switch on, not refreshing the naukri profile", user_id=int(user_id))
        return {**out, "status": STATUS_SKIPPED, "reason": "kill switch on"}
    if forced_dry and not dry_run:
        log.info("global dry-run switch on: naukri profile refresh runs as a dry run", user_id=int(user_id))
        dry_run = True
    if screenshot_path is None:
        screenshot_path = get_settings().screenshots_dir / str(int(user_id)) / (
            f"profile-{utcnow().strftime('%Y%m%d%H%M%S')}.png")

    def _shot(pg: Any) -> None:
        try:
            Path(screenshot_path).parent.mkdir(parents=True, exist_ok=True)
            pg.screenshot(path=str(screenshot_path), full_page=True)
            out["screenshot"] = str(screenshot_path)
        except Exception as exc:  # noqa: BLE001
            log.warning("screenshot failed", error=str(exc)[:200])

    def _run(pg: Any) -> dict[str, Any]:
        try:
            pg.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=60_000)
        except Exception as exc:  # noqa: BLE001
            out["reason"] = f"profile page did not load: {str(exc).splitlines()[0][:160]}"
            return out
        pause_fn(pg, 800, 1600)
        state = classify_page(pg)
        if state in ("login", "challenge"):
            out.update(status=STATUS_PAUSED, login_status=state,
                       reason="naukri login required" if state == "login" else "security check / captcha page")
            _shot(pg)
            return out
        if dry_run:
            out.update(status=STATUS_DRY_RUN, reason="dry run: profile page opened, nothing saved")
            _shot(pg)
            return out
        runner = NaukriApplyRunner(VaultView(profile={}), None, selectors=sel, pause=pause_fn)
        edit = runner._visible(pg, "profile_headline_edit")
        if edit is None:
            out["reason"] = "resume headline editor not found"
            _shot(pg)
            return out
        edit.click(timeout=10_000)
        pause_fn(pg, 500, 1000)
        box = runner._visible(pg, "profile_headline_textarea")
        if box is None:
            out["reason"] = "resume headline textarea did not open"
            _shot(pg)
            return out
        try:
            current = box.input_value() or ""
        except Exception:  # noqa: BLE001
            current = ""
        if current.strip():
            box.fill(current)  # same text: the save still bumps the profile's modified date
        save = runner._visible(pg, "profile_save")
        if save is None:
            out["reason"] = "save button not found"
            _shot(pg)
            return out
        save.click(timeout=10_000)
        pause_fn(pg, 1200, 2200)
        out["headline_saved"] = runner._visible(pg, "profile_headline_textarea") is None or (
            runner._visible(pg, "profile_toast") is not None)
        if resume_path and Path(resume_path).exists():
            inp = pg.locator(sel["profile_resume_input"]).first
            try:
                if inp.count():
                    inp.set_input_files(str(resume_path))
                    pause_fn(pg, 2000, 3500)
                    out["resume_uploaded"] = True
            except Exception as exc:  # noqa: BLE001
                log.warning("naukri resume re-upload failed", error=str(exc)[:200])
        out["status"] = "refreshed" if out["headline_saved"] or out["resume_uploaded"] else STATUS_FAILED
        out["reason"] = ("profile re-saved" if out["status"] == "refreshed"
                         else "save clicked but the editor did not close and no toast appeared")
        _shot(pg)
        return out

    if page is not None:
        return _run(page)
    from playwright.sync_api import sync_playwright

    from jobapply.sites.browser import launch_persistent

    with sync_playwright() as pw:
        context = launch_persistent(pw, int(user_id), SITE, headless=True)
        try:
            return _run(context.new_page())
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass


# ------------------------------------------------------------------ pipeline integration (gated)


def pick_naukri_applications(db: Any, user: User, limit: int) -> list[tuple[Application, Job]]:
    """Approved then queued applications whose job routes to Naukri (``site_for_job(job) == "naukri"``)."""
    from jobapply.pipeline.apply import APPLY_STATUSES, site_for_job
    from jobapply.repo import UserScope

    scope = UserScope(db, user)
    rows = scope.applications_with_jobs(Application.status.in_(APPLY_STATUSES), limit=500)
    rows.sort(key=lambda r: (r[0].status != AppStatus.approved, -(r[0].score or 0)))
    out: list[tuple[Application, Job]] = []
    for app, job in rows:
        if site_for_job(job) != SITE or not (job.apply_url or job.url):
            continue
        out.append((app, job))
        if len(out) >= limit:
            break
    return out


def _notify(db: Any, user: User, kind: str, message: str) -> None:
    import jobapply.notify as notify_mod

    try:
        notify_mod.notify(db, user, kind, message)
    except Exception as exc:  # noqa: BLE001
        log.warning("notification failed", user=user.username, kind=kind, error=str(exc)[:200])


def _route_external(db: Any, user: User, app: Application, job: Job, result: dict[str, Any]) -> Any:
    """Write the resolved company-site URL / ATS onto the job so the ATS filler takes it next run; pause the
    application for a human when the host is not a known ATS or the URL could not be read."""
    from jobapply.pipeline.apply import record_outcome
    from jobapply.repo import UserScope

    url, ats = result.get("apply_url"), result.get("ats_type")
    if url:
        job.apply_url = url
        job.ats_type = ats
        db.add(job)
    if url and ats and UserScope(db, user).site(ats) is not None:
        UserScope(db, user).set_status(app, app.status, f"naukri: apply on company site -> {ats} ({url}); "
                                                        "handed to the ATS filler")
        log.info("naukri job routed to the ats filler", user=user.username, job=job.id, ats=ats)
        return None
    reason = (f"apply on company site manually: {url}" if url
              else "apply on company site: URL only revealed by clicking (not clicked)")
    fill = FillResult(url=job.apply_url, profile_origin=PROFILE_ORIGIN, paused_reason=reason,
                      screenshot_path=result.get("screenshot"))
    return record_outcome(db, user, app, job, fill)


def apply_naukri_user(pw: Any, db: Any, user: User, llm: Any = None, limit: int = 3, dry_run: bool = True, *,
                      context: Any = None, page: Any = None, between: Any = None,
                      selectors: dict[str, str] | None = None, pause: PauseFn | None = None) -> list[Any]:
    """One user's Naukri run inside their persistent (headless) profile. Returns ``ApplyOutcome`` rows.

    Gates, in order: kill switch; global dry-run switch (forces dry run); site enabled; cooldown after a
    challenge; session check (login / challenge -> alert, stop); per-user and per-site daily caps; the site's
    ``auto_submit`` flag (off -> the run stops before the Apply click and screenshots). A challenge or logout
    seen mid-run ends the run for the user and is never retried here.
    """
    from jobapply.pipeline.apply import applied_today, record_outcome
    from jobapply.repo import UserScope
    from jobapply.switches import global_dry_run, kill_switch_on

    if kill_switch_on(db):
        log.warning("kill switch on, not applying on naukri", user=user.username)
        return []
    dry_run = dry_run or global_dry_run(db)
    scope = UserScope(db, user)
    site: UserSite | None = scope.site(SITE)
    if site is None or not site.enabled:
        log.info("naukri disabled for user", user=user.username)
        return []
    if throttled(user, site):
        log.info("naukri throttled (cooldown after a challenge), skipping", user=user.username)
        return []
    picks = pick_naukri_applications(db, user, limit)
    if not picks:
        return []
    outcomes: list[Any] = []
    own_context = context is None and page is None
    if own_context:
        from jobapply.sites.browser import launch_persistent

        context = launch_persistent(pw, user.id, SITE, headless=True)
    nk = NaukriSession(context, user, db, page=page)
    try:
        status = nk.check()
        if status == "unknown":
            log.warning("naukri session state unclear, retry later", user=user.username)
            return []
        if status != "ok":
            if status == "challenge":
                _notify(db, user, "paused", "Naukri is showing a security check / OTP: log in in your browser, "
                                            f"re-export the cookies and import them for {user.username}")
            else:
                _notify(db, user, "paused", f"Naukri login needed ({status}): export naukri.com cookies and import "
                                            f"them for {user.username}")
            log.warning("naukri session not ok", user=user.username, status=status)
            return []
        s = get_settings()
        cap = int((user.preferences or {}).get("daily_cap", 40))
        done_today = applied_today(db, user.id)
        random.shuffle(picks)
        for i, (app, job) in enumerate(picks):
            if not dry_run and (done_today >= cap or applied_today(db, user.id, SITE) >= site.daily_cap):
                log.info("daily cap reached", user=user.username, site=SITE)
                break
            if i:
                (between or (lambda pg: _human_pause(pg, 8000, 20000)))(nk.page)
            shot = s.screenshots_dir / str(user.id) / f"{app.id}-{time.strftime('%Y%m%d%H%M%S')}.png"
            try:
                result = apply_job(nk, app, dry_run=dry_run, auto_submit=bool(site.auto_submit), llm=llm,
                                   screenshot_path=shot, selectors=selectors, pause=pause)
            except Exception as exc:  # noqa: BLE001 - navigation / browser failures
                outcome = record_outcome(db, user, app, job, None, error=f"{type(exc).__name__}: {exc}")
                db.commit()
                outcomes.append(outcome)
                continue
            if result["status"] == STATUS_EXTERNAL:
                outcome = _route_external(db, user, app, job, result)
                db.commit()
                if outcome is not None:
                    outcomes.append(outcome)
                continue
            fill = ApplyResult(**result).to_fill_result()
            outcome = record_outcome(db, user, app, job, fill)
            db.commit()
            outcomes.append(outcome)
            if outcome.status == AppStatus.applied:
                done_today += 1
            log.info("naukri apply outcome", user=user.username, job=job.id, status=outcome.status.value,
                     detail=outcome.detail)
            if result.get("login_status") == "challenge":
                nk.record_challenge()
                _notify(db, user, "paused", f"Naukri blocked or challenged the apply run ({result['reason']}); "
                                            f"paused for {DEFAULT_COOLDOWN_HOURS} h.")
                log.warning("naukri challenge mid-run, stopping", user=user.username, reason=result["reason"])
                break
            if result.get("login_status") == "login":
                nk.record_login_required()
                _notify(db, user, "paused", f"Naukri session expired mid-run: re-import cookies for {user.username}")
                log.warning("naukri session expired mid-run, stopping", user=user.username)
                break
    finally:
        if own_context:
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass
    return outcomes


def apply_naukri(username: str | None = None, limit: int = 3, dry_run: bool = True, engine: Any = None) -> list[dict]:
    """Entry point for a CLI / scheduler task: Naukri apply for every active user (or one), within the active
    hours (``JOBAPPLY_NAUKRI_ACTIVE_HOURS`` when the settings grow it, else the LinkedIn window), caps and
    switches."""
    from playwright.sync_api import sync_playwright
    from sqlmodel import select

    from jobapply.ai.base import LLMError
    from jobapply.ai.service import get_llm
    from jobapply.db import session_scope
    from jobapply.sites.browser import is_active_hours
    from jobapply.switches import global_dry_run, kill_switch_on

    settings = get_settings()
    hours = getattr(settings, "naukri_active_hours", None) or settings.linkedin_active_hours
    if not is_active_hours(spec=hours):
        log.info("outside naukri active hours, nothing done", active_hours=hours)
        return []
    results: list[dict] = []
    with session_scope(engine) as db:
        if kill_switch_on(db):
            log.warning("kill switch on, not applying on naukri")
            return []
        dry_run = dry_run or global_dry_run(db)
        stmt = select(User).where(User.is_active)
        if username:
            stmt = stmt.where(User.username == username)
        users = db.exec(stmt).all()
        with sync_playwright() as pw:
            for user in users:
                try:
                    llm = get_llm(db, user)
                except LLMError as exc:
                    log.warning("no llm, chatbot questions may be left for review", user=user.username,
                                error=str(exc))
                    llm = None
                try:
                    outcomes = apply_naukri_user(pw, db, user, llm, limit=limit, dry_run=dry_run)
                except Exception as exc:  # noqa: BLE001 - one user's browser trouble must not stop the others
                    log.exception("naukri run failed", user=user.username, error=str(exc)[:200])
                    continue
                results.extend({"user": user.username, **o.__dict__} for o in outcomes)
    return results


__all__ = [
    "ApplyResult", "NaukriApplyRunner", "QuestionAnswerer", "apply_job", "refresh_profile", "apply_naukri_user",
    "apply_naukri", "pick_naukri_applications", "classify_apply_control", "key_for_question", "load_question_map",
    "QuestionMap", "QUESTIONS_FILE",
    "DEFAULT_SELECTORS", "STATUSES", "STATUS_APPLIED", "STATUS_DRY_RUN", "STATUS_EXTERNAL", "STATUS_NEEDS_HUMAN",
    "STATUS_PAUSED", "STATUS_SKIPPED", "STATUS_FAILED", "PROFILE_ORIGIN",
]
