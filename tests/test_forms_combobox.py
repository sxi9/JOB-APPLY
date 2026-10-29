"""Custom dropdowns (React-Select style comboboxes, places autocompletes) and the form-location hops."""

from pathlib import Path

import pytest

from jobapply.ai.fake import FakeLLM
from jobapply.forms.fields import extract_fields
from jobapply.forms.profiles import VaultView
from jobapply.forms.runner import AnswerSource, FormRunner
from jobapply.seed import DEMO_PROFILE
from tests.test_forms import PROFILES, vault

FIXTURES = Path(__file__).parent / "fixtures" / "forms"
COMBOBOX_HTML = (FIXTURES / "combobox.html").read_text(encoding="utf-8")

FORM_HTML = """<html><body><h1>Backend Engineer</h1><form>
<label for="fn">First name *</label><input id="fn" name="fn" required>
<label for="ln">Last name *</label><input id="ln" name="ln" required>
<label for="em">Email *</label><input id="em" name="em" type="email" required>
<label for="np">What is your notice period?</label><input id="np" name="np">
<button type="submit">Send</button></form></body></html>"""


class StubAnswers:
    """Answer source that only answers yes/no questions once it has seen the options, like a careful LLM."""

    def __init__(self):
        self.calls: list[tuple[str, list[str] | None]] = []

    def answer(self, question: str, options: list[str] | None = None) -> tuple[str | None, int]:
        self.calls.append((question, list(options) if options else None))
        q = question.lower()
        if "degree" in q:
            return "Bachelor", 70
        if options and ("worked here" in q or "refer you" in q):
            return "No", 90
        return None, 0


def _runner(session, demo, resume, answers, **kw) -> FormRunner:
    return FormRunner(session, vault(resume), answers=answers, llm=FakeLLM(), user=demo, profiles_dir=PROFILES, **kw)


def _single_value(page, name: str) -> str:
    return page.locator(f'.select-shell[data-name="{name}"] .select__single-value').inner_text().strip()


# ------------------------------------------------------------------ extraction


def test_extract_classifies_comboboxes_and_pairs_twins(page):
    page.set_content(COMBOBOX_HTML)
    fields = extract_fields(page)
    by_label = {f.label: f for f in fields if f.label and not f.twin}
    q1 = by_label["Will you require visa sponsorship? *"]
    assert q1.type == "combobox" and q1.role == "combobox" and q1.aria_autocomplete == "list" and q1.required
    assert q1.expanded == "false" and not q1.hidden
    # React-Select's required twin is paired with its combobox, labelled like it, and hidden from the planner
    twins = [f for f in fields if f.twin]
    assert len(twins) == 5 and all(t.hidden for t in twins)
    assert {t.twin for t in twins} >= {q1.selector, by_label["Location (City) *"].selector}
    assert all(t.label for t in twins)
    # Legacy layout: the labelled hidden question_<n> input is the twin, the unlabelled combobox gets its label
    legacy = next(f for f in fields if f.id == "question_4")
    assert legacy.hidden and legacy.twin
    combo = next(f for f in fields if f.selector == legacy.twin)
    assert combo.type == "combobox" and combo.label.startswith("Did a current employee refer you")
    # Plain controls are untouched
    assert by_label["Country of residence *"].type == "select"
    assert by_label["First name *"].type == "text" and not by_label["First name *"].twin
    # A real input beside an optional combobox (phone number next to a country-code picker) is not its twin
    phone = next(f for f in fields if f.id == "phone")
    assert phone.type == "tel" and phone.required and not phone.hidden and not phone.twin
    dial = next(f for f in fields if f.id == "dial")
    assert dial.type == "combobox" and dial.label == "Country code" and not dial.required


# ------------------------------------------------------------------ filling


