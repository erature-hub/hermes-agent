"""Wire the JEV pacer into the Hermes dispatch tick.

Two halves, both observer-only:

**Observing.** After a tick completes (strictly outside the dispatch lock),
the dispatcher tells the pacer what it observed: how much work was ready,
how much it spawned, and what the quota gate said. The pacer answers with a
decision for the NEXT tick and, when it is stalled, a class (f) receipt.

**Obeying.** On the next tick, the pacer's drain order reorders the cards
the dispatcher would spawn and the pacer's budget caps how many it may
spawn. The cap is a ceiling: the dispatcher keeps its own budget when the
pacer is silent, and the pacer can only narrow it, never widen it.

Neither half can slow the dispatcher down. The observation is a single
bounded HTTP call made after the lock is released, and every failure of it
degrades to "no advice".
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Optional, Sequence

from hermes_cli.kanban_pacer import (
    DrainCandidate,
    PacerClient,
    build_dispatch_tick,
    map_quota_deferred,
    map_quota_state,
)
from hermes_cli.kanban_pacer_advice import (
    claim_budget_for,
    drain_order_for,
    read_advice,
    receipt_enrichment,
    record_advice,
)

_log = logging.getLogger(__name__)

#: How many ready cards are offered to the pacer for ranking. The pacer
#: ranks what it is given, so the list is a sample of the queue, not the
#: whole thing: a hundred-card backlog would make the request body large
#: for no gain, because the pacer's own capacity model caps what it will
#: spawn regardless of how many candidates it sees.
MAX_DRAIN_CANDIDATES = 50


def _ready_rows_for_pacing(
    conn: sqlite3.Connection,
    *,
    limit: int = MAX_DRAIN_CANDIDATES,
) -> list[sqlite3.Row]:
    """Ready cards offered to the pacer, in the dispatcher's own order.

    ``ready_since`` is the moment the card entered the ready lane, which is
    what lets the pacer prefer the card that has been waiting longest among
    equally-prioritised ones. The ``promoted`` event records that
    transition; a card that was never promoted (created straight into
    ``ready``) has no such event, and its creation time is the honest
    fallback.
    """
    return conn.execute(
        "SELECT t.id AS id, t.priority AS priority, t.assignee AS assignee, "
        "       COALESCE("
        "         (SELECT MAX(e.created_at) FROM task_events e "
        "          WHERE e.task_id = t.id AND e.kind = 'promoted'),"
        "         t.created_at"
        "       ) AS ready_since "
        "FROM tasks t "
        "WHERE t.status = 'ready' AND t.claim_lock IS NULL "
        "ORDER BY t.priority DESC, t.created_at ASC "
        "LIMIT ?",
        (int(limit),),
    ).fetchall()


def build_observation(
    conn: sqlite3.Connection,
    result: Any,
    *,
    tick_index: int,
    worker_cap: int = 0,
    spawn_budget: int = 0,
    observed_at: Optional[float] = None,
) -> dict[str, Any]:
    """Build the ``DispatchTick`` body describing the tick that just ran.

    ``ready_count`` is the depth of the ready lane AFTER the tick, because
    that is the work the pacer is being asked to pace. ``spawned_count`` is
    what this tick actually spawned.
    """
    ready_rows = _ready_rows_for_pacing(conn)
    todo_count = 0
    try:
        todo_row = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE status = 'todo'"
        ).fetchone()
        todo_count = int(todo_row[0]) if todo_row else 0
    except sqlite3.Error:
        todo_count = 0
    candidates = tuple(
        DrainCandidate(
            task_id=str(row["id"]),
            priority=int(row["priority"] or 0),
            assignee=str(row["assignee"]) if row["assignee"] else None,
            ready_since=float(row["ready_since"]) if row["ready_since"] is not None else None,
        )
        for row in ready_rows
    )
    spawned = getattr(result, "spawned", None) or ()
    return build_dispatch_tick(
        tick_index=tick_index,
        observed_at=time.time() if observed_at is None else observed_at,
        ready_count=len(ready_rows),
        todo_count=todo_count,
        spawned_count=len(spawned),
        quota_deferred=map_quota_deferred(result),
        quota_state=map_quota_state(result),
        worker_cap=worker_cap,
        spawn_budget=spawn_budget,
        drain_candidates=candidates,
    )


def observe_tick(
    conn: sqlite3.Connection,
    result: Any,
    *,
    board: Optional[str] = None,
    tick_index: int = 0,
    worker_cap: int = 0,
    spawn_budget: int = 0,
    client: Optional[PacerClient] = None,
) -> Optional[dict[str, Any]]:
    """Tell the pacer what this tick observed and record what it said.

    Returns the receipt (as enrichment for the alarm bus's class (f) event)
    or ``None``. Every failure -- unreachable pacer, unmounted route, a
    non-JSON body -- yields ``None`` and leaves the dispatcher untouched.
    """
    try:
        observation = build_observation(
            conn,
            result,
            tick_index=tick_index,
            worker_cap=worker_cap,
            spawn_budget=spawn_budget,
        )
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("jev pacer observation build failed: %s", exc)
        return None
    try:
        advisory = (client or PacerClient()).observe(observation)
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("jev pacer observe failed: %s", exc)
        return None
    if advisory is None:
        return None
    try:
        record_advice(advisory, board=board)
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("jev pacer advice record failed: %s", exc)
    return receipt_enrichment(read_advice(board=board))


def apply_pacing_advice(
    rows: Sequence[sqlite3.Row],
    spawn_budget: Optional[int],
    *,
    board: Optional[str] = None,
) -> tuple[list[sqlite3.Row], Optional[int]]:
    """Reorder and cap this tick's spawn plan using the pacer's advice.

    The returned budget is the tighter of the dispatcher's own budget and
    the pacer's. The returned rows are ordered the way the pacer asked.
    When there is no advice, or the advice is stale, both are returned
    unchanged.
    """
    advice = read_advice(board=board)
    if not advice:
        return list(rows), spawn_budget
    candidates = [str(row["id"]) for row in rows]
    ordered_ids = drain_order_for(advice, candidates)
    by_id = {str(row["id"]): row for row in rows}
    ordered: list[sqlite3.Row] = []
    for task_id in ordered_ids:
        row = by_id.get(task_id)
        if row is not None:
            ordered.append(row)
    budget = claim_budget_for(advice, spawn_budget)
    return ordered, budget


__all__ = [
    "MAX_DRAIN_CANDIDATES",
    "apply_pacing_advice",
    "build_observation",
    "observe_tick",
]
