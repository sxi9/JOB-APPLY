"""Workday's My Information widgets as captured live on the Revantage tenant (2026-10-30): listbox buttons,
multi-select prompts with categories, the long-label "previously employed" radio and a required phone number.
Inline HTML fixtures on real headless Chromium; the widget behaviour lives in fixtures/forms/workday_widgets.js."""
# ruff: noqa: E501  (embedded JavaScript fixture, like tests/test_workday_account.py)

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jobapply.forms.fields import extract_fields
from jobapply.forms.runner import AnswerSource, FormRunner
from jobapply.seed import DEMO_PROFILE
from jobapply.sites.workday.wizard import STEP_MY_INFORMATION, load_step_profiles, step_spec, workday_vault_view

FIXTURES = Path(__file__).parent / "fixtures" / "forms"
PROFILES = Path(__file__).parent.parent / "config" / "form_profiles"
WIDGET_JS = (FIXTURES / "workday_widgets.js").read_text(encoding="utf-8")
WD_URL = "https://revantage.wd5.myworkdayjobs.com/en-US/Revantage/job/Mumbai/Analyst_R1/apply/applyManually"

LONG_Q = ("Have you previously been employed by any Revantage entity across any location, including as an intern? "
          "CURRENT EMPLOYEES: Please apply via our internal Workday career site instead.")
SOURCE_TREE = ["Career Fair", "Career Website", "Employee Referral",
               {"Online": ["Job Board", "LinkedIn", "Company Website"]}, "TA Outreach", "Other"]

# The live page: every field in div[data-automation-id="formField-<name>"] with a <label for>; text inputs with
# ids like name--legalName--firstName; required marked by an asterisk in the label.
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>[hidden]{display:none} label{display:block;margin-top:8px}</style>
<script>__WIDGETS__</script></head><body>
<div data-automation-id="applyFlowPage"><h2 data-automation-id="jobTitleHeading">Analyst</h2>
<div id="content"></div>
<button type="button" data-automation-id="bottom-navigation-next-button" onclick="window.__next=(window.__next||0)+1">Save and Continue</button></div>
<script>
const W = window.wdWidgets;
const REQ = ' <abbr title="required">*</abbr>';
const text = (label, id, name, req) => `<div data-automation-id="formField-${name}"><label for="${id}">${label}${req ? REQ : ''}</label>` +
  `<input type="text" id="${id}" name="${name}"${req ? ' aria-required="true"' : ''}></div>`;
document.getElementById('content').innerHTML =
  text('First Name', 'name--legalName--firstName', 'legalName--firstName', true) +
  text('Last Name', 'name--legalName--lastName', 'legalName--lastName', true) +
  text('Address Line 1', 'address--addressLine1', 'addressLine1', false) +
  text('City', 'address--city', 'city', true) +
  text('Postal Code', 'address--postalCode', 'postalCode', false) +
  W.listboxHtml('Country' + REQ, 'country', ['India', 'United States of America', 'Germany'], {required: true, value: __COUNTRY__}) +
  text('Email Address', 'email--email', 'email', true) +
  W.promptHtml('Country Phone Code' + REQ, 'phoneCode', ['India (+91)', 'United States of America (+1)'], {required: true, chips: __PHONE_CHIPS__}) +
  W.listboxHtml('Phone Device Type' + REQ, 'phoneType', ['Mobile', 'Home', 'Work', 'Fax'], {required: true}) +
  text('Phone Number', 'phone--phoneNumber', 'phoneNumber', true) +
  W.promptHtml('How Did You Hear About Us?' + REQ, 'source', __SOURCE_TREE__, {required: true}) +
  __EXTRA__
  W.radioHtml(__LONG_Q__ + REQ, 'previousWorker', ['Yes', 'No'], {required: true, styled: true});
