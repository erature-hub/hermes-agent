"""A worker whose boot misses its deadline is a host fault, not a card failure.

Regression for the ELENCHOS-DISPATCH defect (card t_526a50c4). A managed worker's boot on this
Windows host is the owner's cost — a cold PM dependency probe alone measured 17.9 s here and a
gateway-env boot 82-107 s — so a fixed handshake budget turns a loaded machine into a crashed
card: the worker exits 1 with no message, the dispatcher books an execution failure, and two of
those park a card that never ran anything. 68 of the board's runs carry exactly that signature
(``owner_admitted`` with no ``worker_bound``, all booked ``crashed``).

The contract this file pins down:

* the deadline scales with what the host has actually been paying
  (``max(OWNER_WAIT_S, 2 * p90 of the recent admit -> bind gaps)``),
* a missed deadline exits ``PREBIND_TIMEOUT_EXIT_CODE`` and says so in one diagnostic line,
* that exit releases the claim WITHOUT touching ``consecutive_failures`` or blocking the card,
  and repeated misses stay a spaced retry rather than a breaker trip,
* a worker that DID bind keeps its own exit code — the release path must never widen.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_client as kwc
from hermes_cli.kanban_worker_exit import PREBIND_TIMEOUT_EXIT_CODE
from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway Hermes root; the board lives inside it, never at the operator's."""
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kbd._recent_worker_exits.clear()
    kb.init_db()
    return root


def _db(home: Path) -> str:
    return str(kb.kanban_db_path())


def _seed_bind_gaps(home: Path, gaps: list[float]) -> None:
    """Write ``owner_admitted`` -> ``worker_bound`` pairs with the given gaps, newest last."""
    now = int(time.time())
    with kbc.connect() as conn:
        for i, gap in enumerate(gaps):
            # Oldest first, so the last row is the newest and anchors the window.
            admitted = now - 3600 + i
            for kind, payload, ts in (
                ("owner_admitted", "{}", admitted),
                ("worker_bound", "{}", admitted + int(gap)),
            ):
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (f"seed{i}", 90000 + i, kind, payload, ts),
                )


def _dead_worker_with_log(conn, tid: str, pid: int, rc: int) -> None:
    """Claim ``tid`` for a worker that already exited ``rc`` and left its exit trailer."""
    host = kb._claimer_id().split(":", 1)[0]
    kb.claim_task(conn, tid, claimer=f"{host}:w{pid}")
    conn.execute(
        "UPDATE tasks SET worker_pid=?, worker_started_at=NULL, started_at=? WHERE id=?",
        (pid, int(time.time()) - 120, tid),
    )
    conn.commit()
    log = kb.worker_log_path(tid)
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        f.write(f"{KANBAN_WORKER_EXIT_TRAILER}{rc}\n")


# --- the deadline itself ---------------------------------------------------------------


def test_the_deadline_is_never_tighter_than_the_budget_every_answer_already_had(home):
    """A healthy host keeps today's ``OWNER_WAIT_S``: scaling must not invent a shorter leash."""
    _seed_bind_gaps(home, [3.0] * 20)
    assert kwc.spawn_baseline(_db(home)) == kwc.spawn_baseline_prior()
    assert kwc.handshake_deadline(_db(home)) == kwc.OWNER_WAIT_S


def test_the_deadline_doubles_what_this_host_has_been_paying(home):
    """A degraded host (110 s boots, measured 2026-11-01) buys 220 s, not the 180 s that cut it."""
    _seed_bind_gaps(home, [110.0] * 20)
    assert kwc.spawn_baseline(_db(home)) == pytest.approx(110.0)
    assert kwc.handshake_deadline(_db(home)) == pytest.approx(220.0)


def test_a_host_whose_boots_half_miss_gets_the_slow_number(home):
    """Half the attempts crawling: the baseline follows the crawl, so the deadline does too."""
    _seed_bind_gaps(home, [3.0] * 10 + [100.0] * 10)
    assert kwc.spawn_baseline(_db(home)) == pytest.approx(100.0)
    assert kwc.handshake_deadline(_db(home)) == pytest.approx(200.0)


def test_one_pathological_boot_cannot_park_a_card_behind_a_twenty_minute_wait(home):
    """The ceiling keeps a pathological sample from making the deadline unbounded."""
    _seed_bind_gaps(home, [900.0] * 20)
    assert kwc.spawn_baseline(_db(home)) == kwc.SPAWN_BASELINE_CEILING_S
    assert kwc.handshake_deadline(_db(home)) == pytest.approx(kwc.SPAWN_BASELINE_CEILING_S * 2)


