"""The review lifecycle: claiming a review, and whose card it is.

Split out of ``hermes_cli.kanban_db``; the board's own helpers are reached
late-bound via ``_kb`` (import-cycle breaking) so monkeypatching
``kanban_db.<name>`` keeps working.

Two things live here.

**The review transitions.** Claiming a card from ``review`` back into a run
(``claim_review_task``) and deciding where a retried run lands
(``_retry_status_for_run``) are the review lane's own moves, and they are
only ever reached from the review path.

**The handoff provenance.** ``request_review`` stamps the
``review_requested`` event with the profile that did the work, so
``request_changes`` can hand the card back to it. Cards handed off before
that field existed carry a payload with no ``implementer`` key at all, and
``request_changes`` refused them mechanically -- "review handoff has no valid
implementer provenance" -- which turned a reviewer's verdict into an
operator-only action and stranded the card in review until someone
intervened by hand (incident card ``t_5cfe58e6``, tracked as ``t_a4c9e812``).

The work is not lost. The run that produced the handoff is still in
``task_runs``, and its profile IS the implementer. :func:`resolve_implementer`
recovers the implementer from the board's own run history instead of
refusing, and is the single place that decides where a healed implementer
comes from.

The refusal stays correct in exactly one case: a card nobody worked but its
reviewer. Its handoff run carries no profile, and routing it back would send
the findings to the profile that wrote them -- the contract pinned by
``tests/hermes_cli/test_kanban_review_lifecycle.py::
test_review_handoff_of_card_assigned_to_its_reviewer_records_no_implementer``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Optional

# Where a healed implementer came from. ``None`` means the handoff event
# already carried the provenance and nothing was repaired.
PROVENANCE_EXPLICIT = "explicit"
PROVENANCE_HANDOFF_RUN = "handoff_run"
PROVENANCE_LAST_RUN = "last_run"
PROVENANCE_ASSIGNEE = "assignee"


def clean_profile_name(value: Any) -> Optional[str]:
    """A usable profile id, or ``None``. Never raises on a non-string.

    The canonical form is the lowercase id the board stores (what
    ``normalize_profile_name`` produces), so a recovered implementer is
    directly assignable and directly dispatchable.
    """
    if not isinstance(value, str):
        return None
    try:
        from hermes_cli.profiles import normalize_profile_name

        return normalize_profile_name(value)
    except (ValueError, ImportError):
        return None


def _stripped(value: Any) -> Optional[str]:
    """A non-blank string, unmodified. Used for values already on the board."""
    return value if isinstance(value, str) and value.strip() else None


def handoff_payload(raw: Any) -> Optional[dict]:
    """The handoff event's payload as an object, or ``None`` when unreadable.

    ``None`` means the event is CORRUPT -- malformed JSON, a JSON value that
    is not an object, or no payload at all -- and its provenance cannot be
    trusted in any direction. A corrupt event is not the same thing as a
    LEGACY event: a legacy event is a well-formed object that simply predates
    the ``implementer`` field, and the run history can still answer for it.
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def run_profile(conn: sqlite3.Connection, run_id: Optional[int]) -> Optional[str]:
    """The profile that owned a run, or ``None`` when the run named nobody.

    A synthesized run (a transition on a never-claimed card) carries a NULL
    profile on purpose -- it records that nobody acted -- so a NULL here means
    "no evidence", not "unknown".
    """
    if run_id is None:
        return None
    try:
        row = conn.execute(
            "SELECT profile FROM task_runs WHERE id = ?", (int(run_id),),
        ).fetchone()
    except (TypeError, ValueError):
        return None
    if row is None:
        return None
    return clean_profile_name(row[0])


def last_worker_profile(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    exclude_run_id: Optional[int] = None,
    before_run_id: Optional[int] = None,
) -> Optional[str]:
    """The most recent run that named a worker, newest first.

    ``exclude_run_id`` drops the reviewer's own run: that is the profile
    ASKING for the rework, never the implementer. ``before_run_id`` bounds the
    search to the runs that predate the handoff, so a reviewer's later run can
    never be mistaken for the worker that did the work.
    """
    sql = (
        "SELECT profile FROM task_runs "
        "WHERE task_id = ? AND profile IS NOT NULL AND TRIM(profile) <> ''"
    )
    params: list[Any] = [task_id]
    if exclude_run_id is not None:
        sql += " AND id <> ?"
        params.append(int(exclude_run_id))
    if before_run_id is not None:
        sql += " AND id < ?"
        params.append(int(before_run_id))
    sql += " ORDER BY id DESC LIMIT 1"
    row = conn.execute(sql, tuple(params)).fetchone()
    return clean_profile_name(row[0]) if row is not None else None


