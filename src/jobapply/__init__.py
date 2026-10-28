"""Multi-user job scraper and auto-apply system."""

__version__ = "0.1.0"


def main() -> None:
    from jobapply.cli import app

    app()
