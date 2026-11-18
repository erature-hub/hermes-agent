"""Fleet quota gate at claim time (card t_074cc4fc).

The gate reads the same weekly cloud-token totals the aisuite sovereign router
meters — ``input_tokens + output_tokens`` for cloud-tagged models, summed across
every profile's ``state.db``, Monday 00:00Z window — and defers non-decision
claims once the fleet is past the trip ratio. These tests pin the five
behaviours the card's acceptance criteria name: trip → defer + event with the
card still ready, under-threshold → unchanged claim, priority-8 bypass, flag off
→ fully unchanged, and unreadable ledger → fail-open plus a warning.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timezone, UTC
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_quota_gate as qg

# 23.06M is the real 2026-10-20 week's burn: past 4.25M trip on a 5M budget.
TRIPPED_TOKENS = 23_060_000


@pytest.fixture
def gate_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME that also owns every profile's usage ledger."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in (
        "HERMES_KANBAN_HOME",
        "HERMES_FLEET_QUOTA_GATE",
        "HERMES_FLEET_WEEKLY_BUDGET_TOKENS",
        "HERMES_FLEET_QUOTA_WARN_RATIO",
        "HERMES_FLEET_TRIP_RATIO",
        "HERMES_FLEET_QUOTA_ALLOW_PRIORITY",
        "HERMES_FLEET_QUOTA_CACHE_SECONDS",
        "HERMES_FLEET_QUOTA_ALERT_CMD",
    ):
        monkeypatch.delenv(var, raising=False)
    kb.init_db()
    qg.reset_cache()
    # The suite's earlier dispatcher tests park fake Popen objects in this
    # module-global and never pop them; a later dispatch tick then dies calling
    # ``.poll()`` on one. These tests own the dict for their duration.
    kbd._live_worker_procs.clear()
    try:
        yield home
    finally:
        kbd._live_worker_procs.clear()
        qg.reset_cache()


def _seed_usage(
    home: Path,
    profile: str | None,
    *,
    tokens: int,
    model: str = "deepseek-v4.1-flash:cloud",
    last_seen: float | None = None,
) -> Path:
    """Write one ``session_model_usage`` row into a profile's ledger."""
    path = (home / "profiles" / profile / "state.db") if profile else (home / "state.db")
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS session_model_usage ("
            "session_id TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER, "
            "last_seen REAL)"
        )
        seen = time.time() if last_seen is None else last_seen
        conn.execute(
            "INSERT INTO session_model_usage VALUES (?, ?, ?, ?, ?)",
            (f"s_{profile or 'default'}", model, tokens, 0, seen),
        )
        conn.commit()
    finally:
        conn.close()
    # The gate memoizes readings for 60s; a fresh seed must be visible.
    qg.reset_cache()
    return path


def _spawn_recorder(calls: list):
    def _spawn(task, workspace):
        calls.append((task.id, getattr(task, "assignee", None)))
        return 4242

    return _spawn


def _events(conn, task_id: str, kind: str) -> list[str]:
    return [
        row["kind"] for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        )
        if row["kind"] == kind
    ]


# ─── Trip defers the claim ──────────────────────────────────────────────────


def test_tripped_gate_defers_claim_and_leaves_card_ready(
    gate_home, all_assignees_spawnable,
):
    _seed_usage(gate_home, "worker", tokens=TRIPPED_TOKENS)
    calls: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="bulk audit", assignee="worker", priority=2)
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(calls))
        row = conn.execute("SELECT status, claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()
        deferred_events = _events(conn, tid, "claim_deferred_quota")

    assert calls == [], "a tripped gate must not spawn the worker"
    assert res.spawned == []
    assert res.quota_deferred == [tid]
    assert res.quota_state == qg.STATE_TRIPPED
    assert row["status"] == "ready", "deferred means still claimable later, not failed"
    assert row["claim_lock"] is None
    assert len(deferred_events) == 1


def test_repeated_ticks_do_not_re_emit_the_deferral_event(
    gate_home, all_assignees_spawnable,
):
    """A throttled week is many ticks with MOVING spend: one event, not one per tick.

    The regression this pins: the dedupe once compared the whole event payload,
    and `used_tokens` changes on nearly every real tick (in-flight workers plus
    the operator's own traffic), so the event re-emitted roughly once a minute
    for the rest of the week. The ledger must move between ticks for the
    property to be tested at all — a frozen seed passes even with the defect.
    """
    _seed_usage(gate_home, "worker", tokens=TRIPPED_TOKENS)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="bulk audit", assignee="worker")
        for extra in (0, 100_000, 250_000, 1_000_000):
            if extra:
                _seed_usage(gate_home, "worker", tokens=extra)
            kbd.dispatch_once(conn, spawn_fn=_spawn_recorder([]))
        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'claim_deferred_quota'",
            (tid,),
        ).fetchall()

    assert len(rows) == 1, "moving spend must not re-emit the deferral event"
    assert qg.stored_defer_event_key(rows[0]["payload"]) == (
        qg.STATE_TRIPPED, qg.week_window()[0],
    ), "the event still carries the stable key plus its spend reading"


