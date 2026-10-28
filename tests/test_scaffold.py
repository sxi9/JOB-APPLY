from typer.testing import CliRunner

from jobapply.cli import app
from jobapply.config import Settings


def test_settings_defaults_are_safe(monkeypatch):
    monkeypatch.delenv("JOBAPPLY_DRY_RUN", raising=False)
    s = Settings(_env_file=None)
    assert s.dry_run is True
    assert s.kill_switch is False
    assert s.llm_backend == "fake"


def test_settings_from_env(monkeypatch):
    monkeypatch.setenv("JOBAPPLY_SCORE_THRESHOLD", "55")
    assert Settings(_env_file=None).score_threshold == 55


def test_cli_help():
    result = CliRunner().invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("collect", "score", "apply", "serve"):
        assert cmd in result.output
