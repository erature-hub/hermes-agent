"""The reassignment guard: a HELD card may not change hands, and the failure
breaker may not be laundered.

The guard used to key on ``status='running'``. A card that was moved back to
``ready`` while still holding its claim lock therefore looked FREE, and any
caller could take it over: ``assign_task`` rewrote the assignee, the original
holder's lock and lease survived, and when that lease expired the sweep
requeued the card with ``claim_lock = NULL`` — into the thief's dispatch lane,
with the original holder's worker still running it.

The same write also reset ``consecutive_failures``/``last_failure_error`` on
an assignee change, so a takeover laundered a repeatedly-failing card back
to a clean streak: the breaker never tripped and the card re-entered the
dispatch lane forever instead of parking for an operator.

The contract these tests pin down:

* the guard fires whenever ``claim_lock IS NOT NULL``, whatever the status;
* a refusal changes NOTHING (assignee, lock) and is AUDITED;
* an unheld card (``todo``/``blocked``/``ready``) still accepts a reassign;
* the failure streak survives an assignee change and a card at the breaker
  limit stays parked;
* only an explicit operator reassign grants a fresh retry budget.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _row(conn, tid):
    return conn.execute(
        "SELECT status, claim_lock, assignee, consecutive_failures, last_failure_error "
        "FROM tasks WHERE id = ?",
        (tid,),
    ).fetchone()


def _event_kinds(conn, tid):
    return [
        r["kind"]
        for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id = ? ORDER BY id", (tid,)
        ).fetchall()
    ]


def _strand(conn, tid):
    """The shape the bridge's ``move`` op produces: status back to ``ready``
    while the claim lock is still set — the card is held but LOOKS free."""
    conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (tid,))


# ── (a) running + claimed ─────────────────────────────────────────────


def test_running_and_claimed_card_refuses_reassignment(kanban_home):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="cypher")
        assert kb.claim_task(conn, tid, claimer="cypher") is not None
        before = _row(conn, tid)

        with pytest.raises(RuntimeError, match="held by"):
            kb.assign_task(conn, tid, "sentinel")

        after = _row(conn, tid)
        assert after["assignee"] == before["assignee"] == "cypher"
        assert after["claim_lock"] == before["claim_lock"] == "cypher"
        assert after["status"] == "running"
    finally:
        conn.close()


# ── (b) ready + claimed (the stranding shape) ─────────────────────────


def test_stranded_ready_and_claimed_card_refuses_reassignment(kanban_home):
    """The shape that used to slip through: ``status='ready'`` with the lock
    still set. The assignee and the lock must both survive the attempt."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="cypher")
        assert kb.claim_task(conn, tid, claimer="cypher") is not None
        _strand(conn, tid)
        assert _row(conn, tid)["status"] == "ready"
        assert _row(conn, tid)["claim_lock"] == "cypher"

        with pytest.raises(RuntimeError, match="held by"):
            kb.assign_task(conn, tid, "sentinel")

        after = _row(conn, tid)
        assert after["assignee"] == "cypher"
        assert after["claim_lock"] == "cypher"
        assert after["status"] == "ready"
    finally:
        conn.close()


# ── (c) unheld cards still accept a reassignment ──────────────────────


@pytest.mark.parametrize("status", ["todo", "blocked", "ready"])
def test_unheld_card_accepts_reassignment(kanban_home, status):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="cypher")
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))

        assert kb.assign_task(conn, tid, "sentinel") is True

        after = _row(conn, tid)
        assert after["assignee"] == "sentinel"
        assert after["claim_lock"] is None
    finally:
        conn.close()


# ── (7) a refusal is auditable ─────────────────────────────────────────