def test_dedupe_ignores_used_tokens_but_not_the_quota_week(gate_home):
    """The key is (state, week_start): spend is telemetry, the week is identity."""
    base = {"state": qg.STATE_TRIPPED, "week_start": 1_792_550_400, "used_tokens": 10}
    later = dict(base, used_tokens=99_000_000)
    next_week = dict(base, week_start=base["week_start"] + 7 * 86_400)
    assert qg.stored_defer_event_key(base) == qg.stored_defer_event_key(later)
    assert qg.stored_defer_event_key(base) != qg.stored_defer_event_key(next_week)
    # Tolerate anything the event log might hold: legacy/foreign payload shapes.
    assert qg.stored_defer_event_key(None) is None
    assert qg.stored_defer_event_key("not json") is None
    assert qg.stored_defer_event_key('{"state": "tripped"}') is None
    assert qg.stored_defer_event_key({"state": qg.STATE_TRIPPED, "week_start": "x"}) is None
    assert qg.stored_defer_event_key('{"state": "tripped", "week_start": 5}') == (
        qg.STATE_TRIPPED, 5,
    )


def test_under_threshold_claims_normally(gate_home, all_assignees_spawnable):
    _seed_usage(gate_home, "worker", tokens=1_000_000)  # 20% of budget
    calls: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="bulk audit", assignee="worker")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(calls))
        row = conn.execute("SELECT status FROM tasks WHERE id = ?", (tid,)).fetchone()

    assert [c[0] for c in calls] == [tid]
    assert res.quota_deferred == []
    assert res.quota_state == qg.STATE_OK
    assert row["status"] == "running"


def test_decision_lane_claims_while_throttled(gate_home, all_assignees_spawnable):
    """priority >= 8 is the DECISION lane: it is the point of the gate."""
    _seed_usage(gate_home, "worker", tokens=TRIPPED_TOKENS)
    calls: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="[DECISION] founder call", assignee="worker", priority=8,
        )
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(calls))

    assert [c[0] for c in calls] == [tid]
    assert res.quota_deferred == []
    assert res.quota_state == qg.STATE_TRIPPED, "state is still reported for telemetry"


def test_flag_off_is_fully_unchanged(gate_home, all_assignees_spawnable, monkeypatch):
    monkeypatch.setenv("HERMES_FLEET_QUOTA_GATE", "0")
    _seed_usage(gate_home, "worker", tokens=TRIPPED_TOKENS)
    calls: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="bulk audit", assignee="worker", priority=2)
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(calls))
        deferred_events = _events(conn, tid, "claim_deferred_quota")

    assert [c[0] for c in calls] == [tid]
    assert res.quota_state is None
    assert deferred_events == []


# ─── Fail-open ──────────────────────────────────────────────────────────────


def test_missing_ledger_fails_open_with_warning(
    gate_home, all_assignees_spawnable, caplog,
):
    caplog.set_level(logging.WARNING, logger=qg.__name__)
    calls: list = []  # gate_home has no state.db at all
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="bulk audit", assignee="worker")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(calls))

    assert [c[0] for c in calls] == [tid], "a missing ledger must never brick the fleet"
    assert res.quota_state == qg.STATE_UNKNOWN
    assert res.quota_deferred == []
    assert "no usage ledger" in caplog.text


def test_unreadable_ledger_fails_open_with_warning(
    gate_home, all_assignees_spawnable, caplog,
):
    """A corrupt/locked ledger is the realistic failure: fail open, say so."""
    caplog.set_level(logging.WARNING, logger=qg.__name__)
    (gate_home / "state.db").write_bytes(b"not a sqlite database at all")
    calls: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="bulk audit", assignee="worker")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(calls))

    assert [c[0] for c in calls] == [tid]
    assert res.quota_state == qg.STATE_UNKNOWN
    assert "ledger read failed" in caplog.text


def test_gate_crash_fails_open_and_never_breaks_the_tick(
    gate_home, all_assignees_spawnable, monkeypatch, caplog,
):
    """A bug in the gate itself must not take the dispatcher down with it."""
    caplog.set_level(logging.WARNING, logger=qg.__name__)

    def _boom(**_kwargs):
        raise RuntimeError("ledger exploded")

    monkeypatch.setattr(qg, "evaluate", _boom)
    calls: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="bulk audit", assignee="worker")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(calls))

    assert [c[0] for c in calls] == [tid]
    assert res.quota_state is None, "a crashed gate behaves exactly like a disabled one"
    assert "failing OPEN" in caplog.text


# ─── Ledger basis ───────────────────────────────────────────────────────────


def test_only_cloud_rows_count(gate_home):
    """Local traffic cannot 429 the provider, so it must not throttle the fleet."""
    _seed_usage(gate_home, "worker", tokens=TRIPPED_TOKENS, model="ornith-1.5-35b-a3b")
    reading = qg.evaluate(use_cache=False)
    assert reading.state == qg.STATE_OK
    assert reading.used_tokens == 0


