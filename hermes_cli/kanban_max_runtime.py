"""The runtime bound that applies to a kanban card.

A card's own ``--max-runtime`` (``tasks.max_runtime_seconds``) always wins.
A card claimed *without* one is bounded by the fleet-wide
``kanban.max_runtime_seconds`` default, so no card can hold a worker slot for
an unbounded stretch of time -- the failure that let six runners sit on every
slot for 7-11h while ten ready cards waited behind them.

Two independent bounds live here and must not be confused:

* the **card** bound (``--max-runtime``) is the card's own promise about how
  long it may run. It also widens the worker's terminal-command timeout, so a
  card that was told it may run for hours is not cut off mid-command.
* the **fleet** bound (``kanban.max_runtime_seconds``) is the host's ceiling
  on how long one attempt may hold a slot. It never widens a command timeout:
  a fleet ceiling is not a promise that any single command may run that long.

The fleet bound is read from the *dispatching* home's config, exactly like
``kanban.max_in_progress`` and ``kanban.dispatch_stale_timeout_seconds``: the
host that enforces the bound is the host whose config states it. It is read
when it is enforced, never captured at import or boot, so an operator edit
applies on the next dispatch tick and cards claimed before the default
existed are bounded too.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, NamedTuple, Optional

from hermes_cli.config_defaults import DEFAULT_CONFIG

logger = logging.getLogger(__name__)

#: The shipped fleet bound: 6h. Sits just above the p99 of observed attempt
#: durations on a live board (~5.8h), so it reclaims the runaway tail without
#: touching ordinary work, and is well under the 7-11h runs that held every
#: slot. Operators who need longer raise ``kanban.max_runtime_seconds`` or pin
#: a card with ``--max-runtime``. Sourced from the config default so the two
#: can never drift apart.
DEFAULT_MAX_RUNTIME_SECONDS = int(DEFAULT_CONFIG["kanban"]["max_runtime_seconds"])


def resolve_max_runtime_seconds(raw: Any) -> Optional[int]:
    """The fleet bound in seconds from a configured value; None = unbounded.

    ``0`` (or ``null``) means "no fleet bound" -- the explicit opt-out. A
    positive int is the bound. ``None`` or an unusable value falls back to
    :data:`DEFAULT_MAX_RUNTIME_SECONDS` rather than leaving the fleet
    unbounded, because an unbounded fleet is the failure this bound exists to
    prevent.
    """
    if raw is None or isinstance(raw, bool):
        return DEFAULT_MAX_RUNTIME_SECONDS
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RUNTIME_SECONDS
    if seconds == 0:
        return None
    if seconds < 0:
        return DEFAULT_MAX_RUNTIME_SECONDS
    return seconds


def effective_max_runtime_seconds(
    card_limit: Optional[int],
    fleet_limit: Optional[int],
) -> Optional[int]:
    """The bound in force for one card: its own cap wins, else the fleet bound.

    ``None`` means the card is unbounded -- no explicit cap and no fleet bound.
    """
    if card_limit is not None:
        try:
            return int(card_limit)
        except (TypeError, ValueError):
            return fleet_limit
    return fleet_limit


def fleet_max_runtime_seconds() -> Optional[int]:
    """The fleet bound from the current home's config; None when unbounded.

    Read at call time (never captured at import) so the dispatching home's
    config governs, and so an operator edit applies on the next tick. A config
    that cannot be read falls back to the shipped bound: a broken config
    plane must not silently unbind the fleet.
    """
    from hermes_cli.config import load_config_readonly
    from hermes_cli.config_backend import ConfigBackendUnavailable

    try:
        raw = (load_config_readonly() or {}).get("kanban", {}).get("max_runtime_seconds")
    except ConfigBackendUnavailable:
        # The config backend's unavailable signal is a SystemExit subclass: a
        # remote backend that cannot be reached must not end the dispatch tick.
        logger.warning("kanban: config backend unavailable; using the default max runtime")
        return DEFAULT_MAX_RUNTIME_SECONDS
    except Exception:
        logger.warning("kanban: max runtime config read failed; using the default", exc_info=True)
        return DEFAULT_MAX_RUNTIME_SECONDS
    return resolve_max_runtime_seconds(raw)


class BoundAttempt(NamedTuple):
    """A running attempt that is past its effective runtime bound."""

    id: str
    worker_pid: int
    # The worker fingerprint: an int process-start time, or the "unverified" marker.
    worker_started_at: Any
    claim_lock: Optional[str]
    elapsed_seconds: int
    limit_seconds: int


def attempts_over_bound(conn: sqlite3.Connection, *, now: Optional[int] = None) -> list[BoundAttempt]:
    """The running attempts whose elapsed time exceeds their effective bound.

    Runtime is per attempt: ``tasks.started_at`` records the FIRST start, so a
    retry is measured from the active ``task_runs`` row and gets a fresh window.
    A card with its own ``max_runtime_seconds`` keeps that bound; every other
    card runs under the fleet-wide default, so a card claimed without an
    explicit cap is bounded too.
    """
    from hermes_cli import kanban_db as kb

    now = int(time.time()) if now is None else int(now)
    fleet_limit = fleet_max_runtime_seconds()
    rows = conn.execute(
        "SELECT t.id, t.worker_pid, t.worker_started_at, t.claim_lock, "
        "       COALESCE(r.started_at, t.started_at) AS active_started_at, "
        "       COALESCE(t.max_runtime_seconds, ?) AS limit_seconds "
        "FROM tasks t "
        "LEFT JOIN task_runs r ON r.id = t.current_run_id "
        "WHERE t.status = 'running' AND COALESCE(t.max_runtime_seconds, ?) IS NOT NULL "
        "  AND COALESCE(r.started_at, t.started_at) IS NOT NULL "
        "  AND t.worker_pid IS NOT NULL",
        (fleet_limit, fleet_limit),
    ).fetchall()
    overdue: list[BoundAttempt] = []
    for row in rows:
        elapsed = now - (kb._to_epoch(row["active_started_at"]) or now)  # unreadable clock: never kill
        limit = int(row["limit_seconds"])
        if elapsed < limit:
            continue
        overdue.append(BoundAttempt(
            id=row["id"],
            worker_pid=int(row["worker_pid"]),
            worker_started_at=kb._row_get(row, "worker_started_at"),
            claim_lock=row["claim_lock"],
            elapsed_seconds=elapsed,
            limit_seconds=limit,
        ))
    return overdue
