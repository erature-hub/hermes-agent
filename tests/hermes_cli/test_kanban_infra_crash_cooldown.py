"""An infrastructure loss booked as a CRASH must be SPACED, not re-spawned on the next tick.

Regression for the 2026-11-11 hot retry loop (#t_45f5d3c4). The managed worker's boot
outran the owner's handshake deadline, so the attempt never bound. The booking was already
right in the important sense — it is INFRASTRUCTURE, so the claim is released, the card
stays ``ready``, and the failure budget is never charged (#t_853294b3) — but the card was
re-spawned on the very next dispatcher tick:

    11:09:01 crashed -> 11:09:02 claimed -> 11:09:02 spawned

Sixteen consecutive attempts in forty-five minutes, each one burning a worker slot to
learn nothing new, because a host too slow to boot one worker is too slow to boot the
next one too. The card only moved on when the host happened to catch up.

The respawn guard already spaces an infrastructure loss that booked as ``spawn_failed``
(``infrastructure_cooldown``). It did not see the infrastructure loss that booked as
``crashed``, which is the shape this outage actually produced.

These tests pin the spacing:

  C1  an infrastructure-classified CRASH is held by ``infrastructure_cooldown``;
  C2  a REAL crash (the worker bound, so the card's work ran) is NOT held — it must
      still re-spawn promptly and still charge the breaker;
  C3  the cooldown expires: after it elapses the card re-spawns normally;
  C4  the spacing is what breaks the loop: N consecutive infrastructure crashes produce
      ONE spawn inside the cooldown window, not N.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import psutil
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

NOW = int(time.time())
HOST = kb._claimer_id().split(":", 1)[0]
FINGERPRINT = "6a232414-2dbf-40ab-a534-d9b71db05658:116|4162240"
COOLDOWN = 300


def _absent_pid(base: int = 3_800_000) -> int:
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


@pytest.fixture
def board(tmp_path, monkeypatch):
    """An isolated HERMES_HOME board plus the owner ledger a receipt points at."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setenv("HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS", str(COOLDOWN))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    conn = kbc.connect()
    ledger = tmp_path / "state.db"
    with sqlite3.connect(ledger) as store:
        store.execute(
            "CREATE TABLE session_admissions "
            "(target_session_id TEXT, request_id TEXT, status TEXT, outcome TEXT)"
        )
    scratch = _Board(conn, ledger)
    yield scratch
    conn.close()


