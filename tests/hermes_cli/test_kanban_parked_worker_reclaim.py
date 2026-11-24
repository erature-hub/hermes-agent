"""A parked worker holds a dispatch slot forever: liveness is judged by the heartbeat, not by progress.

The card this came from measured a live fleet and concluded that four of six
running workers were parked, holding every slot. Reproducing that measurement
here changed the design, and the change is the point of this file:

* the HEARTBEAT cannot tell a parked worker from a working one. ``agent.
  activity_tracking`` bridges chunk-level ACTIVITY into ``last_heartbeat_at``
  every 60 s, so a worker waiting on a provider response that never arrives
  keeps a fresh heartbeat;
* the CARD LOG is not a progress signal either. The worker's stdout is a file
  handle, not a tty, so Python block-buffers it: a healthy worker was measured
  on the live fleet with a log 51 minutes stale while its transcript grew every
  few seconds;
* cross-process CPU accounting is NOT available on this host. A child
  spinning at 100% reads back 0.00 CPU seconds through both ``psutil`` and
  ``GetProcessTimes``, while the child's own ``time.process_time()`` reports
  the real 1.98 s.

The signal that does work is the worker's SESSION TRANSCRIPT: every tool call
and every reply appends to it, so a working worker advances it continuously and
a parked worker leaves it frozen.

These tests pin the fix for the CLASS, not the instance:

* a running card whose worker produced no output for N minutes is reclaimed BY
  THE DISPATCHER, with the measurement in the event;
* the reclaimed card frees its slot, and the freed slot goes to the
  highest-priority ready card;
* a card with no runtime ceiling of its own is bounded by a default;
* the dispatcher says WHY it spawned nothing — "full of work" and "full of
  nothing" must not look the same from the outside.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_liveness as liveness

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]
LOCK = f"{HOST}:{os.getpid()}"


def _idle_child() -> subprocess.Popen:
    """A live process that burns no CPU: the shape of a parked worker."""
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(600)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _pid_alive(pid: int) -> bool:
    try:
        import psutil
    except Exception:
        return False
    try:
        return psutil.pid_exists(pid)
    except Exception:
        return False


def _absent_pid(base: int = 3_900_000) -> int:
    """A pid that provably does not exist on this host."""
    pid = base
    while _pid_alive(pid):
        pid += 1
    return pid


class _Board:
    """A scratch board whose worker logs and worker transcripts live under tmp."""

    def __init__(self, conn, home: Path):
        self.conn = conn
        self.home = home
        self.logs = home / "kanban" / "logs"
        self.logs.mkdir(parents=True, exist_ok=True)
        self.state_db = home / "state.db"
        self._init_state_db()

    def _init_state_db(self) -> None:
        """The two tables the dispatcher reads, with the real column names."""
        conn = sqlite3.connect(self.state_db)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, message_count INTEGER, "
            "tool_call_count INTEGER, started_at REAL, ended_at REAL)")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "session_id TEXT, role TEXT, timestamp REAL)")
        conn.commit()
        conn.close()

    # --- the worker transcript -----------------------------------------------------------
    def transcript(self, session_id: str, *, last_message_age: float, messages: int = 12) -> None:
        """A session whose last message is ``last_message_age`` seconds old."""
        stamp = time.time() - float(last_message_age)
        conn = sqlite3.connect(self.state_db)
        conn.execute(
            "INSERT OR REPLACE INTO sessions (id, message_count, tool_call_count, started_at) "
            "VALUES (?, ?, ?, ?)", (session_id, messages, messages // 2, stamp - 600))
        conn.execute(
            "INSERT INTO messages (session_id, role, timestamp) VALUES (?, 'assistant', ?)",
            (session_id, stamp))
        conn.commit()
        conn.close()

    def no_transcript(self, session_id: str) -> None:
        conn = sqlite3.connect(self.state_db)
        conn.execute(
            "INSERT OR REPLACE INTO sessions (id, message_count, tool_call_count, started_at) "
            "VALUES (?, 0, 0, ?)", (session_id, time.time() - 600))
        conn.commit()
        conn.close()

    # --- seeding -------------------------------------------------------------------------
    def running_card(
        self,
        tid: str,
        *,
        pid: int,
        fingerprint=None,
        log_age_seconds: float | None = None,
        log_missing: bool = False,
        log_session: str | None = None,
        started_at: int | None = None,
        heartbeat_age: int | None = 30,
        priority: int = 5,
        assignee: str = "default",
    ) -> int:
        """A ``running`` card with a live worker, its log, and its run row."""
        started = started_at if started_at is not None else NOW - 7200
        expires = NOW + 3600
        run_id = self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, ?, 'running', ?, ?, ?, ?)",
            (tid, assignee, LOCK, expires, pid, started),
        ).lastrowid
        self.conn.execute(
            "INSERT INTO tasks (id, title, assignee, status, created_at, claim_lock, "
            "claim_expires, worker_pid, worker_started_at, last_heartbeat_at, "
            "current_run_id, started_at, priority) "
            "VALUES (?, ?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tid, tid, assignee, NOW - 86400, LOCK, expires, pid, fingerprint,
                (NOW - heartbeat_age) if heartbeat_age is not None else None,
                run_id, started, priority,
            ),
        )
        self.conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, 'claimed', ?, ?)",
            (tid, run_id, json.dumps({"source_status": "ready"}), started),
        )
        self.conn.commit()
        if not log_missing:
            self.write_log(tid, age_seconds=log_age_seconds or 0, session=log_session)
        return int(run_id)

    def write_log(self, tid: str, *, age_seconds: float, session: str | None = None) -> Path:
        path = self.logs / f"{tid}.log"
        body = f"Session: {session}\n" if session else "worker started\n"
        path.write_text(body, encoding="utf-8")
        stamp = time.time() - float(age_seconds)
        os.utime(path, (stamp, stamp))
        return path

    def ready_card(self, tid: str, *, priority: int = 5, assignee: str = "default") -> str:
        row = kb.create_task(conn=self.conn, title=tid, assignee=assignee, priority=priority)
        with kb.write_txn(self.conn):
            self.conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (row,))
        self.conn.commit()
        return row

    # --- reading -------------------------------------------------------------------------
    def state(self, tid: str) -> dict:
        return dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())

    def run(self, tid: str) -> dict:
        return dict(self.conn.execute(
            "SELECT * FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (tid,)).fetchone())

    def event(self, tid: str, kind: str) -> dict:
        row = self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? "
            "ORDER BY id DESC LIMIT 1", (tid, kind)).fetchone()
        return json.loads(row["payload"]) if row else {}


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "kanban").mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    path = home / "kanban.db"
    kbc.init_db(path)
    conn = kbc.connect(path)
    # The host's live memory pressure must not cap the tick under test.
    monkeypatch.setattr(kbd, "_system_memory_sample", dict)
    scratch = _Board(conn, home)
    # The worker transcript lives in the PROFILE's state.db; point the resolver
    # at this scratch copy. Sibling tests purge ``hermes_cli`` from
    # ``sys.modules``, so the dispatcher may import a NEWER module object than
    # the one this file imported — patch every object the resolver could be
    # reached through.
    for mod in {importlib.import_module("hermes_cli.kanban_worker_liveness"), liveness}:
        monkeypatch.setattr(mod, "_profile_state_db", lambda _assignee: scratch.state_db)
    try:
        yield scratch
    finally:
        conn.close()


def _stub_cpu(monkeypatch, deltas: dict[int, float]) -> list[list[int]]:
    """Pin the CPU window: the CPU veto is decided here.

    Sibling test modules purge ``hermes_cli`` from ``sys.modules`` at import
    time, so the dispatcher can reach a DIFFERENT module object than the one
    this file imported. Patch every object it could be reached through.
    """
    calls: list[list[int]] = []

    def _fake(pids, window_seconds):
        pids = list(pids)
        calls.append(pids)
        return {pid: deltas[pid] for pid in pids if pid in deltas}

    for mod in {importlib.import_module("hermes_cli.kanban_worker_liveness"), liveness}:
        monkeypatch.setattr(mod, "sample_cpu_deltas", _fake)
    return calls


# --- 1. the dispatcher reclaims the parked worker itself ---------------------------------

def test_a_parked_worker_is_reclaimed_by_the_dispatcher(board, monkeypatch):
    """Alive, heartbeating, no output for an hour: the dispatcher ends it, with the measurement."""
    child = _idle_child()
    try:
        board.transcript("local-parked", last_message_age=7200)
        board.running_card(
            "t_parked", pid=child.pid,
            fingerprint=kbd._process_fingerprint(child.pid),
            log_age_seconds=7200, log_session="local-parked", heartbeat_age=5,
        )
        _stub_cpu(monkeypatch, {child.pid: 0.0})

        reclaimed = kbd.detect_parked_workers(
            board.conn, idle_seconds=3600, cpu_sample_seconds=0.01)

        assert reclaimed == ["t_parked"]
        row = board.state("t_parked")
        assert row["status"] == "ready"
        assert row["claim_lock"] is None
        assert row["worker_pid"] is None
        run = board.run("t_parked")
        assert run["outcome"] == "parked"
        assert "no output for" in (run["error"] or "")
        # The measurement travels with the event, so an operator can see WHY.
        payload = board.event("t_parked", "parked")
        assert payload["transcript_idle_seconds"] >= 3600
        assert payload["log_idle_seconds"] >= 3600
        assert payload["session_id"] == "local-parked"
        assert payload["pid"] == child.pid
        # The worker is gone: the slot is really free.
        assert not _pid_alive(child.pid)
    finally:
        child.kill()
        child.wait()


def test_a_worker_whose_transcript_is_advancing_is_never_reclaimed(board, monkeypatch):
    """The transcript is the progress signal: a worker that wrote a minute ago is working."""
    child = _idle_child()
    try:
        board.transcript("local-working", last_message_age=60)
        board.running_card(
            "t_working", pid=child.pid, log_age_seconds=7200,
            log_session="local-working", heartbeat_age=5)
        _stub_cpu(monkeypatch, {child.pid: 0.0})

        assert kbd.detect_parked_workers(
            board.conn, idle_seconds=3600, cpu_sample_seconds=0.01) == []
        assert board.state("t_working")["status"] == "running"
    finally:
        child.kill()
        child.wait()


def test_a_worker_whose_log_moved_recently_is_never_reclaimed(board, monkeypatch):
    """The log is the SECOND gate: a recent write means the worker produced output."""
    child = _idle_child()
    try:
        board.transcript("local-logfresh", last_message_age=7200)
        board.running_card(
            "t_logfresh", pid=child.pid, log_age_seconds=30,
            log_session="local-logfresh", heartbeat_age=5)
        _stub_cpu(monkeypatch, {child.pid: 0.0})

        assert kbd.detect_parked_workers(
            board.conn, idle_seconds=3600, cpu_sample_seconds=0.01) == []
        assert board.state("t_logfresh")["status"] == "running"
    finally:
        child.kill()
        child.wait()


def test_a_worker_that_is_burning_cpu_is_never_reclaimed(board, monkeypatch):
    """CPU is a veto: a worker visibly burning cycles is working, whatever else says."""
    child = _idle_child()
    try:
        board.transcript("local-busy", last_message_age=7200)
        board.running_card(
            "t_busy", pid=child.pid, log_age_seconds=7200,
            log_session="local-busy", heartbeat_age=5)
        _stub_cpu(monkeypatch, {child.pid: 3.5})

        assert kbd.detect_parked_workers(
            board.conn, idle_seconds=3600, cpu_sample_seconds=0.01) == []
        assert board.state("t_busy")["status"] == "running"
    finally:
        child.kill()
        child.wait()


def test_a_parked_worker_is_still_reclaimed_when_the_host_cannot_measure_cpu(board, monkeypatch):
    """Cross-process CPU accounting is unavailable on some hosts. That must not disable the
    reclaim — it must only remove the veto."""
    child = _idle_child()
    try:
        board.transcript("local-nocpu", last_message_age=7200)
        board.running_card(
            "t_nocpu", pid=child.pid, log_age_seconds=7200,
            log_session="local-nocpu", heartbeat_age=5)
        # The CPU window sees nothing: the counter could not be read.
        _stub_cpu(monkeypatch, {})

        assert kbd.detect_parked_workers(
            board.conn, idle_seconds=3600, cpu_sample_seconds=0.01) == ["t_nocpu"]
        payload = board.event("t_nocpu", "parked")
        assert payload["cpu_delta_seconds"] is None
    finally:
        child.kill()
        child.wait()


def test_a_worker_the_dispatcher_cannot_measure_is_never_reclaimed(board, monkeypatch):
    """Unknown is never idle: no session line, no transcript, no log — hands off in every case."""
    child = _idle_child()
    try:
        # No session id in the log: the card cannot be mapped to a transcript.
        board.running_card(
            "t_nosession", pid=child.pid, log_age_seconds=7200, log_session=None,
            heartbeat_age=5)
        # A session id, but no readable transcript.
        board.no_transcript("local-gone")
        board.running_card(
            "t_notranscript", pid=child.pid, log_age_seconds=7200,
            log_session="local-gone", heartbeat_age=5)
        # No log at all.
        board.transcript("local-nolog", last_message_age=7200)
        board.running_card(
            "t_nolog", pid=child.pid, log_missing=True, log_session="local-nolog",
            heartbeat_age=5)
        _stub_cpu(monkeypatch, {child.pid: 0.0})

        assert kbd.detect_parked_workers(
            board.conn, idle_seconds=3600, cpu_sample_seconds=0.01) == []
        assert board.state("t_nosession")["status"] == "running"
        assert board.state("t_notranscript")["status"] == "running"
        assert board.state("t_nolog")["status"] == "running"
    finally:
        child.kill()
        child.wait()


def test_one_cpu_window_is_shared_by_every_candidate(board, monkeypatch):
    """The window is paid once per tick, not once per card."""
    children = [_idle_child(), _idle_child()]
    try:
        for index, child in enumerate(children):
            board.transcript(f"local_c{index}", last_message_age=7200)
            board.running_card(
                f"t_c{index}", pid=child.pid, log_age_seconds=7200,
                log_session=f"local_c{index}", heartbeat_age=5)
        calls = _stub_cpu(monkeypatch, {c.pid: 0.0 for c in children})

        reclaimed = kbd.detect_parked_workers(
            board.conn, idle_seconds=3600, cpu_sample_seconds=0.01)

        assert sorted(reclaimed) == ["t_c0", "t_c1"]
        assert len(calls) == 1
        assert sorted(calls[0]) == sorted(c.pid for c in children)
    finally:
        for child in children:
            child.kill()
            child.wait()


def test_a_parked_reclaim_is_not_charged_to_the_cards_failure_budget(board, monkeypatch):
    """A parked worker is an infrastructure hang, not the card's fault: the breaker must not
    trip on it and block a healthy card."""
    child = _idle_child()
    try:
        board.transcript("local-budget", last_message_age=7200)
        board.running_card(
            "t_parked", pid=child.pid, log_age_seconds=7200,
            log_session="local-budget", heartbeat_age=5)
        _stub_cpu(monkeypatch, {child.pid: 0.0})

        kbd.detect_parked_workers(
            board.conn, idle_seconds=3600, cpu_sample_seconds=0.01)

        row = board.state("t_parked")
        assert row["status"] == "ready"
        assert row["consecutive_failures"] == 0
    finally:
        child.kill()
        child.wait()


def test_the_parked_reclaim_can_be_switched_off(board, monkeypatch):
    """0 disables the check: an operator who wants the old behaviour gets it."""
    child = _idle_child()
    try:
        board.transcript("local-off", last_message_age=7200)
        board.running_card(
            "t_parked", pid=child.pid, log_age_seconds=7200,
            log_session="local-off", heartbeat_age=5)
        _stub_cpu(monkeypatch, {child.pid: 0.0})

        assert kbd.detect_parked_workers(
            board.conn, idle_seconds=0, cpu_sample_seconds=0.01) == []
        assert board.state("t_parked")["status"] == "running"
    finally:
        child.kill()
        child.wait()


# --- 2. the freed slot goes to the highest-priority ready card ----------------------------

def test_the_freed_slot_goes_to_the_highest_priority_ready_card(board, monkeypatch):
    """The card with real work stops waiting behind a worker that will never do any."""
    child = _idle_child()
    try:
        board.transcript("local-slot", last_message_age=7200)
        board.running_card(
            "t_parked", pid=child.pid, log_age_seconds=7200,
            log_session="local-slot", heartbeat_age=5, priority=5)
        high = board.ready_card("t_high", priority=90)
        low = board.ready_card("t_low", priority=10)
        _stub_cpu(monkeypatch, {child.pid: 0.0})

        spawned: list[str] = []

        def _spawn(task, workspace, board=None):
            spawned.append(task.id)
            return 999000 + len(spawned)

        res = kbd.dispatch_once(
            board.conn, spawn_fn=_spawn, max_spawn=1, ttl_seconds=900)

        assert res.parked == ["t_parked"]
        assert spawned == [high]
        assert low not in spawned
        # The slot the parked worker held is the one the priority card took.
        assert res.spawn_suppressed == []
    finally:
        child.kill()
        child.wait()


# --- 3. a card with no ceiling of its own is bounded ---------------------------------------

def test_a_card_without_a_ceiling_is_bounded_by_the_default(board):
    """``max_runtime_seconds`` NULL meant "unbounded" — a card that could hold its slot forever."""
    board.running_card(
        "t_norun", pid=_absent_pid(), fingerprint=None,
        log_missing=True, started_at=NOW - 7200, heartbeat_age=5)
    board.running_card(
        "t_own", pid=_absent_pid(), fingerprint=None,
        log_missing=True, started_at=NOW - 7200, heartbeat_age=5)
    board.conn.execute(
        "UPDATE tasks SET max_runtime_seconds = 86400 WHERE id = 't_own'")
    board.conn.commit()

    assert kbd.enforce_max_runtime(
        board.conn, default_seconds=3600, signal_fn=lambda *_: None) == ["t_norun"]
    assert board.state("t_norun")["status"] == "ready"
    # A card that carries its own ceiling keeps it — the default only fills the gap.
    assert board.state("t_own")["status"] == "running"


def test_no_default_means_no_ceiling(board):
    """0 (or unset) keeps the old unbounded behaviour — the default is opt-out, not forced."""
    board.running_card(
        "t_norun", pid=_absent_pid(), fingerprint=None,
        log_missing=True, started_at=NOW - 999999, heartbeat_age=5)

    assert kbd.enforce_max_runtime(
        board.conn, default_seconds=None, signal_fn=lambda *_: None) == []
    assert board.state("t_norun")["status"] == "running"


def test_a_claimed_card_carries_the_default_ceiling(board):
    """The bound is visible on the card, not just in the dispatcher's head."""
    high = board.ready_card("t_high", priority=90)

    res = kbd.dispatch_once(
        board.conn, spawn_fn=lambda *a, **k: 999001, max_spawn=2,
        ttl_seconds=900, default_max_runtime_seconds=1800)

    assert [tid for tid, _who, _ws in res.spawned] == [high]
    assert board.state(high)["max_runtime_seconds"] == 1800