def test_refusal_is_recorded_as_an_event(kanban_home):
    """The trail must show the takeover attempt, not only the column it
    failed to change."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="cypher")
        assert kb.claim_task(conn, tid, claimer="cypher") is not None
        _strand(conn, tid)

        with pytest.raises(RuntimeError):
            kb.assign_task(conn, tid, "sentinel")

        rows = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'assignment_rejected' "
            "ORDER BY id DESC LIMIT 1",
            (tid,),
        ).fetchall()
        assert len(rows) == 1
        import json

        payload = json.loads(rows[0]["payload"])
        assert payload["held_by"] == "cypher"
        assert payload["requested"] == "sentinel"
        assert payload["status"] == "ready"
        # No ``assigned`` event: the write was refused, not applied.
        assert "assigned" not in _event_kinds(conn, tid)
    finally:
        conn.close()


# ── (5) the breaker is not launderable ─────────────────────────────────


def test_failure_streak_survives_an_assignee_change(kanban_home):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="cypher")
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 3, last_failure_error = 'boom' "
            "WHERE id = ?",
            (tid,),
        )

        assert kb.assign_task(conn, tid, "sentinel") is True

        after = _row(conn, tid)
        assert after["assignee"] == "sentinel"
        assert after["consecutive_failures"] == 3
        assert after["last_failure_error"] == "boom"
    finally:
        conn.close()


def test_card_at_the_breaker_limit_stays_parked_after_a_reassignment(kanban_home):
    """A card that failed ``failure_limit`` times must still park — a takeover
    (or any assignee change) may not buy it a clean streak."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="cypher")
        conn.execute(
            "UPDATE tasks SET status = 'blocked', consecutive_failures = ?, "
            "last_failure_error = 'boom' WHERE id = ?",
            (kb.DEFAULT_FAILURE_LIMIT, tid),
        )

        assert kb.assign_task(conn, tid, "sentinel") is True

        after = _row(conn, tid)
        assert after["consecutive_failures"] == kb.DEFAULT_FAILURE_LIMIT
        assert after["status"] == "blocked"
        # The breaker still holds: the card is not auto-recovered.
        assert kb.recompute_ready(conn) == 0
        assert _row(conn, tid)["status"] == "blocked"
    finally:
        conn.close()


def test_operator_reassign_grants_a_fresh_retry_budget(kanban_home):
    """The explicit operator path stays the escape hatch: a person
    re-routing a card is not a takeover."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="cypher")
        conn.execute(
            "UPDATE tasks SET consecutive_failures = 3, last_failure_error = 'boom' "
            "WHERE id = ?",
            (tid,),
        )

        assert kb.reassign_task(conn, tid, "sentinel") is True

        after = _row(conn, tid)
        assert after["assignee"] == "sentinel"
        assert after["consecutive_failures"] == 0
        assert after["last_failure_error"] is None
    finally:
        conn.close()


# ── the holder may re-affirm its own assignee ──────────────────────────


def test_holder_may_reaffirm_its_own_assignee(kanban_home):
    """The claim path names the agent that just took the card; that write is
    the holder asserting itself, not a takeover."""
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="cypher")
        assert kb.claim_task(conn, tid, claimer="cypher") is not None

        assert kb.assign_task(conn, tid, "cypher", holder="cypher") is True
        assert _row(conn, tid)["assignee"] == "cypher"

        # But the holder may not hand the card to somebody else.
        with pytest.raises(RuntimeError, match="held by"):
            kb.assign_task(conn, tid, "sentinel", holder="cypher")
        assert _row(conn, tid)["assignee"] == "cypher"
        assert _row(conn, tid)["claim_lock"] == "cypher"
    finally:
        conn.close()


def test_reassign_task_refuses_a_held_card_without_reclaim(kanban_home):
    conn = kbc.connect()
    try:
        tid = kb.create_task(conn, title="t", assignee="cypher")
        assert kb.claim_task(conn, tid, claimer="cypher") is not None
        _strand(conn, tid)

        assert kb.reassign_task(conn, tid, "sentinel") is False
        assert _row(conn, tid)["assignee"] == "cypher"
        assert _row(conn, tid)["claim_lock"] == "cypher"
    finally:
        conn.close()