class _Board:
    def __init__(self, conn, ledger: Path):
        self.conn = conn
        self.ledger = ledger

    def running(
        self, tid: str = "t_slowboot", *, worker_pid=None, claim_expires_in: int = 900,
        heartbeat_age=None, owner: bool = True, admission=("terminal", "failed"),
        worker_lost=None, dead_seconds: int = 120, bound: bool = False,
        infrastructure: bool = False, ended_at: int | None = None,
    ) -> int:
        """A ``running`` card whose managed worker died.

        ``bound=False`` (the default) is the outage's shape: the owner admitted the attempt
        and no interpreter ever introduced itself.

        The card row is REUSED when it already exists: the real loop re-claims the SAME card
        over and over, so the fixture must be able to put it back to ``running``.
        """
        worker_pid = _absent_pid() if worker_pid is None else int(worker_pid)
        lock = f"{HOST}:{_absent_pid(base=3_900_000)}"
        started = NOW - dead_seconds
        exists = self.conn.execute(
            "SELECT 1 FROM tasks WHERE id = ?", (tid,)).fetchone() is not None
        run_id = int(self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'fan', 'running', ?, ?, ?, ?)",
            (tid, lock, NOW + claim_expires_in, worker_pid, started),
        ).lastrowid)
        if exists:
            # The card is re-claimed for another attempt: reset the row to ``running``.
            self.conn.execute(
                "UPDATE tasks SET status = 'running', claim_lock = ?, claim_expires = ?, "
                "worker_pid = ?, worker_started_at = ?, last_heartbeat_at = ?, "
                "current_run_id = ?, started_at = ?, consecutive_failures = 0, "
                "last_failure_error = NULL WHERE id = ?",
                (lock, NOW + claim_expires_in, worker_pid, FINGERPRINT,
                 (NOW - heartbeat_age) if heartbeat_age is not None else None, run_id,
                 started, tid),
            )
        else:
            self.conn.execute(
                "INSERT INTO tasks (id, title, assignee, status, created_at, claim_lock, claim_expires, "
                "worker_pid, worker_started_at, last_heartbeat_at, current_run_id, started_at, priority) "
                "VALUES (?, ?, 'fan', 'running', ?, ?, ?, ?, ?, ?, ?, ?, 5)",
                (tid, tid, NOW - 86400, lock, NOW + claim_expires_in,
                 worker_pid, FINGERPRINT,
                 (NOW - heartbeat_age) if heartbeat_age is not None else None, run_id, started),
            )
        self.event(tid, run_id, "claimed", {"lock": lock, "expires": NOW + claim_expires_in}, started)
        self.event(tid, run_id, "spawned", {"pid": worker_pid, "started_at": FINGERPRINT}, started)
        if owner:
            request_id = f'kanban:["default","{tid}",{run_id}]'
            self.event(tid, run_id, "owner_admitted",
                       {"db": str(self.ledger), "session_id": "sess-1",
                        "pid": _absent_pid(base=3_950_000), "request_id": request_id}, started + 3)
            if admission:
                with sqlite3.connect(self.ledger) as store:
                    store.execute("INSERT INTO session_admissions VALUES ('sess-1', ?, ?, ?)",
                                  (request_id, admission[0], admission[1]))
        if bound:
            self.event(tid, run_id, "worker_bound",
                       {"pid": worker_pid, "claim_lock": lock, "started_at": FINGERPRINT}, started + 30)
        if worker_lost:
            lost = {"reason": worker_lost, "claim_lock": lock, "run_id": run_id,
                    "pid": worker_pid, "bound": bound}
            if infrastructure:
                # The owner's own note: this loss is the HOST's condition (the boot never
                # finished), so the attempt never began the card's work.
                lost["infrastructure"] = True
                lost["elapsed_s"] = 60
            self.event(tid, run_id, "worker_lost", lost, started + 24)
        self.conn.commit()
        return run_id

    def event(self, tid: str, run: int, kind: str, payload: dict, ts: int) -> None:
        self.conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, ?, ?, ?, ?)", (tid, run, kind, json.dumps(payload), ts))

    def state(self, tid: str) -> dict:
        return dict(self.conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())

    def run(self, run_id: int) -> dict:
        return dict(self.conn.execute("SELECT * FROM task_runs WHERE id = ?", (run_id,)).fetchone())

    def tick(self, *, failure_limit: int | None = None) -> kbd.DispatchResult:
        """One real dispatcher tick: reclaim phase + promotion, no spawn slot."""
        return kbd.dispatch_once(self.conn, max_spawn=0, failure_limit=failure_limit,
                                 stale_timeout_seconds=0)


# --- C1: the infrastructure crash is held by the cooldown ------------------------------

def test_an_infrastructure_crash_is_spaced_by_the_cooldown(board):
    """C1: the loss the host caused is held; the card does not re-spawn on the next tick."""
    board.running("t_slowboot", worker_lost="managed_worker_hello_timeout", infrastructure=True)
    board.tick(failure_limit=2)

    # The card is retryable and was never charged a failure — that part already worked.
    row = board.state("t_slowboot")
    assert row["status"] == "ready", "an infrastructure loss releases the card"
    assert row["consecutive_failures"] == 0, "an infrastructure loss never charges the breaker"

    # The run booked it as a crash carrying the infrastructure marker.
    run = board.conn.execute(
        "SELECT outcome, metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        ("t_slowboot",),
    ).fetchone()
    assert run["outcome"] == "crashed"
    assert json.loads(run["metadata"]).get("infrastructure") is True, \
        "the crash must carry the infrastructure marker the guard reads"

    # And the guard now spaces it.
    assert kbd.check_respawn_guard(board.conn, "t_slowboot", lane="ready") == "infrastructure_cooldown"


