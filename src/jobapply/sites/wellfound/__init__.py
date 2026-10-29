"""Wellfound (wellfound.com, ex-AngelList Talent): public role-page collector, cookie-import session, gated apply.

Modules follow the LinkedIn package layout (`jobapply.sites.linkedin`):

- `search`  WellfoundCollector: logged-out job search over the public `/role/...` landing pages.
- `session` classify_page / check_session / import_cookies on the persistent headless profile.
- `apply`   apply_job: the logged-in "Apply" modal (note + optional questions), fully gated.
"""

from jobapply.sites.wellfound.apply import apply_job
from jobapply.sites.wellfound.search import WellfoundCollector
from jobapply.sites.wellfound.session import SITE, WellfoundSession, check_session, classify_page, import_cookies

__all__ = ["SITE", "WellfoundCollector", "WellfoundSession", "apply_job", "check_session", "classify_page",
           "import_cookies"]