# --- 4. the dispatcher says WHY it spawned nothing ----------------------------------------

def test_the_dispatcher_names_the_concurrency_cap_that_stopped_it(board):
    """``--max 1`` with one card running spawns nothing — and now says so."""
    child = _idle_child()
    try:
        board.running_card(
            "t_running", pid=child.pid, log_age_seconds=5, heartbeat_age=5)

        res = kbd.dispatch_once(board.conn, dry_run=True, max_spawn=1)

        assert res.spawned == []
        keys = [key for key, _text in res.spawn_suppressed]
        assert "max_spawn_cap" in keys
        text = dict(res.spawn_suppressed)["max_spawn_cap"]
        assert "LIVE concurrency cap" in text
        assert "not a per-tick budget" in text
        # And the summary line the gateway's "dispatcher stuck" warning prints.
        assert "max_spawn_cap=1" in kbd.describe_suppression([res])
    finally:
        child.kill()
        child.wait()


def test_the_dispatcher_names_the_host_cap_that_stopped_it(board):
    """The host-level cap is named too — it is the one that starves a whole board."""
    child = _idle_child()
    try:
        board.running_card(
            "t_running", pid=child.pid, log_age_seconds=5, heartbeat_age=5)

        res = kbd.dispatch_once(board.conn, dry_run=True, max_in_progress=1)

        keys = [key for key, _text in res.spawn_suppressed]
        assert "max_in_progress_cap" in keys
        assert "max_in_progress_cap=1" in kbd.describe_suppression([res])
    finally:
        child.kill()
        child.wait()