def test_an_infrastructure_spawn_failure_is_still_spaced(board):
    """The pre-existing shape keeps working: a host-refused spawn is spaced too."""
    run_id = board.running("t_slowboot", owner=False)
    board.conn.execute(
        "UPDATE task_runs SET outcome = 'spawn_failed', ended_at = ? WHERE id = ?",
        (int(time.time()), run_id),
    )
    board.conn.execute(
        "UPDATE task_runs SET metadata = ? WHERE id = ?",
        (json.dumps({"infrastructure": True}), run_id),
    )
    board.conn.commit()
    assert kbd.check_respawn_guard(board.conn, "t_slowboot", lane="ready") == "infrastructure_cooldown"


# --- C2: a REAL crash is untouched -----------------------------------------------------

def test_a_real_crash_is_not_spaced(board):
    """C2: a worker that BOUND did the card's work; its crash must re-spawn promptly."""
    board.running("t_slowboot", worker_lost="managed_worker_failed", bound=True)
    board.tick(failure_limit=2)

    row = board.state("t_slowboot")
    assert row["consecutive_failures"] == 1, "a real crash still charges the breaker"
    run = board.conn.execute(
        "SELECT outcome, metadata FROM task_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1",
        ("t_slowboot",),
    ).fetchone()
    assert json.loads(run["metadata"]).get("infrastructure") is None, \
        "a bound worker's crash is not an infrastructure loss"
    assert kbd.check_respawn_guard(board.conn, "t_slowboot", lane="ready") != "infrastructure_cooldown"


# --- C3: the cooldown expires ----------------------------------------------------------

def test_the_card_re_spawns_once_the_cooldown_elapses(board):
    """C3: the hold is a delay, not a park — the card retries when the host has had its rest."""
    run_id = board.running("t_slowboot", worker_lost="managed_worker_hello_timeout", infrastructure=True)
    board.tick(failure_limit=2)
    assert kbd.check_respawn_guard(board.conn, "t_slowboot", lane="ready") == "infrastructure_cooldown"

    # The run ends now, as it would once the cooldown has passed.
    ended = int(time.time()) - COOLDOWN - 10
    board.conn.execute(
        "UPDATE task_runs SET ended_at = ? WHERE id = ?", (ended, run_id),
    )
    board.conn.commit()
    assert kbd.check_respawn_guard(board.conn, "t_slowboot", lane="ready") != "infrastructure_cooldown", \
        "after the cooldown the card must be free to run again"


# --- C4: the spacing breaks the loop ----------------------------------------------------

def test_consecutive_infrastructure_crashes_do_not_hot_loop(board):
    """C4: the loop this fix exists for — N failures inside the window produce ONE spawn.

    Before the fix each crash re-spawned the card on the very next tick, so a host that
    could not boot a worker burned one every ~2.8 minutes forever. The guard now holds the
    card for the cooldown, so the failures land but the card does not churn.
    """
    spawned = []
    for attempt in range(5):
        # Each attempt: the card is running, its worker dies before binding. The card keeps
        # its id across attempts — that IS the loop (the same card, re-claimed each time).
        board.running("t_slowboot", worker_lost="managed_worker_hello_timeout",
                      infrastructure=True)
        board.tick(failure_limit=2)
        # Promotion would spawn it; the guard is what must say no.
        if kbd.check_respawn_guard(board.conn, "t_slowboot", lane="ready") is None:
            spawned.append(attempt)

    assert spawned == [], \
        "an infrastructure loss must be held inside the cooldown window, not re-spawned"


def test_the_breaker_still_never_trips_on_infrastructure_losses(board):
    """The card stays retryable FOREVER: an infrastructure loss is the host's, not the card's."""
    for _ in range(6):
        board.running("t_slowboot", worker_lost="managed_worker_hello_timeout", infrastructure=True)
        board.tick(failure_limit=2)
    row = board.state("t_slowboot")
    assert row["consecutive_failures"] == 0, "infrastructure losses never charge the breaker"
    assert row["status"] == "ready", "the card is never blocked by a host condition"