def test_every_profile_ledger_is_summed(gate_home):
    for profile in ("worker", "vox", "rayze"):
        _seed_usage(gate_home, profile, tokens=2_000_000)
    _seed_usage(gate_home, None, tokens=500_000)  # default profile
    reading = qg.evaluate(use_cache=False)

    assert reading.used_tokens == 6_500_000
    assert reading.state == qg.STATE_TRIPPED
    assert len(reading.sources) == 4
    assert {Path(s["path"]).parent.name for s in reading.sources} == {
        gate_home.name, "worker", "vox", "rayze",
    }


def test_warn_band_does_not_defer(gate_home, all_assignees_spawnable):
    _seed_usage(gate_home, "worker", tokens=3_600_000)  # 72% > warn, < trip
    calls: list = []
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="bulk audit", assignee="worker")
        res = kbd.dispatch_once(conn, spawn_fn=_spawn_recorder(calls))

    assert [c[0] for c in calls] == [tid]
    assert res.quota_state == qg.STATE_WARN


def test_budget_and_trip_ratio_are_env_driven(gate_home, monkeypatch):
    _seed_usage(gate_home, "worker", tokens=TRIPPED_TOKENS)
    monkeypatch.setenv("HERMES_FLEET_WEEKLY_BUDGET_TOKENS", "100000000")
    qg.reset_cache()
    assert qg.evaluate(use_cache=False).state == qg.STATE_OK

    monkeypatch.setenv("HERMES_FLEET_TRIP_RATIO", "0.99")
    monkeypatch.setenv("HERMES_FLEET_WEEKLY_BUDGET_TOKENS", "30000000")
    qg.reset_cache()
    # 23.06M against a 30M budget: past 70% (21M) but short of 99% (29.7M).
    assert qg.evaluate(use_cache=False).state == qg.STATE_WARN


# ─── Window + alerting ──────────────────────────────────────────────────────


def test_week_window_anchors_to_monday_midnight_utc():
    # Wednesday 2026-10-14 12:00Z → Monday 2026-10-12 00:00Z.
    wednesday = datetime(2026, 10, 14, 12, 0, tzinfo=UTC).timestamp()
    start, end = qg.week_window(wednesday)
    assert datetime.fromtimestamp(start, tz=UTC) == datetime(
        2026, 10, 12, 0, 0, tzinfo=UTC
    )
    assert end - start == 7 * 86_400

    # A Sunday belongs to the week that opened the preceding Monday.
    sunday = datetime(2026, 10, 18, 23, 0, tzinfo=UTC).timestamp()
    assert qg.week_window(sunday)[0] == start


def test_one_alert_per_transition(gate_home):
    _seed_usage(gate_home, "worker", tokens=TRIPPED_TOKENS)
    tripped = qg.evaluate(env={}, use_cache=False)
    first = qg.announce(tripped, env={})
    second = qg.announce(tripped, env={})

    assert first and first.startswith("HERMES FLEET QUOTA TRIP")
    assert second is None, "an unchanged state must be silent (30s ticks must not spam)"

    healthy = qg.evaluate(
        env={"HERMES_FLEET_WEEKLY_BUDGET_TOKENS": "100000000"}, use_cache=False,
    )
    recovered = qg.announce(healthy, env={})
    assert recovered and recovered.startswith("HERMES FLEET QUOTA RECOVERED")
    assert qg.announce(healthy, env={}) is None


def test_first_healthy_observation_is_silent(gate_home):
    """A healthy first sight is not news; it is only the baseline."""
    _seed_usage(gate_home, "worker", tokens=1000)
    reading = qg.evaluate(env={}, use_cache=False)
    assert qg.announce(reading, env={}) is None
    # …but it is persisted, so the next change is measured against "ok".
    assert (gate_home / "kanban" / "fleet_quota_gate.state.json").is_file()


def test_alert_command_receives_the_message(gate_home, tmp_path, monkeypatch):
    out = tmp_path / "alert.txt"
    monkeypatch.setenv(
        "HERMES_FLEET_QUOTA_ALERT_CMD",
        f'"{__import__("sys").executable}" -c '
        f'"import sys,pathlib;pathlib.Path(r\'{out}\').write_text(sys.stdin.read())"',
    )
    _seed_usage(gate_home, "worker", tokens=TRIPPED_TOKENS)
    reading = qg.evaluate(use_cache=False)
    message = qg.announce(reading)

    assert message is not None
    assert "FLEET QUOTA TRIP" in out.read_text(encoding="utf-8")


def test_describe_suppression_names_the_hold():
    result = kbd.DispatchResult()
    result.quota_deferred = ["t_1", "t_2"]
    result.quota_state = qg.STATE_TRIPPED
    described = kbd.describe_suppression([result])
    assert "quota_deferred=2" in described
    assert "quota_state=tripped" in described
