"""Regression tests for the sticky-block guard bypass in ``recompute_ready``.

THE DEFECT
==========
``recompute_ready`` decided whether a card was held for a human by reading
its ``status`` COLUMN — and it consulted the sticky-block predicate only when
that column said ``blocked``::

    if cur_status == "blocked" and _has_sticky_block(conn, task_id):
        continue

A card that a worker parked for a human (``block_task(kind="needs_input")``)
records that decision in the EVENT LOG and in ``block_kind``; the ``status``
column is only a cache of it, and several writers rewrite that cache without
recording a release — a raw board write, the dashboard's ``set_status``, a
status rewrite by ``unblock_task``. When such a card's column drifted back to
``todo``, the guard was skipped entirely and the card was promoted to
``ready`` the moment its parent completed: a respawn with none of the human
input it was waiting for.

The live shape (measured on the Erature board, 2026-11-15): three of the six
children of epic ``t_91efba98`` sat at ``status='todo'`` with
``block_kind='needs_input'`` and a pending ``blocked`` event, held behind a
root parked ``scheduled``. The moment that root went terminal, all three
would have been released.

WHAT THESE TESTS PIN
====================
* A card held for a human (``needs_input`` / ``capability``) stays parked
  whatever the status column drifted to — ``todo`` as well as ``blocked``.
* The ``dependency`` lane is NOT a park: it waits in ``todo`` and MUST be
  promoted the moment its parents land. A blanket "``block_kind IS NOT
  NULL`` → skip" rule would strand every dependency-wait card on the board,
  forever.
* An explicit ``kanban_unblock`` releases the park — the guard must not turn
  a park into a permanent one, even though ``unblock_task`` deliberately
  keeps ``block_kind`` for unblock-loop detection.
* The ``block_kind`` column is a fallback authority for a decision whose
  event was lost (a board import, a restored snapshot), and only when the
  log never recorded a release.
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


def _drift_status_to_todo(conn, task_id: str) -> None:
    """Rewrite a card's status column to ``todo`` WITHOUT recording a release.

    This models the real writers: a raw board write, the dashboard's
    ``set_status``, or a status rewrite by ``unblock_task``. The event log —
    the audit trail of the park decision — is deliberately left untouched.
    """
    conn.execute("UPDATE tasks SET status = 'todo' WHERE id = ?", (task_id,))
    conn.commit()


def _complete_parent(conn, parent_id: str) -> None:
    """Take the parent to ``done`` so its children become promotable."""
    kb.claim_task(conn, parent_id)
    kb.complete_task(conn, parent_id, result="done")
    assert kb.get_task(conn, parent_id).status == "done"


# ---------------------------------------------------------------------------
# The bypass: a park whose status column drifted to 'todo'
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["needs_input", "capability"])
def test_todo_card_held_for_a_human_is_not_promoted_when_parent_completes(
    kanban_home: Path, kind: str,
) -> None:
    """AC2: ``status='todo'`` + ``block_kind`` + a ``blocked`` event stays
    parked when its parent completes.

    Before the fix the guard was gated on ``cur_status == "blocked"``, so a
    card whose column had drifted to ``todo`` was promoted the instant its
    parent went terminal.
    """
    with kbc.connect() as conn:
        parent_id = kb.create_task(conn, title="epic root")
        child_id = kb.create_task(conn, title="gated child")

        # Park the card for a human: block_kind set, 'blocked' event logged.
        kb.claim_task(conn, child_id)
        assert kb.block_task(
            conn, child_id, kind=kind, reason="awaiting a human decision",
            expected_run_id=kb.get_task(conn, child_id).current_run_id,
        )
        parked = kb.get_task(conn, child_id)
        # The park lands in a lane ``recompute_ready`` does not scan. Which
        # lane is POLICY (``blocked`` on main, ``review`` where a human gate
        # is routed out of the promotable columns entirely) — both are parks,
        # and neither is promoted.
        assert parked.status in ("blocked", "review"), (
            f"a {kind} park must land in a non-promotable lane, "
            f"got {parked.status!r}"
        )
        assert parked.block_kind == kind

        # The card now waits on the parent, and its status column drifts.
        kb.link_tasks(conn, parent_id, child_id)
        _drift_status_to_todo(conn, child_id)
        assert kb.get_task(conn, child_id).status == "todo"

        _complete_parent(conn, parent_id)

        # The parent is terminal — the card must NOT be released.
        kb.recompute_ready(conn)
        assert kb.get_task(conn, child_id).status == "todo", (
            "a card held for a human must stay parked when its parent "
            "completes, whatever the status column drifted to"
        )


def test_parked_child_stays_parked_across_many_dispatcher_ticks(
    kanban_home: Path,
) -> None:
    """The guard holds over repeated ticks, not just the first one."""
    with kbc.connect() as conn:
        parent_id = kb.create_task(conn, title="epic root")
        child_id = kb.create_task(conn, title="gated child")
        kb.claim_task(conn, child_id)
        kb.block_task(
            conn, child_id, kind="needs_input", reason="awaiting a human decision",
            expected_run_id=kb.get_task(conn, child_id).current_run_id,
        )
        kb.link_tasks(conn, parent_id, child_id)
        _drift_status_to_todo(conn, child_id)
        _complete_parent(conn, parent_id)

        for _ in range(5):
            assert kb.recompute_ready(conn) == 0, "a parked card must never be promoted"
            assert kb.get_task(conn, child_id).status == "todo"


# ---------------------------------------------------------------------------
# The guard must not over-reach
# ---------------------------------------------------------------------------


def test_dependency_wait_card_is_still_promoted_when_its_parent_completes(
    kanban_home: Path,
) -> None:
    """The ``dependency`` lane waits in ``todo`` and MUST be promoted.

    This is the regression that a naive "``block_kind IS NOT NULL`` → skip"
    rule would introduce: every dependency-wait card on the board would be
    stranded forever.
    """
    with kbc.connect() as conn:
        parent_id = kb.create_task(conn, title="epic root")
        child_id = kb.create_task(conn, title="dependency waiter")

        claimed = kb.claim_task(conn, child_id)
        kb.link_tasks(
            conn, parent_id, child_id, expected_child_run_id=claimed.current_run_id,
        )
        assert kb.block_task(
            conn, child_id, kind="dependency", reason="waiting on the epic",
            expected_run_id=claimed.current_run_id,
        )
        waiter = kb.get_task(conn, child_id)
        assert waiter.status == "todo", "a dependency wait parks in 'todo'"
        assert waiter.block_kind == "dependency"

        _complete_parent(conn, parent_id)

        kb.recompute_ready(conn)
        assert kb.get_task(conn, child_id).status == "ready", (
            "a dependency wait is not a human gate: it must be promoted the "
            "moment its parent lands"
        )


def test_explicit_unblock_releases_the_park(kanban_home: Path) -> None:
    """``unblock_task`` is the ONLY way out of a park.

    ``unblock_task`` deliberately keeps ``block_kind`` for unblock-loop
    detection, so the column still names a human gate after the release. The
    guard must read the event log (the release) and let the card go — a park
    that can never be released is just a lost card.
    """
    with kbc.connect() as conn:
        parent_id = kb.create_task(conn, title="epic root")
        child_id = kb.create_task(conn, title="gated child")
        kb.claim_task(conn, child_id)
        kb.block_task(
            conn, child_id, kind="needs_input", reason="awaiting a human decision",
            expected_run_id=kb.get_task(conn, child_id).current_run_id,
        )
        kb.link_tasks(conn, parent_id, child_id)
        assert kb.get_task(conn, child_id).status in ("blocked", "review")

        assert kb.unblock_task(conn, child_id)
        released = kb.get_task(conn, child_id)
        assert released.status == "todo", (
            "the release lands the card in 'todo' — its parent is still open"
        )
        assert released.block_kind == "needs_input", (
            "unblock_task keeps block_kind for loop detection"
        )

        _complete_parent(conn, parent_id)

        kb.recompute_ready(conn)
        assert kb.get_task(conn, child_id).status == "ready", (
            "an explicit unblock must release the park"
        )


def test_plain_todo_card_is_promoted_when_its_parent_completes(
    kanban_home: Path,
) -> None:
    """The ordinary dependency gate keeps working: no block, no park."""
    with kbc.connect() as conn:
        parent_id = kb.create_task(conn, title="epic root")
        child_id = kb.create_task(conn, title="ordinary child", parents=[parent_id])
        assert kb.get_task(conn, child_id).status == "todo"

        _complete_parent(conn, parent_id)

        kb.recompute_ready(conn)
        assert kb.get_task(conn, child_id).status == "ready"


# ---------------------------------------------------------------------------
# The column as a fallback authority
# ---------------------------------------------------------------------------


def test_column_only_park_survives_a_lost_event_log(kanban_home: Path) -> None:
    """``block_kind`` still parks a card whose block event was lost.

    A board import or a restored snapshot can carry the ``block_kind`` column
    without the ``blocked`` event that explains it. The column is the weaker
    authority, but it is an authority — and here the log never recorded a
    release either.
    """
    with kbc.connect() as conn:
        parent_id = kb.create_task(conn, title="epic root")
        child_id = kb.create_task(conn, title="gated child")
        kb.claim_task(conn, child_id)
        kb.block_task(
            conn, child_id, kind="needs_input", reason="awaiting a human decision",
            expected_run_id=kb.get_task(conn, child_id).current_run_id,
        )
        kb.link_tasks(conn, parent_id, child_id)

        # The log loses the decision; the column keeps it.
        conn.execute(
            "DELETE FROM task_events WHERE task_id = ? AND kind IN ('blocked', 'unblocked')",
            (child_id,),
        )
        _drift_status_to_todo(conn, child_id)
        assert kb.get_task(conn, child_id).block_kind == "needs_input"

        _complete_parent(conn, parent_id)

        kb.recompute_ready(conn)
        assert kb.get_task(conn, child_id).status == "todo", (
            "the block_kind column is a fallback authority for a lost decision"
        )
