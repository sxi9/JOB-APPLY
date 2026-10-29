"""Workday tenant URL parsing (pure)."""

from __future__ import annotations

import pytest

from jobapply.sites.workday.tenant import WorkdayTenant, is_workday_url, parse_tenant_url

LIVE = "https://revantage.wd1.myworkdayjobs.com/Revantage/job/Bengaluru/Associate--Information-Security-Analyst_JR104387"


def test_parse_live_job_url():
    t = parse_tenant_url(LIVE)
    assert t == WorkdayTenant(tenant="revantage", wd="wd1", site="Revantage", locale=None,
                              job_path="/job/Bengaluru/Associate--Information-Security-Analyst_JR104387")
    assert t.host == "revantage.wd1.myworkdayjobs.com"
    assert t.base_url == "https://revantage.wd1.myworkdayjobs.com/Revantage"
    assert t.job_url == LIVE and t.apply_url == LIVE + "/apply"
    assert t.apply_manually_url == LIVE + "/apply/applyManually"
    assert t.autofill_url == LIVE + "/apply/autofillWithResume"
    assert t.candidate_home_url == "https://revantage.wd1.myworkdayjobs.com/Revantage/userHome"
    assert t.vault_site == "workday:revantage" and t.req_id == "JR104387"


@pytest.mark.parametrize("url", [
    LIVE + "/apply",
    LIVE + "/apply/applyManually",
    LIVE + "/apply/autofillWithResume?source=x",
    "https://revantage.wd1.myworkdayjobs.com/en-US/Revantage/job/Bengaluru/Associate--Information-Security-Analyst_JR104387/apply/applyManually",  # noqa: E501
])
def test_apply_and_locale_variants_point_at_the_same_job(url):
    t = parse_tenant_url(url)
    assert t is not None and t.tenant == "revantage" and t.site == "Revantage"
    assert t.job_path == "/job/Bengaluru/Associate--Information-Security-Analyst_JR104387"
    assert t.vault_site == "workday:revantage"


def test_locale_is_kept_in_the_urls():
    t = parse_tenant_url("https://acme.wd5.myworkdayjobs.com/en-US/External/details/Engineer_R-1")
    assert t.locale == "en-US" and t.site == "External" and t.job_path == "/details/Engineer_R-1"
    assert t.base_url == "https://acme.wd5.myworkdayjobs.com/en-US/External"
    assert t.apply_manually_url.endswith("/External/details/Engineer_R-1/apply/applyManually")
    assert t.req_id == "R-1"


def test_site_root_and_non_workday_urls():
    root = parse_tenant_url("https://acme.wd5.myworkdayjobs.com/External")
    assert root is not None and root.job_path is None and root.job_url is None and root.apply_url is None
    assert root.login_url == "https://acme.wd5.myworkdayjobs.com/External/login"
    assert parse_tenant_url("https://acme.wd5.myworkdayjobs.com/") is None
    assert parse_tenant_url("https://acme.wd5.myworkdayjobs.com/job/x/y") is None
    assert parse_tenant_url("https://boards.greenhouse.io/acme/jobs/1") is None
    assert parse_tenant_url("https://acme.myworkday.com/acme/d/home.htmld") is None
    assert parse_tenant_url("") is None and parse_tenant_url(None) is None  # type: ignore[arg-type]
    assert is_workday_url(LIVE) and not is_workday_url("https://jobs.lever.co/acme/1")
    upper = parse_tenant_url("https://ACME.WD3.myworkdayjobs.com/Careers/job/Remote/Dev_123")
    assert upper.vault_site == "workday:acme" and upper.wd == "wd3"
