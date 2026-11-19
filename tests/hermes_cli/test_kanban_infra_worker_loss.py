"""Infrastructure worker losses must not trip the breaker.

A worker lost to a gateway restart, an update drain, or an owner that cannot
resolve the outcome ("Kanban execution unknown; retained by owner") usually did
real work before it died. Booking that as a crash spends the card's failure
budget, and two such losses auto-block a healthy card -- the cascade behind the
2026-11-14/15 blocked-card pile. The reclaim already requeued the card, so the
loss must leave ``consecutive_failures`` untouched and let the card restart.
"""
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _dead(output: str) -> "kbd._DeadWorker":
    return kbd._DeadWorker(
        kind="nonzero_exit",
        code=1,
        error_text="pid 4242 exited with code 1",
        event_kind="crashed",
        event_payload={"worker_output": output},
    )


def test_receipt_unknown_loss_is_infrastructure_not_a_crash(kanban_home):
    """The owner's unknown-outcome refusal is a host interruption: no failure."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="infra loss", assignee="worker")
        dead = _dead("Kanban owner refused: Kanban execution unknown; retained by owner")

        assert kbd._is_infrastructure_worker_loss(dead) is True

        blocked = kbd._account_crashes(conn, [(tid, 4242, "erature-ai:1", dead)])

        assert blocked == []
        assert kb.get_task(conn, tid).consecutive_failures == 0


def test_update_drain_loss_is_infrastructure_not_a_crash(kanban_home):
    """A gateway draining for an update starves the worker; that is not a task crash."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="drain loss", assignee="worker")
        dead = _dead("Kanban owner silent for 183s: Gateway draining: update_paused")

        assert kbd._is_infrastructure_worker_loss(dead) is True
        assert kbd._account_crashes(conn, [(tid, 4242, "erature-ai:1", dead)]) == []
        assert kb.get_task(conn, tid).consecutive_failures == 0


def test_genuine_crash_still_counts_against_the_card(kanban_home):
    """A real work failure still spends the budget, so the breaker can trip."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="real crash", assignee="worker")
        dead = _dead("real work failed: AssertionError in test_foo")

        assert kbd._is_infrastructure_worker_loss(dead) is False

        kbd._account_crashes(conn, [(tid, 4242, "erature-ai:1", dead)])

        assert kb.get_task(conn, tid).consecutive_failures == 1
