"""A worker that never bound must RELEASE the card, not spend its failure budget.

Regression for the 2026-11-01 fleet-wide outage (#t_853294b3): every lane's managed worker
was admitted and then died having produced nothing. The cause was the managed worker's BOOT:
it runs the PM dependency probe before it can introduce itself, and on a slow host that probe
alone outran the owner's 60s handshake deadline. The worker exited 1 with no output, the
dispatcher booked the crash, and at two failures the breaker blocked the card — so a card that
had never started was reported as one that had failed twice.

These tests pin the disposition the board owes an attempt that never bound:

  B1  an admitted-but-never-bound worker is booked as INFRASTRUCTURE: the claim is released
      and the card stays ``ready``;
  B2  it does NOT consume the card's failure budget, so the breaker never trips on it
      (one slow boot must not block the card it never started);
  B3  the run error names the deadline that ran out and the observed admission->exit gap,
      so the board says WHY instead of a bare exit code;
  B4  a worker that DID bind keeps the old booking: it is a real crash that charges the
      breaker, because its card's work actually ran;
  B5  the owner's own ``worker_lost`` note, when it names an infrastructure loss, books the
      same way even if the board's fence evidence is gone.
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
    ) -> int:
        """A ``running`` card whose managed worker died.

        ``bound=False`` (the default) is the outage's shape: the owner admitted the attempt
        and no interpreter ever introduced itself.
        """
        worker_pid = _absent_pid() if worker_pid is None else int(worker_pid)
        lock = f"{HOST}:{_absent_pid(base=3_900_000)}"
        started = NOW - dead_seconds
        run_id = int(self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, claim_lock, claim_expires, "
            "worker_pid, started_at) VALUES (?, 'fan', 'running', ?, ?, ?, ?)",
            (tid, lock, NOW + claim_expires_in, worker_pid, started),
        ).lastrowid)
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
            self.event(tid, run_id, "worker_lost",
                       {"reason": worker_lost, "claim_lock": lock, "run_id": run_id,
                        "pid": worker_pid, "bound": bound}, started + 24)
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


# --- B1/B2/B3: a never-bound worker releases the card instead of failing it --------------

def test_a_never_bound_worker_releases_the_card_instead_of_failing_it(board):
    """B1/B2: the claim is released and the card stays retryable — no failure charged.

    The card's work never began, so it must not be reported as a card that failed.
    """
    run_id = board.running("t_slowboot", claim_expires_in=900, heartbeat_age=30)
    board.tick(failure_limit=2)
    row, run = board.state("t_slowboot"), board.run(run_id)
    assert row["status"] == "ready"                       # released, not blocked
    assert (row["claim_lock"], row["worker_pid"], row["current_run_id"]) == (None, None, None)
    assert row["consecutive_failures"] == 0               # B2: the card's budget is untouched
    assert run["outcome"] == "crashed"                    # the run really did die
    assert json.loads(run["metadata"] or "{}").get("infrastructure") is True


def test_a_never_bound_worker_never_trips_the_breaker(board):
    """B2: repeated slow boots must not block the card they never started.

    The dispatcher's breaker gives up at two failures, which is what blocked the card the
    outage caught. A slow host can slow many boots; none of them ran the card.
    """
    for attempt in range(4):
        board.running(f"t_slow{attempt}", claim_expires_in=900, heartbeat_age=30)
        board.tick(failure_limit=2)
        row = board.state(f"t_slow{attempt}")
        assert row["status"] == "ready"
        assert row["consecutive_failures"] == 0
    kinds = [r["kind"] for r in board.conn.execute(
        "SELECT kind FROM task_events WHERE task_id = 't_slow0' ORDER BY id")]
    assert "gave_up" not in kinds


def test_the_run_error_names_the_deadline_the_worker_missed(board):
    """B3: the board says WHICH deadline ran out and how long the boot took."""
    run_id = board.running("t_slowboot", claim_expires_in=900, heartbeat_age=30)
    board.tick(failure_limit=2)
    error = board.run(run_id)["error"]
    assert "never introduced itself" in error
    assert "handshake deadline" in error
    # The admission->exit gap is measured, not guessed.
    assert "s after admission" in error
    metadata = json.loads(board.run(run_id)["metadata"] or "{}")
    assert metadata["admitted_to_exit_s"] >= 0


# --- B4: a worker that DID bind keeps the old booking ------------------------------------

def test_a_worker_that_bound_is_still_a_crash_that_charges_the_breaker(board):
    """B4: a bound worker ran the card's work, so its death is the card's failure."""
    run_id = board.running("t_bound", claim_expires_in=900, heartbeat_age=30, bound=True)
    board.tick(failure_limit=2)
    row, run = board.state("t_bound"), board.run(run_id)
    assert row["status"] == "ready"
    assert row["consecutive_failures"] == 1               # the breaker still sees it
    assert json.loads(run["metadata"] or "{}").get("infrastructure") is None


def test_a_worker_that_bound_without_an_owner_is_still_a_crash(board):
    """A plain dispatcher worker (no owner, no bind) keeps the old booking too."""
    run_id = board.running("t_plain", claim_expires_in=900, heartbeat_age=30, owner=False)
    board.tick(failure_limit=2)
    row, run = board.state("t_plain"), board.run(run_id)
    assert row["consecutive_failures"] == 1
    assert json.loads(run["metadata"] or "{}").get("infrastructure") is None


# --- B5: the owner's own infrastructure note books the same way --------------------------

def test_the_owners_infrastructure_note_books_the_same_way(board):
    """B5: the owner's note, when it names an infrastructure loss, releases the card.

    The owner's note is written off the owner loop and can arrive after the crash sweep has
    already read the board's fence evidence, so the note is the second, independent source.
    """
    run_id = board.running("t_noted", claim_expires_in=900, heartbeat_age=30, bound=True,
                            worker_lost="managed_worker_hello_timeout")
    # The note itself carries the infrastructure marker (the owner's path writes it).
    board.conn.execute(
        "UPDATE task_events SET payload = ? WHERE task_id = 't_noted' AND kind = 'worker_lost'",
        (json.dumps({"reason": "managed_worker_hello_timeout", "bound": False,
                     "infrastructure": True, "elapsed_s": 61}),))
    board.conn.commit()
    board.tick(failure_limit=2)
    row, run = board.state("t_noted"), board.run(run_id)
    assert row["status"] == "ready"
    assert row["consecutive_failures"] == 0
    assert json.loads(run["metadata"] or "{}").get("infrastructure") is True


def test_the_owners_note_of_a_real_failure_still_charges_the_breaker(board):
    """B5 (the other side): a note that names a real failure is NOT infrastructure.

    The owner's note says the worker HAD bound and then failed: the card's work ran, so the
    breaker must see it. Only a note that says the attempt never began is infrastructure.
    """
    run_id = board.running("t_real", claim_expires_in=900, heartbeat_age=30, bound=True,
                            worker_lost="managed_worker_failed")
    board.tick(failure_limit=2)
    row, run = board.state("t_real"), board.run(run_id)
    assert row["consecutive_failures"] == 1
    assert json.loads(run["metadata"] or "{}").get("infrastructure") is None
    assert "managed worker lost: managed_worker_failed" in run["error"]
