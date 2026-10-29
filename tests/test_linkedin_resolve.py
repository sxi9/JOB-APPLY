"""LinkedIn offsite-apply resolution: pure parsing of saved logged-in job pages, plus resolve_external_url
driven against a duck-typed fake page so the suite never opens a browser."""

from pathlib import Path

from jobapply.sites.linkedin.resolve import (
    ApplyResolution,
    parse_apply_button,
    resolve_external_url,
)

FIXTURES = Path(__file__).parent / "fixtures" / "linkedin"


def read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ parse_apply_button (pure)


def test_parse_easy_apply_button():
    assert parse_apply_button(read("logged_in_easy_apply.html")) == ("easy_apply", None)


def test_parse_offsite_button_with_tracked_url():
    kind, url = parse_apply_button(read("logged_in_offsite_greenhouse.html"))
    assert kind == "offsite"
    assert url == "https://boards.greenhouse.io/stripe/jobs/6543210?gh_src=linkedin"


def test_parse_signin_walled_page_has_no_url():
    assert parse_apply_button(read("logged_in_signin_wall.html")) == ("signin", None)


def test_parse_handles_guest_fixtures_too():
    # The same parser recognises the guest posting markup (search.py fixtures).
    assert parse_apply_button(read("posting_easy_apply.html")) == ("easy_apply", None)
    kind, url = parse_apply_button(read("posting_offsite_greenhouse.html"))
    assert kind == "offsite" and url == "https://boards.greenhouse.io/stripe/jobs/6543210?gh_src=linkedin"


def test_parse_offsite_not_fooled_by_sidebar_easy_apply():
    """A logged-in offsite job page whose PRIMARY control is an external Apply must stay 'offsite' even when a
    recommended-jobs rail elsewhere renders its own Easy Apply controls (same jobs-apply-button class).
    Regression: easy-apply detection used to scan every control on the page."""
    html = """
    <section class="top-card-layout">
      <div class="jobs-apply-button--top-card">
        <a class="jobs-apply-button artdeco-button artdeco-button--primary"
           href="https://boards.greenhouse.io/stripe/jobs/6543210?gh_src=linkedin">
          <span class="artdeco-button__text">Apply</span>
        </a>
      </div>
    </section>
    <aside class="jobs-similar-jobs">
      <button class="jobs-apply-button" aria-label="Easy Apply to Other Role at Other Co">
        <span class="artdeco-button__text">Easy Apply</span>
      </button>
    </aside>
    """
    kind, url = parse_apply_button(html)
    assert kind == "offsite"
    assert url == "https://boards.greenhouse.io/stripe/jobs/6543210?gh_src=linkedin"


def test_parse_empty_and_unknown():
    assert parse_apply_button("") == ("none", None)
    assert parse_apply_button("<html><body><p>nothing here</p></body></html>") == ("none", None)


def test_resolution_properties():
    assert ApplyResolution("easy_apply").easy_apply is True
    assert ApplyResolution("offsite", "https://boards.greenhouse.io/x/jobs/1", "greenhouse").resolved is True
    assert ApplyResolution("offsite", None).resolved is False
    assert ApplyResolution("signin").resolved is False and ApplyResolution("signin").easy_apply is False


# ------------------------------------------------------------------ resolve_external_url (fake page)


class FakePage:
    """Enough of a Playwright page for resolve_external_url: goto / wait_for_timeout / content."""

    def __init__(self, html: str, url: str = "https://www.linkedin.com/jobs/view/1/"):
        self.html = html
        self.url = url
        self.visited: list[str] = []

    def goto(self, url, **kw):
        self.visited.append(url)
        return None

    def wait_for_timeout(self, ms):
        pass

    def content(self):
        return self.html


def test_resolve_external_url_easy_apply():
    page = FakePage(read("logged_in_easy_apply.html"))
    res = resolve_external_url(page, "https://www.linkedin.com/jobs/view/4480000001/")
    assert res.kind == "easy_apply" and res.apply_url is None and res.ats_type is None
    assert page.visited == ["https://www.linkedin.com/jobs/view/4480000001/"]


def test_resolve_external_url_offsite_detects_greenhouse():
    page = FakePage(read("logged_in_offsite_greenhouse.html"))
    res = resolve_external_url(page, "https://www.linkedin.com/jobs/view/4480000002/")
    assert res.resolved is True
    assert res.apply_url == "https://boards.greenhouse.io/stripe/jobs/6543210?gh_src=linkedin"
    assert res.ats_type == "greenhouse"


def test_resolve_external_url_signin_is_unresolved():
    page = FakePage(read("logged_in_signin_wall.html"))
    res = resolve_external_url(page, "https://www.linkedin.com/jobs/view/3/")
    assert res.kind == "signin" and res.resolved is False