def test_the_max_flag_help_text_says_it_is_a_concurrency_cap():
    """The flag's help contradicted the implementation and cost a debugging cycle."""
    from hermes_cli import kanban_parser

    root = argparse.ArgumentParser()
    subparsers = root.add_subparsers(dest="cmd")
    kanban = kanban_parser.build_parser(subparsers)
    dispatch = None
    for action in kanban._subparsers._group_actions:
        for name, sub in (getattr(action, "choices", {}) or {}).items():
            if name == "dispatch":
                dispatch = sub
    assert dispatch is not None
    text = " ".join(dispatch.format_help().split())
    assert "Cap number of spawns this pass" not in text
    assert "concurrency cap" in text
    assert "RUNNING" in text


# --- the liveness signals themselves --------------------------------------------------------

def test_the_session_id_comes_from_the_worker_own_log(board):
    """The worker declares its session; the dispatcher reads it from the log."""
    board.write_log("t_x", age_seconds=0, session="local-abc123")
    assert liveness.newest_worker_session_id("t_x") == "local-abc123"
    # The NEWEST line wins: the log is appended to across attempts.
    path = board.logs / "t_x.log"
    path.write_text("Session: local-old\nnoise\nSession: local-new\n", encoding="utf-8")
    assert liveness.newest_worker_session_id("t_x") == "local-new"


