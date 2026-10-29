"""Workday (``<tenant>.wd<N>.myworkdayjobs.com/<site>``): per-tenant candidate account, email verification
through the user's mailbox, and the fixed six-step application wizard.

- ``tenant``   parse a job URL into tenant / wd pod / site / job path, build the apply and account URLs
- ``session``  the persistent headless profile for the site plus the challenge cooldown (uniform interface)
- ``account``  the Create Account / Sign In step: classify the page, sign in, create the account with a
               generated password kept in the vault, confirm the email with ``mail.otp``
- ``wizard``   My Information -> My Experience -> Application Questions -> Voluntary Disclosures ->
               (Self Identify) -> Review, driven by ``forms.runner.FormRunner`` and
               ``config/form_profiles/workday.yaml``; Submit only when allowed
- ``apply``    ``apply_job(session, application, *, dry_run, auto_submit)`` and the gated orchestrator

See docs/sites/WORKDAY.md.
"""
