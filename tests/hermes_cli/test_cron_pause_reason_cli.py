"""Tests for `hermes cron pause` — the CLI door of the pause-reason guardrail.

The reason is mandatory: a pause with no recorded reason is indistinguishable, weeks later,
from a job that simply stopped firing. The CLI refuses it, asks for one on an interactive
terminal, and refuses it outright when it cannot ask (a cron run, a script, a pipeline).
"""

import argparse

import pytest

from cron.jobs import create_job, get_job
from hermes_cli.cron import cron_command
from hermes_cli.subcommands.cron import build_cron_parser


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _parse(argv):
    parser = argparse.ArgumentParser(prog="hermes")
    subparsers = parser.add_subparsers(dest="command")
    build_cron_parser(subparsers, cmd_cron=cron_command)
    return parser.parse_args(argv)


class TestCronPauseRequiresReason:
    def test_pause_without_a_reason_fails(self, tmp_cron_dir, capsys):
        job = create_job(prompt="digest", schedule="every 1h")
        rc = cron_command(_parse(["cron", "pause", job["id"]]))
        assert rc == 1
        out = capsys.readouterr().out
        assert "reason" in out.lower()
        # The job was NOT paused.
        assert get_job(job["id"])["enabled"] is True

    def test_pause_with_a_reason_succeeds(self, tmp_cron_dir, capsys):
        job = create_job(prompt="digest", schedule="every 1h")
        rc = cron_command(_parse(["cron", "pause", job["id"], "--reason", "disk pressure"]))
        assert rc == 0
        stored = get_job(job["id"])
        assert stored["enabled"] is False
        assert stored["paused_reason"] == "disk pressure"
        assert "Paused" in capsys.readouterr().out

    def test_a_non_interactive_caller_is_never_prompted(self, tmp_cron_dir, monkeypatch):
        """No TTY → no prompt: a script/cron caller must pass --reason itself."""
        import hermes_cli.cron as cron_cli

        monkeypatch.setattr(cron_cli, "_is_interactive_terminal", lambda: False)

        def _boom(*_a, **_k):
            raise AssertionError("a non-interactive pause must not prompt")

        monkeypatch.setattr("builtins.input", _boom)
        job = create_job(prompt="digest", schedule="every 1h")
        assert cron_command(_parse(["cron", "pause", job["id"]])) == 1

    def test_an_interactive_caller_is_prompted(self, tmp_cron_dir, monkeypatch, capsys):
        import hermes_cli.cron as cron_cli

        monkeypatch.setattr(cron_cli, "_is_interactive_terminal", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _prompt="": "typed at the terminal")
        job = create_job(prompt="digest", schedule="every 1h")
        assert cron_command(_parse(["cron", "pause", job["id"]])) == 0
        assert get_job(job["id"])["paused_reason"] == "typed at the terminal"

    def test_an_empty_prompt_answer_is_still_refused(self, tmp_cron_dir, monkeypatch, capsys):
        import hermes_cli.cron as cron_cli

        monkeypatch.setattr(cron_cli, "_is_interactive_terminal", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _prompt="": "   ")
        job = create_job(prompt="digest", schedule="every 1h")
        assert cron_command(_parse(["cron", "pause", job["id"]])) == 1
        assert get_job(job["id"])["enabled"] is True