def test_the_platform_prior_is_data_not_a_faked_host():
    """Host-specific cost as a pure function of the platform name (no ``sys.platform`` patching)."""
    assert kwc.spawn_baseline_prior("win32") == kwc.SPAWN_BASELINE_PRIORS_S["win32"]
    assert kwc.spawn_baseline_prior("linux") == kwc.SPAWN_BASELINE_DEFAULT_S
    assert kwc.spawn_baseline_prior("plan9") == kwc.SPAWN_BASELINE_DEFAULT_S


def test_the_client_never_cuts_a_boot_the_owner_still_grants(home, monkeypatch):
    """The owner's own hello deadline is a floor: a tighter client budget is the same bug."""
    _seed_bind_gaps(home, [3.0] * 20)
    monkeypatch.setattr(kwc, "owner_hello_deadline", lambda: 240.0)
    assert kwc.handshake_deadline(_db(home)) == pytest.approx(240.0)


def test_the_owner_hello_deadline_is_read_from_the_owner(monkeypatch):
    """The real number, not a copy that can drift: whatever the owner grants is what we allow."""
    import sys as _sys
    module = _sys.modules.get("gateway.session_managed_worker")
    if module is None:
        pytest.importorskip("gateway.session_managed_worker")
        module = _sys.modules["gateway.session_managed_worker"]
    monkeypatch.setattr(module, "HELLO_SECONDS", 222, raising=False)
    assert kwc.owner_hello_deadline() == pytest.approx(222.0)


def test_an_unreadable_board_never_invents_a_deadline(tmp_path):
    """Missing/racing board: fall back to the prior instead of guessing from nothing."""
    assert kwc.spawn_baseline(str(tmp_path / "absent.db")) == kwc.spawn_baseline_prior()
    assert kwc.handshake_deadline(str(tmp_path / "absent.db")) == kwc.OWNER_WAIT_S


# --- the booking -----------------------------------------------------------------------


def test_a_pre_bind_timeout_releases_the_claim_and_charges_the_card_nothing(home):
    """The whole point: exit 76 releases the claim instead of booking an execution failure."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="slow boot", assignee="a")
        _dead_worker_with_log(conn, tid, 70001, PREBIND_TIMEOUT_EXIT_CODE)

        assert kbd.detect_crashed_workers(conn) == []
        assert kbd.detect_crashed_workers._last_prebind_timeout == [tid]

        event = conn.execute(
            "SELECT kind, payload FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (tid,)).fetchone()
        run = conn.execute(
            "SELECT outcome, error, metadata FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (tid,)).fetchone()
        task = kb.get_task(conn, tid)
        assert task is not None

        assert event["kind"] == "prebind_timeout"
        assert run["outcome"] == "prebind_timeout"
        assert kb._json_dict(run["metadata"])["prebind_timeout"] is True
        assert task.status == "ready"
        assert task.consecutive_failures == 0
        # The owner of the board still needs to see WHY it came back.
        assert "pre-bind deadline" in (task.last_failure_error or "")
        assert task.claim_lock is None and task.worker_pid is None


def test_repeated_pre_bind_timeouts_never_trip_the_breaker(home):
    """Four slow boots in a row: the card stays runnable and the counter never moves."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="hostile host", assignee="a")
        for i in range(4):
            _dead_worker_with_log(conn, tid, 70010 + i, PREBIND_TIMEOUT_EXIT_CODE)
            kbd.detect_crashed_workers(conn)
            task = kb.get_task(conn, tid)
            assert task is not None
            assert task.consecutive_failures == 0, f"attempt {i} charged the card"
            assert task.status == "ready", f"attempt {i} parked the card"
        assert kb.recompute_ready(conn, failure_limit=kbd.DEFAULT_FAILURE_LIMIT) == 0


def test_the_retry_is_spaced_instead_of_counted(home):
    """No failure budget is spent, so the release is paced by the cooldown guard instead."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="paced", assignee="a")
        _dead_worker_with_log(conn, tid, 70020, PREBIND_TIMEOUT_EXIT_CODE)
        kbd.detect_crashed_workers(conn)
        assert kbd.check_respawn_guard(conn, tid) == "prebind_timeout_cooldown"


def test_a_bound_worker_that_crashes_still_counts(home):
    """Control: the release path must not swallow an ordinary crash."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="real crash", assignee="a")
        _dead_worker_with_log(conn, tid, 70030, 1)
        assert kbd.detect_crashed_workers(conn) == [tid]
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.consecutive_failures == 1
        assert kbd.detect_crashed_workers._last_prebind_timeout == []
        assert conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (tid,)).fetchone()["outcome"] == "crashed"