def prior_reviewer(conn: sqlite3.Connection, task_id: str):
    """Reviewer recorded by the latest ``changes_requested`` run's event.

    ``None`` = first review (no such run); ``False`` = a run exists but its
    provenance is missing/malformed.

    Lives here with the rest of the review-handoff provenance: the reviewer
    of the last round and the implementer of the next one are read off the
    same event log, and neither belongs in the board's facade.
    """
    changes_run = conn.execute(
        "SELECT id FROM task_runs "
        "WHERE task_id = ? AND outcome = 'changes_requested' "
        "ORDER BY id DESC LIMIT 1", (task_id,),
    ).fetchone()
    if changes_run is None:
        return None
    row = conn.execute(
        "SELECT payload FROM task_events "
        "WHERE task_id = ? AND kind = 'changes_requested' AND run_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (task_id, int(changes_run["id"])),
    ).fetchone()
    payload = handoff_payload(row[0]) if row is not None else None
    reviewer = (payload or {}).get("reviewer")
    return reviewer if isinstance(reviewer, str) and reviewer.strip() else False


def resolve_implementer(
    conn: sqlite3.Connection,
    task_id: str,
    *,
    handoff_payload_raw: Any = None,
    handoff_run_id: Optional[int] = None,
    assignee: Optional[str] = None,
    current_run_id: Optional[int] = None,
    target_assignee: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    """Return ``(implementer, source)`` for a card being sent back for rework.

    ``handoff_payload_raw`` is the review_requested event's raw payload column
    (a JSON text) or an already-parsed object. A payload that cannot be read
    as an object is CORRUPT and refuses outright: the board's record of the
    handoff is untrustworthy, so nothing is inferred from it -- not even from
    the run history.

    ``source`` is ``None`` when the handoff event already carried the
    implementer (nothing was repaired) and one of the ``PROVENANCE_*``
    markers when the value had to be recovered. A ``None`` implementer means
    there is no honest answer and the caller must refuse the transition.

    Resolution order:

    1. ``target_assignee`` -- the caller named the owner explicitly. It is
       provenance in its own right, so it also rescues a handoff that has none.
    2. the handoff event's own ``implementer`` field.
    3. the profile of the run that produced the handoff -- the historical
       worker, recovered from the board's run history.
    4. the last worker that touched the card before this review run.
    5. the card's registered assignee, but never the profile the handoff
       recorded as the reviewer: routing the findings back to the profile
       that wrote them is the misroute the refusal exists to prevent.

    Step 5's guard reads the handoff's *recorded* reviewer rather than the
    active run's profile on purpose. The active run's profile is copied off
    the card's assignee when the reviewer claims the card, so on a legacy
    handoff (which never recorded a reviewer) it IS the implementer and
    would veto the one answer the board still has.
    """
    explicit = clean_profile_name(target_assignee)
    if explicit:
        return explicit, PROVENANCE_EXPLICIT

    payload = handoff_payload(handoff_payload_raw)
    # A corrupt payload is not a missing one: refuse rather than infer.
    if payload is None:
        return None, None

    recorded = _stripped(payload.get("implementer"))
    if recorded:
        return recorded, None

    profile = run_profile(conn, handoff_run_id)
    if profile:
        return profile, PROVENANCE_HANDOFF_RUN

    profile = last_worker_profile(
        conn, task_id, exclude_run_id=current_run_id, before_run_id=handoff_run_id,
    )
    if profile:
        return profile, PROVENANCE_LAST_RUN

    registered = clean_profile_name(assignee)
    reviewer = clean_profile_name(payload.get("reviewer"))
    if registered and registered != reviewer:
        return registered, PROVENANCE_ASSIGNEE
    return None, None


def claim_review_task(
    conn: sqlite3.Connection, task_id: str, *, ttl_seconds: Optional[int] = None,
    claimer: Optional[str] = None,
) -> Optional[Any]:
    """Atomic ``review -> running`` (None when lost). Parents are re-checked
    (one may have reopened meanwhile) and a NEW run tracks the reviewer
    separately from the implementer."""
    now = int(time.time())
    lock = claimer or _kb._claimer_id()
    expires = now + _kb._resolve_claim_ttl_seconds(ttl_seconds)
    with _kb.write_txn(conn):
        if not _kb._parents_satisfied(conn, task_id):
            demoted = conn.execute(
                "UPDATE tasks SET status = 'todo' "
                "WHERE id = ? AND status = 'review' AND claim_lock IS NULL", (task_id,),
            )
            if demoted.rowcount == 1:
                _kb._append_event(
                    conn, task_id, "dependency_wait",
                    {"reason": "parent_reopened", "source_status": "review"},
                )
            return None
        run_id = _kb._claim_and_open_run(
            conn, task_id, "review", lock, expires, now, event_extra={"source_status": "review"},
        )
        if run_id is None:
            return None
        return _kb.get_task(conn, task_id)


def retry_status_for_run(
    conn: sqlite3.Connection, task_id: str, run_id: Optional[int] = None,
) -> str:
    """``review`` when the run's ``claimed`` event says ``source_status=review``,
    else ``ready`` -- one place, so crash/timeout/reclaim can't silently turn a
    reviewer's run into an implementation run."""
    if run_id is None:
        run_id = _kb._current_run_id(conn, task_id)
    if run_id is None:
        return "ready"
    event = _kb._latest_event(conn, task_id, "claimed", run_id)
    payload = _kb._json_dict(_kb._row_get(event, "payload"))
    return "review" if payload.get("source_status") == "review" else "ready"


# The board's own helpers, reached late-bound so this module can be imported
# before ``kanban_db`` has finished loading.
from hermes_cli import kanban_db as _kb