def test_dry_run_fills_comboboxes_and_autocomplete(page, session, demo, resume):
    answers = StubAnswers()
    runner = _runner(session, demo, resume, answers)
    result = runner.run(page, url="https://careers.example.com/apply/cb", html=COMBOBOX_HTML, submit=False)
    assert result.error == "" and result.profile_origin == "ai", result.summary()
    assert not result.submitted and not page.locator("#thanks").is_visible()
    # vault bool -> option by label
    assert _single_value(page, "q1") == "No"
    # yes/no question the stub could only answer once it saw the dropdown's options
    assert _single_value(page, "q2") == "No"
    assert ("Have you worked here before? *", ["Yes", "No"]) in answers.calls
    # fuzzy label pick: "Bachelor" -> "Bachelor's Degree"
    assert _single_value(page, "q3") == "Bachelor's Degree"
    # places style autocomplete: the suggestion, not the typed text, is what sticks
    assert _single_value(page, "city") == "Bengaluru, Karnataka, India"
    # the legacy hidden twin maps by label and its combobox is the one filled
    assert _single_value(page, "question_4") == "No"
    assert page.locator("#question_4").input_value() == "No"
    assert page.locator("#country").input_value() == "India"
    # the phone number goes into the phone input, not into the country-code combobox beside it
    assert page.locator("#phone").input_value() == DEMO_PROFILE["phone"]
    # hidden twins are never reported as unfilled
    assert result.unfilled_required == [], result.unfilled_required
    assert result.paused_reason == "", result.summary()
    answered = result.answers
    assert answered["Have you worked here before? *"] == "No" and answered["Highest degree *"] == "Bachelor's Degree"


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Mysuru", "Mysore, Karnataka, India"),  # one suggestion
        ("Bentley", "Bengaluru, Karnataka, India"),  # several: the FIRST one, not the second (ArrowDown+Enter)
    ],
)
def test_autocomplete_falls_back_to_first_suggestion_and_flags_it(page, session, demo, resume, location, expected):
    profile = {**DEMO_PROFILE, "location": location}
    v = VaultView(profile=profile, resume_path=str(resume))
    runner = FormRunner(session, v, answers=StubAnswers(), llm=FakeLLM(), user=demo, profiles_dir=PROFILES)
    result = runner.run(page, url="https://careers.example.com/apply/cb2", html=COMBOBOX_HTML)
    assert _single_value(page, "city") == expected
    assert any("first suggestion" in n and "Location (City)" in n for n in result.needs_human), result.needs_human
    assert result.paused_reason == "a question needs a human"
    assert not page.locator("#thanks").is_visible()


def test_no_options_notice_is_not_an_option(page, session, demo, resume):
    """React-Select renders `select__menu-notice--no-options` inside the listbox: it must read as an empty menu,
    so an autocomplete is not mistaken for a fixed list of ["No options"]."""
    runner = _runner(session, demo, resume, StubAnswers())
    page.set_content(COMBOBOX_HTML)
    city = next(f for f in extract_fields(page) if f.id == "city")
    loc = page.locator(city.selector).first
    assert runner._open_empty(page, city, loc) == []
    loc.press_sequentially("zz", delay=15)
    assert runner._wait_options(page, city, loc, 600) == []
    assert not any("no options" in t.lower() for t in runner._wait_options(page, city, loc, 300))


def test_dry_run_guard_blocks_enter_on_closed_menu(page, session, demo, resume):
    """Enter on a combobox input whose menu is closed is the browser's implicit form submission. A dry run
    installs a submit guard, so even a stray Enter leaves the form unsent."""
    runner = _runner(session, demo, resume, StubAnswers())
    result = runner.run(page, url="https://careers.example.com/apply/cb4", html=COMBOBOX_HTML, submit=False)
    assert not result.submitted and not page.locator("#thanks").is_visible()
    page.locator("#first_name").press("Enter")
    page.locator("#q1").press("Escape")
    page.locator("#q1").press("Enter")
    page.wait_for_timeout(200)
    assert not page.locator("#thanks").is_visible()
    # the runner itself never presses Enter unless the menu is open with a focused row
    q1 = next(f for f in extract_fields(page) if f.id == "q1")
    assert runner._enter_commits(page, q1, page.locator("#q1").first) is False


def test_locate_form_does_not_click_a_quick_apply_submit_button(page, session, demo, resume):
    """A two-field quick-apply form has fewer than MIN_FORM_FIELDS fields and its submit button says "Apply":
    that button is the form's submit control, not a link to the application, and must not be clicked."""
    quick = ('<html><body><h1>Backend Engineer</h1>'
             '<form onsubmit="event.preventDefault(); document.getElementById(\'thanks\').style.display=\'block\'">'
             '<label for="em">Email *</label><input id="em" name="email" type="email" required>'
             '<label for="cv">Resume</label><input id="cv" type="file" name="resume">'
             '<button type="submit">Apply</button></form>'
             '<div id="thanks" style="display:none">Thank you for applying</div></body></html>')
    _route_fixture(page, {"/job": quick})
    runner = _runner(session, demo, resume, AnswerSource(session, FakeLLM(), demo))
    result = runner.run(page, url="http://fixture.test/job", submit=False)
    assert result.url == "http://fixture.test/job" and page.url == "http://fixture.test/job"
    assert not result.submitted and not page.locator("#thanks").is_visible()
    assert page.locator("#em").input_value() == DEMO_PROFILE["email"]