def test_a_missing_worker_log_measures_nothing(board):
    """No log means no 'no output' measurement — the caller must not treat it as idle."""
    assert liveness.worker_log_idle_seconds("t_absent") is None
    assert liveness.newest_worker_session_id("t_absent") is None


def test_the_transcript_progress_is_read_from_the_profile_state_db(board):
    """The transcript is the progress signal: it reports when the worker last wrote."""
    board.transcript("local-live", last_message_age=42, messages=7)
    progress = liveness.worker_transcript_progress("default", "local-live")
    assert progress is not None
    assert progress.session_id == "local-live"
    assert progress.messages == 7
    assert 30 <= progress.idle_seconds() <= 90
    # An unknown session measures nothing.
    assert liveness.worker_transcript_progress("default", "local-nope") is None


def test_the_profile_state_db_resolves_under_the_profiles_root():
    """The dispatcher reads the PROFILE's own state.db — that is where the worker's turn lives."""
    from hermes_cli import kanban_worker_liveness as lv
    from hermes_cli.profiles import get_profile_dir

    # A named profile lives under <root>/profiles/<name>/; "default" IS the root home.
    named = lv._profile_state_db("sentinel")
    assert named is not None
    assert named == get_profile_dir("sentinel") / "state.db"
    assert named.name == "state.db"
    assert named.parent.name == "sentinel"
    assert named.parent.parent.name == "profiles"

    default = lv._profile_state_db("default")
    assert default is not None
    assert default == get_profile_dir("default") / "state.db"
    assert default.name == "state.db"


def test_an_unreadable_pid_is_absent_from_the_cpu_sample():
    """A pid that cannot be read is ABSENT, never zero: unknown is not idle."""
    assert liveness.sample_cpu_deltas([_absent_pid()], 0.01) == {}
