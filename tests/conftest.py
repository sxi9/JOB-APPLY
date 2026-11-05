import pytest
from sqlmodel import Session

from jobapply.config import get_settings
from jobapply.db import create_all, make_engine
from jobapply.notify import LogNotifier, set_notifier
from jobapply.seed import DEMO_PROFILE
from jobapply.users import create_user


@pytest.fixture(autouse=True)
def _no_live_whatsapp():
    """The box's .env points at a LIVE WhatsApp bridge; no test may ever reach it. Every test starts with
    the log-only notifier (tests that want a transport install their own MockTransport-backed one)."""
    set_notifier(LogNotifier())
    yield
    set_notifier(None)


@pytest.fixture
def engine(tmp_path):
    eng = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    with Session(engine, expire_on_commit=False) as s:
        yield s


# ------------------------------------------------------------------ browser and demo user, shared by the form tests


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        # The sandbox ships a Chromium build that differs from the one this Playwright wants.
        exe = get_settings().chromium_executable
        b = pw.chromium.launch(executable_path=exe) if exe else pw.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def page(browser):
    ctx = browser.new_context()
    p = ctx.new_page()
    yield p
    ctx.close()


@pytest.fixture
def demo(session, tmp_path, monkeypatch):
    monkeypatch.setenv("JOBAPPLY_DATA_DIR", str(tmp_path / "data"))
    get_settings.cache_clear()
    # automatic mode: the apply-path tests exercise the pickers and fillers (new users default to notify-only)
    user = create_user(session, "demo", "demo1234", profile=DEMO_PROFILE,
                       preferences={"daily_cap": 20, "apply_mode": "auto"})
    session.commit()
    yield user
    get_settings.cache_clear()


@pytest.fixture
def resume(tmp_path):
    p = tmp_path / "resume.pdf"
    p.write_bytes(b"%PDF-1.4\n%fake\n")
    return p