def test_shows_rejects_short_substring_matches():
    shows = FormRunner._shows
    assert shows("Norway", "No", "No") is False  # "No" is in "Norway", but that is not the pick
    assert shows("Not applicable", "No", "No") is False
    assert shows("No", "No", "No") is True
    assert shows("Bengaluru, Karnataka, India", "Bengaluru", "Bengaluru") is True
    assert shows("India +91", "India", "Bengaluru, India") is True
    assert shows("Bachelor's Degree", "Bachelor's Degree", "Bachelor") is True
    assert shows("Bengaluru", "Bengaluru, Karnataka, India", "Bengaluru") is False  # only echoes what was typed


def test_combobox_without_answer_is_reported_once(page, session, demo, resume):
    runner = FormRunner(session, vault(resume), answers=None, llm=FakeLLM(), user=demo, profiles_dir=PROFILES)
    result = runner.run(page, url="https://careers.example.com/apply/cb3", html=COMBOBOX_HTML)
    # No answer source: the yes/no questions stay open, but only under their question text, never as the twin
    assert "Have you worked here before? *" in result.unfilled_required
    assert all(not u.startswith("[data-jobapply") for u in result.unfilled_required)
    assert len(result.unfilled_required) == len(set(result.unfilled_required))


def test_answer_source_reasks_when_banked_text_matches_no_option(session, demo):
    from jobapply.forms.answers import question_hash
    from jobapply.models import AnswerBank

    session.add(AnswerBank(user_id=demo.id, question_hash=question_hash("Degree *"), question="Degree *",
                           answer="B.Tech CSE", source="heuristic", approved=False))
    session.flush()
    src = AnswerSource(session, FakeLLM(), demo)
    assert src.answer("Degree *") == ("B.Tech CSE", 60)
    answer, conf = src.answer("Degree *", ["Associate's Degree", "Bachelor's Degree", "Master's Degree"])
    assert answer == "Bachelor's Degree" and conf > 60


def test_best_option_ranking():
    best = FormRunner._best_option
    assert best(["Yes", "No"], "no") == "No"
    assert best(["British Indian Ocean Territory +246", "India +91"], "Bengaluru, India") == "India +91"
    assert best(["Associate's Degree", "Bachelor's Degree"], "Bachelor") == "Bachelor's Degree"
    assert best(["Norway", "Nothing"], "No") is None  # short values never start-with match
    assert best(["Bengaluru, Karnataka, India"], "Bengaluru, India") == "Bengaluru, Karnataka, India"
    assert best(["Yes", "No"], "Maybe") is None
    # free text containing "no" / "yes" is not a boolean answer: hand it back so the options are re-asked
    assert best(["Yes", "No"], "Unknown") is None
    assert best(["Yes", "No"], "I don't know") is None
    assert best(["Yes", "No"], "Innovation") is None
    assert FormRunner._pick_option(["Yes", "No"], "Unknown") is None
    assert FormRunner._pick_option(["Germany", "India"], "Bengaluru, India") == "India"


# ------------------------------------------------------------------ _locate_form hops


def _route_fixture(page, pages: dict[str, str]) -> None:
    def handler(route, request):
        path = request.url.split("fixture.test", 1)[1].split("?")[0] or "/"
        body = pages.get(path)
        if body is None:
            route.fulfill(status=404, body="not here", content_type="text/html")
        else:
            route.fulfill(status=200, body=body, content_type="text/html; charset=utf-8")

    page.route("http://fixture.test/**", handler)


def test_locate_form_hops_into_embedded_frame(page, session, demo, resume):
    outer = ('<html><body><h1>Backend Engineer</h1><p>Apply below.</p>'
             '<iframe id="grnhse" src="http://fixture.test/embed" style="width:800px;height:600px"></iframe>'
             '</body></html>')
    _route_fixture(page, {"/job": outer, "/embed": FORM_HTML})
    runner = _runner(session, demo, resume, AnswerSource(session, FakeLLM(), demo))
    result = runner.run(page, url="http://fixture.test/job")
    assert result.url == "http://fixture.test/embed" and page.url == "http://fixture.test/embed"
    assert result.error == "" and result.profile_origin == "ai", result.summary()
    assert page.locator("#fn").input_value() == "Demo" and page.locator("#em").input_value() == DEMO_PROFILE["email"]
    assert not result.submitted


def test_locate_form_follows_apply_link(page, session, demo, resume):
    outer = ('<html><body><h1>Backend Engineer</h1><p>Great job.</p>'
             '<a href="/apply" class="btn">Apply now</a></body></html>')
    _route_fixture(page, {"/job": outer, "/apply": FORM_HTML})
    runner = _runner(session, demo, resume, AnswerSource(session, FakeLLM(), demo))
    result = runner.run(page, url="http://fixture.test/job")
    assert result.url == "http://fixture.test/apply"
    assert result.error == "" and page.locator("#fn").input_value() == "Demo", result.summary()
    assert page.locator("#np").input_value() == "30 days"
    assert not result.submitted
