"""Custom dropdowns (React-Select style comboboxes, places autocompletes) and the form-location hops."""

from pathlib import Path

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
    # hidden twins are never reported as unfilled
    assert result.unfilled_required == [], result.unfilled_required
    assert result.paused_reason == "", result.summary()
    answered = result.answers
    assert answered["Have you worked here before? *"] == "No" and answered["Highest degree *"] == "Bachelor's Degree"


def test_autocomplete_falls_back_to_first_suggestion_and_flags_it(page, session, demo, resume):
    profile = {**DEMO_PROFILE, "location": "Mysuru"}
    v = VaultView(profile=profile, resume_path=str(resume))
    runner = FormRunner(session, v, answers=StubAnswers(), llm=FakeLLM(), user=demo, profiles_dir=PROFILES)
    result = runner.run(page, url="https://careers.example.com/apply/cb2", html=COMBOBOX_HTML)
    assert _single_value(page, "city") == "Mysore, Karnataka, India"
    assert any("first suggestion" in n and "Location (City)" in n for n in result.needs_human), result.needs_human
    assert result.paused_reason == "a question needs a human"


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
