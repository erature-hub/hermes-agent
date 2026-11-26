"""The fleet-wide runtime bound: ``kanban.max_runtime_seconds``.

A card claimed without an explicit ``--max-runtime`` used to hold its worker slot
for as long as it liked: six unbounded runners held every slot for 7-11h while ten
ready cards waited behind them. These tests pin the bound that ends that, the
precedence between the card's own cap and the fleet default, and the named
diagnostic that makes a long runner visible instead of a silent slot loss.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_diagnostics as kd
from hermes_cli import kanban_max_runtime as kmr


@pytest.fixture
def fleet_home(tmp_path, monkeypatch):
    """A fresh HERMES_HOME with a clean kanban DB and a writable config."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _set_fleet_bound(home: Path, seconds) -> None:
    """Write the operator's fleet bound into the home's config."""
    (home / "config.yaml").write_text(
        f"kanban:\n  max_runtime_seconds: {seconds}\n", encoding="utf-8"
    )


def _running_card(conn, *, title="long job", max_runtime_seconds=None, age_seconds=30):
    """A claimed card with a live worker pid whose attempt started ``age_seconds`` ago."""
    tid = kb.create_task(
        conn, title=title, assignee="worker", max_runtime_seconds=max_runtime_seconds,
    )
    kb.claim_task(conn, tid)
    kbd._set_worker_pid(conn, tid, os.getpid())
    old_started = int(time.time()) - age_seconds
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET started_at = ? WHERE id = ?", (old_started, tid))
        conn.execute(
            "UPDATE task_runs SET started_at = ? "
            "WHERE id = (SELECT current_run_id FROM tasks WHERE id = ?)",
            (old_started, tid),
        )
    return tid


@pytest.fixture
def dead_worker(monkeypatch):
    """Pretend the worker's pid is gone so the grace-poll exits immediately."""
    import hermes_cli.kanban_db as _kb

    original = _kb._pid_alive
    _kb._pid_alive = lambda pid: False
    yield
    _kb._pid_alive = original


# --- the fleet default bounds a card that carries no cap of its own --------------------


def test_a_card_claimed_without_a_cap_is_reclaimed_at_the_fleet_default(fleet_home, dead_worker):
    """The incident: a card with no ``--max-runtime`` held its slot for 11h."""
    _set_fleet_bound(fleet_home, 60)
    conn = kbc.connect()
    try:
        tid = _running_card(conn, age_seconds=120)
        assert kb.get_task(conn, tid).max_runtime_seconds is None

        assert kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None) == [tid]

        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert task.worker_pid is None
        events = kb.list_events(conn, tid)
        to_event = next(e for e in events if e.kind == "timed_out")
        assert to_event.payload["limit_seconds"] == 60
        assert to_event.payload["elapsed_seconds"] >= 120
    finally:
        conn.close()


def test_the_fleet_default_books_a_timeout_not_a_crash(fleet_home, dead_worker):
    """A timeout charges the timeout breaker; a crash would charge the wrong one."""
    _set_fleet_bound(fleet_home, 60)
    conn = kbc.connect()
    try:
        tid = _running_card(conn, age_seconds=120)
        kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None)

        run = conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id = ? ORDER BY id DESC", (tid,)
        ).fetchone()
        assert run["outcome"] == "timed_out"
        kinds = {e.kind for e in kb.list_events(conn, tid)}
        assert "timed_out" in kinds
        assert "crashed" not in kinds
        assert kb.get_task(conn, tid).consecutive_failures == 1
        # The card is no longer running, so the crash sweep must not book it.
        assert kbd.detect_crashed_workers(conn) == []
    finally:
        conn.close()


def test_an_explicit_card_cap_wins_over_the_fleet_default(fleet_home, dead_worker):
    """A card that was told it may run for an hour is not cut off by a 60s fleet bound."""
    _set_fleet_bound(fleet_home, 60)
    conn = kbc.connect()
    try:
        long_card = _running_card(conn, title="pinned", max_runtime_seconds=3600, age_seconds=120)
        assert kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None) == []
        assert kb.get_task(conn, long_card).status == "running"
    finally:
        conn.close()