def test_the_exit_code_is_its_own_kind(home):
    """76 must not read as a plain non-zero exit (nor as 75, the quota release)."""
    assert kbd._exit_code_kind(PREBIND_TIMEOUT_EXIT_CODE) == ("prebind_timeout", PREBIND_TIMEOUT_EXIT_CODE)
    assert kbd._exit_code_kind(kb.KANBAN_RATE_LIMIT_EXIT_CODE) == ("rate_limited", 75)
    assert kbd._exit_code_kind(1) == ("nonzero_exit", 1)


# --- the worker process ----------------------------------------------------------------


def test_the_process_exits_with_the_release_code_and_names_the_deadline(monkeypatch, capsys):
    """Exit 76 plus ``PreBindTimeout: worker boot took 185s > 180s`` — never a bare exit 1."""
    async def _boom(params, board_db):
        raise kwc.PreBindTimeout(
            "Kanban pre-bind deadline missed", elapsed=185, deadline=180,
            reason="managed_worker_hello_timeout")

    monkeypatch.setattr(kwc, "run", _boom)
    for key, value in {
        "HERMES_KANBAN_BOARD": "default",
        "HERMES_KANBAN_TASK": "t1",
        "HERMES_KANBAN_RUN_ID": "7",
        "HERMES_KANBAN_CLAIM_LOCK": "host:w1",
        "HERMES_KANBAN_DB": "unused.db",
    }.items():
        monkeypatch.setenv(key, value)

    assert kwc._submit() == PREBIND_TIMEOUT_EXIT_CODE
    err = capsys.readouterr().err
    assert "PreBindTimeout: worker boot took 185s > 180s" in err
    assert "managed_worker_hello_timeout" in err
    assert "no failure counted" in err


def _claimed_run(home: Path, *, boot_s: float = 62.0, bound: bool) -> tuple[str, str, dict]:
    """A running card whose attempt started *boot_s* ago, optionally having bound its claim."""
    path = _db(home)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="boot", assignee="a")
        task = kb.claim_task(conn, tid, claimer=f"{kb._claimer_id().split(':', 1)[0]}:w555")
        assert task is not None and task.current_run_id is not None
        run_id = task.current_run_id
        conn.execute("UPDATE task_runs SET started_at=? WHERE id=?", (int(time.time() - boot_s), run_id))
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (tid, run_id, "owner_admitted", "{}", int(time.time() - boot_s)))
        if bound:
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
                (tid, run_id, "worker_bound", "{}", int(time.time() - boot_s + 4)))
        params = {"task_id": tid, "run_id": run_id, "claim_lock": task.claim_lock, "board": "default"}
    return tid, path, params


def test_a_settlement_with_nothing_bound_is_a_pre_bind_timeout(home):
    """The incident signature: receipt terminal, no ``worker_bound``, nothing charged to the card."""
    _, path, params = _claimed_run(home, boot_s=185.0, bound=False)
    with pytest.raises(kwc.PreBindTimeout) as exc:
        kwc._exit_code({"status": "terminal", "outcome": "failed"}, params, path)
    line = exc.value.diagnostic()
    # Wall-clock elapsed, so the seconds are 185-186: the shape is what is pinned here (the exact
    # text is asserted against a constructed timeout below).
    assert line.startswith("PreBindTimeout: worker boot took 18")
    assert "> 180s (no worker_bound for this run" in line


def test_a_settlement_after_binding_keeps_the_workers_own_exit_code(home):
    """Control: a bound attempt that failed is still the card's failure, with its own message."""
    tid, path, params = _claimed_run(home, boot_s=62.0, bound=True)
    with kbc.connect() as conn:
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?,?,?,?,?)",
            (tid, params["run_id"], "worker_result",
             f'{{"claim_lock": "{params["claim_lock"]}", "exit_code": 1, '
            f'"last_output": "the model said something"}}', int(time.time())))
    assert kwc._exit_code({"status": "terminal", "outcome": "failed"}, params, path) == 1


def test_an_unbound_boot_inside_its_deadline_is_left_alone(home):
    """The deadline bounds a boot; a young attempt is never cut short, whatever the receipt says."""
    _, path, params = _claimed_run(home, boot_s=5.0, bound=False)
    assert kwc._overdue_prebind(params, path, kwc.OWNER_WAIT_S) is None
    assert kwc._settled_prebind(params, path, kwc.OWNER_WAIT_S) is not None
