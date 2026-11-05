"""Tests for the pause-reason guardrail — a cron pause must record WHY it stopped.

The Nov-1 hindsight teardown paused every hindsight cron with a null ``paused_reason``: weeks
later nothing on disk said why those jobs had stopped, so the pause read as a silent success.
The writer (``cron.jobs.update_job``) is the chokepoint every door shares — CLI, agent tool,
dashboard, REST API — so the requirement lives there and is enforced for all of them.
"""

import json

import pytest

from cron.jobs import (
    PAUSE_REASON_REQUIRED_ERROR,
    create_job,
    get_job,
    pause_job,
    resume_job,
    update_job,
)


@pytest.fixture()
def tmp_cron_dir(tmp_path, monkeypatch):
    monkeypatch.setattr("cron.jobs.CRON_DIR", tmp_path / "cron")
    monkeypatch.setattr("cron.jobs.JOBS_FILE", tmp_path / "cron" / "jobs.json")
    monkeypatch.setattr("cron.jobs.OUTPUT_DIR", tmp_path / "cron" / "output")
    return tmp_path


def _make_job():
    return create_job(prompt="digest", schedule="every 1h", name="guarded")


def _stored_record(job_id):
    # Read the path through the module: the module-level JOBS_FILE constant was bound at
    # import time, before the fixture pointed the store at tmp_path.
    from cron.jobs import JOBS_FILE as jobs_file

    raw = json.loads(jobs_file.read_text(encoding="utf-8"))
    return next(j for j in raw["jobs"] if j["id"] == job_id)


class TestPauseReasonRequired:
    def test_pause_without_a_reason_is_refused(self, tmp_cron_dir):
        job = _make_job()
        with pytest.raises(ValueError) as exc:
            pause_job(job["id"])
        assert "reason" in str(exc.value).lower()
        # The record is untouched — the pause never happened.
        stored = get_job(job["id"])
        assert stored["enabled"] is True
        assert stored.get("paused_reason") is None
        assert stored.get("paused_at") is None

    def test_a_blank_reason_is_refused(self, tmp_cron_dir):
        job = _make_job()
        with pytest.raises(ValueError):
            pause_job(job["id"], reason="   ")
        assert get_job(job["id"])["enabled"] is True

    def test_pause_with_a_reason_is_stored_on_disk(self, tmp_cron_dir):
        job = _make_job()
        reason = "Nov-4 sweep: holding for the restic fix"
        paused = pause_job(job["id"], reason=reason)
        assert paused["enabled"] is False
        assert paused["state"] == "paused"
        assert paused["paused_reason"] == reason
        # …and it is on disk, not just in the returned record.
        assert _stored_record(job["id"])["paused_reason"] == reason

    def test_resume_clears_the_reason(self, tmp_cron_dir):
        job = _make_job()
        pause_job(job["id"], reason="temporary hold")
        resumed = resume_job(job["id"])
        assert resumed["enabled"] is True
        assert resumed.get("paused_reason") is None
        assert resumed.get("paused_at") is None
        assert _stored_record(job["id"])["paused_reason"] is None


class TestWriterGuard:
    """The guard lives in the writer, so a door that forgets the reason is still caught."""

    def test_bare_enabled_flip_is_refused(self, tmp_cron_dir):
        job = _make_job()
        with pytest.raises(ValueError) as exc:
            update_job(job["id"], {"enabled": False, "state": "paused"})
        assert str(exc.value) == PAUSE_REASON_REQUIRED_ERROR
        assert get_job(job["id"])["enabled"] is True

    def test_enabled_flip_with_a_reason_is_accepted(self, tmp_cron_dir):
        job = _make_job()
        updated = update_job(
            job["id"], {"enabled": False, "state": "paused", "paused_reason": "disk pressure"})
        assert updated["enabled"] is False
        assert updated["paused_reason"] == "disk pressure"

    def test_an_already_disabled_job_is_not_a_flip(self, tmp_cron_dir):
        """Re-touching a paused job (a rename, say) must not demand a second reason."""
        job = _make_job()
        pause_job(job["id"], reason="hold")
        updated = update_job(job["id"], {"name": "renamed"})
        assert updated["enabled"] is False
        assert updated["paused_reason"] == "hold"

    def test_a_resume_is_not_a_flip(self, tmp_cron_dir):
        job = _make_job()
        pause_job(job["id"], reason="hold")
        resumed = resume_job(job["id"])
        assert resumed["enabled"] is True
        assert resumed["paused_reason"] is None
