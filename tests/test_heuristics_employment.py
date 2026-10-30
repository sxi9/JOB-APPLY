"""No-LLM answers for the "previously employed by / current employee of <company>" family and for "how did you
hear about us", which every ATS asks (seen live on Workday's My Information step, 2026-10-30)."""

from __future__ import annotations

from jobapply.ai.heuristics import heuristic_answer
from jobapply.seed import DEMO_PROFILE

LONG_Q = ("Have you previously been employed by any Revantage entity across any location, including as an intern? "
          "CURRENT EMPLOYEES: Please apply via our internal Workday career site instead.")
YN = ["Yes", "No"]


def test_previously_employed_is_no_unless_the_profile_worked_there():
    assert heuristic_answer(LONG_Q, YN, DEMO_PROFILE) == ("No", 85)
    assert heuristic_answer(LONG_Q, None, DEMO_PROFILE) == ("No", 85)  # a text box gets the word
    for q in ("Have you ever worked for Acme?", "Have you previously worked for Acme Corp, including as an intern?",
              "Are you a current employee of Acme?", "Are you a former employee?", "Were you employed by us before?",
              "Are you currently employed by Acme or one of its subsidiaries?", "Have you been an intern here before?"):
        assert heuristic_answer(q, YN, DEMO_PROFILE) == ("No", 85), q
    # the profile's current employer, by full name or its first distinctive word ("Example" of "Example Systems")
    assert heuristic_answer("Have you previously been employed by Example Systems?", YN, DEMO_PROFILE) == ("Yes", 85)
    assert heuristic_answer("Are you a current employee of any Example entity?", YN, DEMO_PROFILE) == ("Yes", 85)
    # a past employer from the resume counts too
    assert heuristic_answer("Have you ever worked for Startup Labs?", YN, DEMO_PROFILE) == ("Yes", 85)
    current = {**DEMO_PROFILE, "current_company": "Revantage Asia Pacific"}
    assert heuristic_answer(LONG_Q, YN, current) == ("Yes", 85)


def test_employment_rule_leaves_other_questions_alone():
    # a general status question, not "employed by us"
    assert heuristic_answer("Are you currently employed?", YN, DEMO_PROFILE) == (None, 0)
    # the current-company rule still wins its own question
    assert heuristic_answer("Current company", None, DEMO_PROFILE) == ("Example Systems", 95)
    assert heuristic_answer("Current employer", None, DEMO_PROFILE) == ("Example Systems", 95)
    # a mismatching option list hands the question on
    assert heuristic_answer(LONG_Q, ["Maybe", "Later"], DEMO_PROFILE) == (None, 0)
    # experience / screening questions that only share the verb: no employer named, so no confident No
    for q in ("Have you worked with Kubernetes before?", "Have you worked with Python before?",
              "Have you ever worked remotely?", "Have you ever worked in the United States?",
              "Were you referred by a current employee?", "Have you ever been an intern at a bank?",
              "Would you like to work for us?"):
        assert heuristic_answer(q, YN, DEMO_PROFILE) == (None, 0), q
    for q in ("Describe your previous employment", "Years of prior employment"):
        assert heuristic_answer(q, None, DEMO_PROFILE) == (None, 0), q
    # "Startup Labs" on the resume must not make "a startup" the employer
    for q in ("Have you previously worked in a startup environment?", "Have you previously worked in a startup?",
              "Have you worked at a startup before?", "Are you currently working at a startup?"):
        assert heuristic_answer(q, YN, DEMO_PROFILE) == (None, 0), q
    global_co = {**DEMO_PROFILE, "current_company": "Global Payments"}
    assert heuristic_answer("Have you ever worked for a global company?", YN, global_co) == (None, 0)
    assert heuristic_answer("Have you ever worked for Global Payments?", YN, global_co) == ("Yes", 85)


def test_how_did_you_hear_defaults_to_job_board_or_linkedin():
    assert heuristic_answer("How Did You Hear About Us?", None, DEMO_PROFILE) == ("Job Board", 70)
    assert heuristic_answer("How did you hear about this position?", None, DEMO_PROFILE) == ("Job Board", 70)
    assert heuristic_answer("Source *", None, DEMO_PROFILE) == ("Job Board", 70)
    assert heuristic_answer("Where did you find this job?", None, DEMO_PROFILE) == ("Job Board", 70)
    linkedin = {"source": "linkedin"}
    assert heuristic_answer("How did you hear about us?", None, DEMO_PROFILE, job=linkedin) == ("LinkedIn", 70)

    class JobRow:
        source = "linkedin_search"

    assert heuristic_answer("How did you hear about us?", None, DEMO_PROFILE, job=JobRow()) == ("LinkedIn", 70)
    # against an option list the entry is picked, or the question handed on when there is no fitting entry
    options = ["Career Fair", "Career Website", "Job Boards", "Employee Referral", "LinkedIn"]
    assert heuristic_answer("How did you hear about us?", options, DEMO_PROFILE) == ("Job Boards", 70)
    assert heuristic_answer("How did you hear about us?", options, DEMO_PROFILE, job=linkedin) == ("LinkedIn", 70)
    assert heuristic_answer("How did you hear about us?", ["Career Fair", "Referral"], DEMO_PROFILE) == (None, 0)
    # not a source question
    assert heuristic_answer("Open source contributions?", None, DEMO_PROFILE) == (None, 0)
    for q in ("Where did you learn to code?", "Where did you learn Python?",
              "How did you find out about our company culture?"):
        assert heuristic_answer(q, None, DEMO_PROFILE) == (None, 0), q
    assert heuristic_answer("How did you learn of this opportunity?", None, DEMO_PROFILE) == ("Job Board", 70)
