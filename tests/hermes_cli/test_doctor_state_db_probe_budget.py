"""The doctor's state.db check must not read as a hang on a bloated store.

``PRAGMA integrity_check`` is O(file size): measured 140s on a 2.5 GiB store and 86s on a
1.45 GiB one when the OS had to read it cold (5s once cached). The doctor ran that scan
IN PLACE on a quiet store, so ``hermes -p <profile> doctor`` took minutes and looked hung --
which is exactly the symptom that sent the profile's operator to the recovery guide instead
of to the cause.

The scan is now bounded: past the budget the probe reports a PARTIAL verdict (the cheap FTS
and write probes still run, so the FTS-corruption class is still detected), and doctor
finishes in seconds. ``hermes doctor --fix`` and ``hermes sessions repair`` still scan in
full -- a full scan belongs to the repair path, not to a health report.
"""
import sqlite3
import time

import pytest

from hermes_cli.doctor_report import Finding
from hermes_cli.doctor_state import (
    _WRITE_PROBE_INTEGRITY_BUDGET_S, _state_db_health, _write_health_reason,
)
from hermes_state_repair import HealthCheckUnavailable, _db_opens_cleanly


def _store(tmp_path, *, rows: int = 200):
    """A real WAL-mode state.db with some message payload."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s", "cli")
        for i in range(rows):
            db._execute_write(lambda conn, i=i: conn.execute(
                "INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)",
                ("s", "user", f"payload-{i} " + "x" * 200, 1.0)))
        return db
    except Exception:
        db.close()
        raise


def test_integrity_check_is_bounded_and_reports_a_partial_verdict(tmp_path):
    """A budget too small to finish must NOT report the store healthy, and must not raise."""
    db = _store(tmp_path)
    db.close()
    db_path = tmp_path / "state.db"

    # A zero budget cannot complete the scan, so the probe must come back as a partial verdict.
    reason = _db_opens_cleanly(db_path, integrity_budget_s=0.0)
    assert isinstance(reason, HealthCheckUnavailable)
    assert "budget" in str(reason)


def test_a_healthy_store_still_reports_healthy(tmp_path):
    """The budget must not turn a HEALTHY store into a failure when the scan does fit."""
    db = _store(tmp_path, rows=5)
    db.close()
    db_path = tmp_path / "state.db"
    assert _db_opens_cleanly(db_path, integrity_budget_s=30.0) is None


def test_an_unbounded_probe_is_unchanged(tmp_path):
    """The default (no budget) keeps the historic behaviour: a full scan, no truncation."""
    db = _store(tmp_path, rows=5)
    db.close()
    db_path = tmp_path / "state.db"
    assert _db_opens_cleanly(db_path) is None


def test_the_doctor_db_check_finishes_well_inside_the_budget(tmp_path, monkeypatch):
    """The doctor's state.db check must complete, and quickly, on a store it cannot scan."""
    db = _store(tmp_path, rows=200)
    db.close()
    db_path = tmp_path / "state.db"

    # Nothing holds this store, so the doctor probes it IN PLACE (the slow path).
    monkeypatch.setattr("hermes_state_repair._live_writer_holds_db", lambda p: False)

    f = Finding()
    t0 = time.time()
    _state_db_health(f, False, db_path, "HERMES_HOME")
    elapsed = time.time() - t0

    assert elapsed < 10.0, f"the state.db check took {elapsed:.1f}s -- it must not read as a hang"


def test_the_doctor_reports_the_bounded_scan_honestly(tmp_path, monkeypatch):
    """A cut-short scan is reported as unavailable, never as a clean bill of health."""
    db = _store(tmp_path, rows=200)
    db.close()
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("hermes_state_repair._live_writer_holds_db", lambda p: False)
    # A budget no scan can meet: this is what a multi-GB store looks like to the probe.
    monkeypatch.setattr("hermes_cli.doctor_state._WRITE_PROBE_INTEGRITY_BUDGET_S", 0.0)

    reason = _write_health_reason(db_path, should_fix=False)
    assert isinstance(reason, HealthCheckUnavailable)
    assert "integrity_check" in str(reason)


def test_a_small_store_is_still_scanned_in_full(tmp_path, monkeypatch):
    """The budget only bites a store too big to scan: a small one is verified, not skipped."""
    db = _store(tmp_path, rows=200)
    db.close()
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("hermes_state_repair._live_writer_holds_db", lambda p: False)

    assert _write_health_reason(db_path, should_fix=False) is None


def test_fix_still_probes_without_a_bound(tmp_path, monkeypatch):
    """``--fix`` is the repair path: it may take the time it needs."""
    db = _store(tmp_path, rows=5)
    db.close()
    db_path = tmp_path / "state.db"
    monkeypatch.setattr("hermes_state_repair._live_writer_holds_db", lambda p: False)

    assert _write_health_reason(db_path, should_fix=True) is None


def test_the_budget_is_short_enough_for_the_acceptance_target():
    """The AC is a doctor state.db check under 5s. The budget plus the rest of the check
    (a session count, a stats read) has to fit inside it."""
    assert _WRITE_PROBE_INTEGRITY_BUDGET_S <= 3.0


def test_a_corrupt_store_is_still_reported_as_unhealthy(tmp_path, monkeypatch):
    """Bounding the scan must not blind the probe to real damage."""
    db = _store(tmp_path, rows=5)
    db.close()
    db_path = tmp_path / "state.db"
    # Real byte damage in the middle of the file.
    with open(db_path, "r+b") as fh:
        fh.seek(4096)
        fh.write(b"\x00" * 8192)

    monkeypatch.setattr("hermes_state_repair._live_writer_holds_db", lambda p: False)
    reason = _write_health_reason(db_path, should_fix=False)
    assert reason is not None, "a damaged store must not report healthy"