def test_the_card_own_shorter_cap_still_applies_under_a_generous_fleet_bound(fleet_home, dead_worker):
    """The fleet default is a ceiling for unbounded cards, not a floor for bounded ones."""
    _set_fleet_bound(fleet_home, 3600)
    conn = kbc.connect()
    try:
        tid = _running_card(conn, max_runtime_seconds=60, age_seconds=120)
        assert kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None) == [tid]
        assert kb.get_task(conn, tid).status == "ready"
    finally:
        conn.close()


def test_the_fleet_bound_is_read_live_so_an_operator_edit_applies(fleet_home, dead_worker):
    """The bound is resolved when it is enforced, not captured at boot."""
    _set_fleet_bound(fleet_home, 60)
    conn = kbc.connect()
    try:
        tid = _running_card(conn, age_seconds=120)
        _set_fleet_bound(fleet_home, 3600)
        assert kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None) == []
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


def test_zero_turns_the_fleet_bound_off(fleet_home, dead_worker):
    """``0`` is the explicit opt-out to an unbounded fleet."""
    _set_fleet_bound(fleet_home, 0)
    conn = kbc.connect()
    try:
        tid = _running_card(conn, age_seconds=86400)
        assert kbd.enforce_max_runtime(conn, signal_fn=lambda pid, sig: None) == []
        assert kb.get_task(conn, tid).status == "running"
    finally:
        conn.close()


# --- the resolver ----------------------------------------------------------------------


def test_an_unusable_configured_value_falls_back_to_the_shipped_default():
    assert kmr.DEFAULT_MAX_RUNTIME_SECONDS == 21600
    assert kmr.resolve_max_runtime_seconds(None) == 21600
    assert kmr.resolve_max_runtime_seconds(-5) == 21600
    assert kmr.resolve_max_runtime_seconds("not-a-number") == 21600
    assert kmr.resolve_max_runtime_seconds(True) == 21600
    assert kmr.resolve_max_runtime_seconds(0) is None
    assert kmr.resolve_max_runtime_seconds(900) == 900


def test_the_card_cap_wins_the_effective_bound():
    assert kmr.effective_max_runtime_seconds(None, 21600) == 21600
    assert kmr.effective_max_runtime_seconds(60, 21600) == 60
    assert kmr.effective_max_runtime_seconds(None, None) is None


def test_the_shipped_default_is_the_documented_config_default():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["kanban"]["max_runtime_seconds"] == kmr.DEFAULT_MAX_RUNTIME_SECONDS


# --- the named condition ---------------------------------------------------------------


def test_an_over_bound_card_is_a_named_diagnostic_with_its_elapsed_time(fleet_home, dead_worker):
    """A long runner is a named condition, not a silent slot loss."""
    _set_fleet_bound(fleet_home, 3600)
    conn = kbc.connect()
    try:
        tid = _running_card(conn, age_seconds=7200)
        task = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)
        config = kd.config_from_runtime_config({"kanban": {"max_runtime_seconds": 3600}})

        diags = [d for d in kd.compute_task_diagnostics(task, [], runs, config=config)
                 if d.kind == "over_max_runtime"]
        assert len(diags) == 1
        assert diags[0].severity == "error"          # 2x the bound
        assert diags[0].data["limit_seconds"] == 3600
        assert diags[0].data["elapsed_seconds"] >= 7200
        assert [a.kind for a in diags[0].actions] == ["reclaim", "cli_hint"]
    finally:
        conn.close()


def test_a_card_inside_its_bound_is_not_named(fleet_home, dead_worker):
    _set_fleet_bound(fleet_home, 3600)
    conn = kbc.connect()
    try:
        tid = _running_card(conn, age_seconds=60)
        task = kb.get_task(conn, tid)
        runs = kb.list_runs(conn, tid)
        config = kd.config_from_runtime_config({"kanban": {"max_runtime_seconds": 3600}})

        assert not [d for d in kd.compute_task_diagnostics(task, [], runs, config=config)
                    if d.kind == "over_max_runtime"]
    finally:
        conn.close()