W.wire(document);
</script></body></html>"""


def page_html(*, country: str = "India", phone_chips=("India (+91)",), source_tree=SOURCE_TREE, extra: str = "") -> str:
    return (PAGE.replace("__WIDGETS__", WIDGET_JS).replace("__COUNTRY__", json.dumps(country))
            .replace("__PHONE_CHIPS__", json.dumps(list(phone_chips))).replace("__SOURCE_TREE__", json.dumps(source_tree))
            .replace("__LONG_Q__", json.dumps(LONG_Q)).replace("__EXTRA__", extra))


class App:
    resume_variant: str | None = None
    cover_letter = ""
    message = ""


def runner_for(session, demo, resume, answers=None) -> FormRunner:
    """A FormRunner set up like the wizard does it: the Workday vault view and the My Information step profile."""
    app = App()
    app.resume_variant = str(resume)
    runner = FormRunner(session, workday_vault_view(demo, app), answers=answers, llm=None, user=demo,
                        profiles_dir=PROFILES, learn=False, navigate=False)
    base, steps = load_step_profiles()
    runner.profiles = {**runner.profiles, "workday": step_spec(base, steps[STEP_MY_INFORMATION])}
    return runner


def chips(page, name: str) -> list[str]:
    return page.locator(f'[data-automation-id="formField-{name}"] [data-automation-id="selectedItem"] span').all_inner_texts()


def opens(page, name: str) -> int:
    return int(page.locator(f"#{name}--{name}").get_attribute("data-opens") or 0)


# ------------------------------------------------------------------ extraction


def test_extract_sees_listbox_buttons_prompts_and_the_long_radio(page):
    page.set_content(page_html())
    fields = extract_fields(page)
    by_label = {f.label: f for f in fields if f.label}
    country = by_label["Country *"]
    assert country.type == "listbox" and country.is_listbox and country.tag == "button"
    assert country.value == "India" and country.required and country.selector == "#country--country"
    device = by_label["Phone Device Type *"]
    assert device.is_listbox and device.value == "" and device.required  # "Select One" is no value
    source = by_label["How Did You Hear About Us? *"]
    assert source.type == "combobox" and source.multiselect and source.required and source.value == ""
    assert source.selector == "#source--source" and "multiselect" in source.brief()
    code = by_label["Country Phone Code *"]
    assert code.multiselect and code.value == "India (+91)"
    radios = [f for f in fields if f.type == "radio"]
    assert [r.label for r in radios] == ["Yes", "No"] and all(r.required for r in radios)
    assert radios[0].group == LONG_Q + " *" and radios[0].question == LONG_Q + " *"
    phone = by_label["Phone Number *"]
    assert phone.type == "text" and phone.required and phone.selector == "#phone--phoneNumber"
    first = by_label["First Name *"]
    assert first.selector == "#name--legalName--firstName" and first.name == "legalName--firstName"
    # the chips' remove buttons are not fields
    assert not any(f.tag == "button" and not f.is_listbox for f in fields)


# ------------------------------------------------------------------ the step as the wizard fills it


def test_my_information_fills_the_widgets_and_fails_only_on_phone_number(page, session, demo, resume):
    demo.profile = {**demo.profile, "phone": ""}
    runner = runner_for(session, demo, resume, AnswerSource(session, None, demo))
    result = runner.run(page, url=WD_URL, html=page_html(), submit=False)
    assert result.profile_origin == "builtin" and result.error == "", result.summary()
    assert result.unfilled_required == ["Phone Number *"], result.unfilled_required
    assert result.needs_human == [] and result.paused_reason == "required fields left empty"
    # the listbox was opened once and now shows the choice; Country already showed a value and was left alone
    assert page.locator("#phoneType--phoneType").inner_text() == "Mobile" and opens(page, "phoneType") == 1
    assert page.locator("#phoneType--phoneType").get_attribute("aria-expanded") == "false"
    assert page.locator("#country--country").inner_text() == "India" and opens(page, "country") == 0
    # the source prompt: "Job Board" sits under the "Online" category, the popup was closed afterwards
    assert chips(page, "source") == ["Job Board"]
    assert page.locator("#source--source").get_attribute("aria-expanded") == "false"
    assert page.locator("#source--source").input_value() == ""
    # the pre-filled phone code chip is kept
    assert chips(page, "phoneCode") == ["India (+91)"]
    # the long-label radio: No, from the heuristic (no LLM here)
    assert page.locator("#previousWorker-1").is_checked() and not page.locator("#previousWorker-0").is_checked()
    assert page.locator("#name--legalName--firstName").input_value() == "Demo"
    assert page.locator("#phone--phoneNumber").input_value() == ""
    assert page.evaluate("() => window.__next || 0") == 0  # never clicked Save and Continue
    answers = result.answers
    assert answers["Phone Device Type *"] == "Mobile" and answers["Country *"] == "India"
    assert answers["Country Phone Code *"] == "India (+91)" and answers[(LONG_Q + " *")[:120]] == "No"


def test_my_information_passes_with_a_phone_number(page, session, demo, resume):
    runner = runner_for(session, demo, resume, AnswerSource(session, None, demo))
    result = runner.run(page, url=WD_URL, html=page_html(), submit=False)
    assert result.unfilled_required == [] and result.needs_human == [] and result.paused_reason == ""
    assert page.locator("#phone--phoneNumber").input_value() == DEMO_PROFILE["phone"]


def test_prompt_takes_the_matching_leaf_directly(page, session, demo, resume):
    demo.profile = {**demo.profile, "how_heard": "Career Website"}
    runner = runner_for(session, demo, resume, AnswerSource(session, None, demo))
    result = runner.run(page, url=WD_URL, html=page_html(), submit=False)
    assert chips(page, "source") == ["Career Website"] and result.needs_human == []


def test_prompt_falls_back_to_a_generic_entry_and_flags_it(page, session, demo, resume):
    """Nothing matches "Naukri": the full list is browsed, "Online" (a fallback) opens, "Job Board" (the first
    fallback) is picked one level down and the reviewer is told."""
    demo.profile = {**demo.profile, "how_heard": "Naukri"}
    runner = runner_for(session, demo, resume, AnswerSource(session, None, demo))
    result = runner.run(page, url=WD_URL, html=page_html(), submit=False)
    assert chips(page, "source") == ["Job Board"]
    assert result.unfilled_required == []
    assert any("How Did You Hear" in n and "picked 'Job Board' for 'Naukri'" in n for n in result.needs_human), result.needs_human
    assert page.locator("#source--source").get_attribute("aria-expanded") == "false"


class LanguagesStub:
    """Answers a free-text ask with a value the prompt does not have (or nothing at all, `blind=None`), then
    picks from the entries it is shown."""

    def __init__(self, blind: tuple[str | None, int] = ("Haskell", 60)):
        self.calls: list[tuple[str, list[str] | None]] = []
        self.blind = blind

    def answer(self, question, options=None):
        self.calls.append((question, list(options) if options else None))
        if "languages" not in question.lower():
            return None, 0
        return ("Go", 80) if options else self.blind


def test_other_prompts_hand_their_entries_back_for_a_second_ask(page, session, demo, resume):
    extra = ("W.promptHtml('Preferred Languages' + REQ, 'languages', ['Python', 'Go', 'Rust'], {required: true}) +")
    stub = LanguagesStub()
    runner = runner_for(session, demo, resume, stub)
    result = runner.run(page, url=WD_URL, html=page_html(extra=extra), submit=False)
    assert ("Preferred Languages *", None) in stub.calls
    assert ("Preferred Languages *", ["Python", "Go", "Rust"]) in stub.calls
    assert chips(page, "languages") == ["Go"]
    assert "Preferred Languages *" not in result.unfilled_required
    # no generic source fallback on a prompt that is not a "how did you hear" question
    assert not any("Preferred Languages" in n for n in result.needs_human)


def test_prompt_nobody_could_answer_blind_is_browsed_once_and_asked_with_its_entries(page, session, demo, resume):
    """Planned as pending (no blind answer), the prompt was never opened: its entries are read once and the
    answer source gets them, instead of the field landing in unfilled_required unasked."""
    extra = ("W.promptHtml('Preferred Languages' + REQ, 'languages', ['Python', 'Go', 'Rust'], {required: true}) +")
    stub = LanguagesStub(blind=(None, 0))
    runner = runner_for(session, demo, resume, stub)
    result = runner.run(page, url=WD_URL, html=page_html(extra=extra), submit=False)
    assert ("Preferred Languages *", None) in stub.calls
    assert ("Preferred Languages *", ["Python", "Go", "Rust"]) in stub.calls
    assert chips(page, "languages") == ["Go"]
    assert page.locator("#languages--languages").get_attribute("aria-expanded") == "false"
    assert "Preferred Languages *" not in result.unfilled_required


def test_mapped_answers_radio_is_asked_with_its_choices(page, session, demo, resume):
    """The step map sends the previously-employed radio to an `answers.` key: the one ask carries Yes/No, so a
    source that needs the options (bank, LLM) can answer it."""

    class ChoiceStub:
        def __init__(self):
            self.calls = []

        def answer(self, question, options=None):
            self.calls.append((question, list(options) if options else None))
            return ("No", 90) if options and "previously been employed" in question else (None, 0)

    stub = ChoiceStub()
    runner = runner_for(session, demo, resume, stub)
    result = runner.run(page, url=WD_URL, html=page_html(), submit=False)
    assert (LONG_Q + " *", ["Yes", "No"]) in stub.calls
    assert page.locator("#previousWorker-1").is_checked()
    assert not any("previously been employed" in u for u in result.unfilled_required)


LOOSE_MARKUP = """
<div data-automation-id="formField-device"><span id="dev-lbl">Phone Device Type *</span>
<button type="button" aria-haspopup="listbox" aria-labelledby="dev-lbl" id="device--device" name="device">Select One</button></div>
<div data-automation-id="formField-prev"><fieldset><legend><label>Have you previously been employed by Acme? *</label></legend>
<div role="radiogroup"><div><input type="radio" id="prev-0" name="prev" value="Yes"><label for="prev-0">Yes</label></div>
<div><input type="radio" id="prev-1" name="prev" value="No"><label for="prev-1">No</label></div></div></fieldset></div>
<div data-automation-id="formField-src"><label>How Did You Hear About Us? *</label><div data-automation-id="multiSelectContainer">
<div data-automation-id="selectedItemList"></div><input type="text" id="src--src" name="src" placeholder="Search"></div></div>
<div><label for="note">Notes</label><input type="text" id="note" name="note"></div>
"""


def test_extract_reads_required_from_labelledby_legend_and_unassociated_labels(page):
    """The asterisk in an aria-labelledby target, in a radio group's legend (no `required` on the inputs) and
    in a formField <label> without `for` (whose input only has the placeholder "Search")."""
    page.set_content(f"<!doctype html><html><body>{LOOSE_MARKUP}</body></html>")
    by_sel = {f.selector: f for f in extract_fields(page)}
    device = by_sel["#device--device"]
    assert device.is_listbox and device.label == "Phone Device Type *" and device.required
    radios = [by_sel["#prev-0"], by_sel["#prev-1"]]
    assert all(r.required for r in radios) and radios[0].group == "Have you previously been employed by Acme? *"
    src = by_sel["#src--src"]
    assert src.multiselect and src.label == "How Did You Hear About Us? *" and src.required
    assert not by_sel["#note"].required


def test_listbox_with_no_matching_option_is_reasked_with_its_rows(page, session, demo, resume):
    class DeviceStub:
        def __init__(self):
            self.calls = []

        def answer(self, question, options=None):
            self.calls.append((question, list(options) if options else None))
            if "device" in question.lower() and options:
                return "Work", 80
            return (None, 0)

    stub = DeviceStub()
    runner = runner_for(session, demo, resume, stub)
    # the profile says "Pager": no such row, so the rows are read and the answer source picks one
    runner.profiles["workday"].fields["label=Phone Device Type"] = "const:Pager"
    runner.run(page, url=WD_URL, html=page_html(), submit=False)
    assert ("Phone Device Type *", ["Mobile", "Home", "Work", "Fax"]) in stub.calls
    assert page.locator("#phoneType--phoneType").inner_text() == "Work"
    assert page.locator("#phoneType--phoneType").get_attribute("aria-expanded") == "false"


def test_previously_employed_is_yes_for_an_employee_of_that_company(page, session, demo, resume):
    demo.profile = {**demo.profile, "current_company": "Revantage Asia Pacific"}
    runner = runner_for(session, demo, resume, AnswerSource(session, None, demo))
    runner.run(page, url=WD_URL, html=page_html(), submit=False)
    assert page.locator("#previousWorker-0").is_checked()


def test_runner_never_clicks_a_row_that_reads_like_an_action(page, session, demo, resume):
    runner = runner_for(session, demo, resume, None)
    with pytest.raises(ValueError, match="form action"):
        runner._click_row(page, lambda: None, "Submit")
