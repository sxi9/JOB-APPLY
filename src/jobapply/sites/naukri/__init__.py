"""Naukri (naukri.com): public search collector, persistent session, one-click Apply with chatbot answering.

Layout mirrors ``jobapply.sites.linkedin``:

- ``search``   ``NaukriCollector`` (logged-out JSON search API, headless-browser fallback)
- ``session``  ``classify_page`` / ``check_session`` / ``import_cookies`` / ``NaukriSession`` / ``throttled``
- ``apply``    ``apply_job`` (Apply or "Apply on company site"), the chatbot questionnaire, ``refresh_profile``
"""
