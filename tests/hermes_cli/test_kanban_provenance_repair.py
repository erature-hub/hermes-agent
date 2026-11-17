"""Provenance repair for review handoffs that predate the implementer field.

``request_review`` stamps the ``review_requested`` event with the profile that
did the work, so ``request_changes`` can hand the card back to it. Cards
handed off before that field existed carry a payload with no ``implementer``
key at all, and ``request_changes`` refused them mechanically -- "review
handoff has no valid implementer provenance" -- so a reviewer's verdict could
not be delivered: the card sat in review until an operator intervened by hand
(incident card ``t_5cfe58e6``, tracked as ``t_a4c9e812``).

These tests pin two things:

* the repair -- the implementer is recovered from the board's own run history
  (the run that produced the handoff IS the implementer), so a legacy card
  transitions to ``ready`` assigned to the historical worker; and
* the one case where refusing is still correct -- a card nobody worked but its
  reviewer. Routing it back would send the findings to the profile that wrote
  them, which is the misroute the refusal exists to prevent.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_review as provenance


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _events(conn, tid, kind=None):
    rows = conn.execute(
        "SELECT kind, payload FROM task_events WHERE task_id = ? ORDER BY id",
        (tid,),
    ).fetchall()
    out = [
        (r["kind"], json.loads(r["payload"]) if r["payload"] else None)
        for r in rows
    ]
    if kind is not None:
        out = [e for e in out if e[0] == kind]
    return out


def _event(conn, tid, kind):
    found = _events(conn, tid, kind=kind)
    assert found, f"no {kind} event on {tid}"
    return found[-1][1]


def _legacy_handoff(conn, tid, *, run_id, summary="legacy handoff", implementer=None):
    """Rewrite a card's review handoff the way the pre-provenance writer did.

    The legacy writer stamped no ``implementer`` on the event and left the
    card assigned to whoever worked it -- there was no reviewer concept yet.
    ``implementer`` defaults to omitting the key entirely, which is what the
    incident card's payload looked like::

        {"summary": "...", "reviewer": null}
    """
    now = int(time.time())
    conn.execute(
        "UPDATE tasks SET status = 'review', current_run_id = NULL, claim_lock = NULL, "
        "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
        (tid,),
    )
    conn.execute(
        "UPDATE task_runs SET status = 'review', outcome = 'review_requested', "
        "ended_at = ? WHERE id = ?",
        (now, run_id),
    )
    payload: dict = {"summary": summary, "reviewer": None}
    if implementer is not None:
        payload["implementer"] = implementer
    conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
        "VALUES (?, ?, 'review_requested', ?, ?)",
        (tid, run_id, json.dumps(payload), now),
    )


def _worked_card(conn, *, title="legacy card", assignee="cypher"):
    """A card claimed and worked by ``assignee``; returns (task_id, run_id)."""
    tid = kb.create_task(conn, title=title, assignee=assignee)
    claimed = kb.claim_task(conn, tid)
    assert claimed is not None
    return tid, claimed.current_run_id


def test_legacy_handoff_without_implementer_key_routes_to_the_historical_worker(
    kanban_home: Path,
) -> None:
    """The incident: a handoff event with NO ``implementer`` key at all.

    The rework must go to the worker the handoff run belongs to, and the card
    must land on ``ready`` -- not stay stranded in review.
    """
    with kbc.connect() as conn:
        tid, run_id = _worked_card(conn, assignee="cypher")
        _legacy_handoff(conn, tid, run_id=run_id)

        review = kb.claim_review_task(conn, tid, claimer="cypher")
        assert review is not None

        ok, implementer = kb.request_changes(conn, tid, reason="3 defects")
        assert ok is True
        assert implementer == "cypher"

        card = kb.get_task(conn, tid)
        assert card.status == "ready"
        assert card.assignee == "cypher"


def test_legacy_handoff_with_a_null_implementer_routes_to_the_historical_worker(
    kanban_home: Path,
) -> None:
    """``"implementer": null`` is the same missing provenance as no key."""
    with kbc.connect() as conn:
        tid, run_id = _worked_card(conn, assignee="cypher")
        _legacy_handoff(conn, tid, run_id=run_id, implementer=None)
        # Write the key explicitly as null (the legacy writer's other shape).
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE task_id = ? AND kind = 'review_requested'",
            (json.dumps({"summary": "legacy handoff", "reviewer": None,
                         "implementer": None}), tid),
        )

        assert kb.claim_review_task(conn, tid, claimer="cypher") is not None
        ok, implementer = kb.request_changes(conn, tid, reason="needs work")
        assert ok is True
        assert implementer == "cypher"
        assert kb.get_task(conn, tid).assignee == "cypher"


def test_repaired_rework_goes_to_the_worker_not_to_the_current_owner(
    kanban_home: Path,
) -> None:
    """The card's present owner is not the implementer.

    Once the card is in review its owner is the reviewer; the rework must go
    back to whoever did the work. A legacy handoff names nobody, so the only
    honest source left is the run the handoff belongs to.
    """
    with kbc.connect() as conn:
        tid, run_id = _worked_card(conn, assignee="cypher")
        _legacy_handoff(conn, tid, run_id=run_id)

        # The card is handed to a reviewer who owns it from here on.
        assert kb.assign_task(conn, tid, "lead-reviewer") is True
        review = kb.claim_review_task(conn, tid, claimer="lead-reviewer")
        assert review is not None

        ok, implementer = kb.request_changes(conn, tid, reason="fix the defects")
        assert ok is True
        assert implementer == "cypher"

        card = kb.get_task(conn, tid)
        assert card.status == "ready"
        assert card.assignee == "cypher"


def test_legacy_handoff_without_a_run_id_routes_to_the_last_worker(
    kanban_home: Path,
) -> None:
    """No run on the event: the last worker that touched the card answers."""
    with kbc.connect() as conn:
        tid, run_id = _worked_card(conn, assignee="cypher")
        _legacy_handoff(conn, tid, run_id=run_id)
        # Detach the event from its run -- the run is still in the history.
        conn.execute(
            "UPDATE task_events SET run_id = NULL WHERE task_id = ? AND kind = 'review_requested'",
            (tid,),
        )

        assert kb.claim_review_task(conn, tid, claimer="cypher") is not None
        ok, implementer = kb.request_changes(conn, tid, reason="fix it")
        assert ok is True
        assert implementer == "cypher"


def test_legacy_handoff_falls_back_to_the_registered_assignee(
    kanban_home: Path,
) -> None:
    """No run evidence at all: the card's registered assignee is the answer.

    The handoff recorded no reviewer either, so the assignee is not the
    profile that wrote the findings and is safe to route back to.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="no run history", assignee="cypher")
        # A handoff event with no run, no implementer and no reviewer.
        conn.execute(
            "UPDATE tasks SET status = 'review' WHERE id = ?", (tid,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
            "VALUES (?, NULL, 'review_requested', ?, ?)",
            (tid, json.dumps({"summary": "handed off", "reviewer": None}), int(time.time())),
        )

        assert kb.claim_review_task(conn, tid, claimer="cypher") is not None
        ok, implementer = kb.request_changes(conn, tid, reason="please rework")
        assert ok is True
        assert implementer == "cypher"
        assert kb.get_task(conn, tid).status == "ready"


def test_handoff_assigned_to_its_reviewer_still_refuses(kanban_home: Path) -> None:
    """A card nobody worked but its reviewer has no honest implementer.

    Routing it back would send the findings to the profile that wrote them,
    so the transition must refuse -- the one case the repair must not touch.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already applied", assignee="reviewer-a")
        assert kb.request_review(
            conn, tid, summary="review this", reviewer="reviewer-a",
        ) is True

        ev = _event(conn, tid, "review_requested")
        assert ev["reviewer"] == "reviewer-a"
        assert ev["implementer"] is None

        assert kb.claim_review_task(conn, tid, claimer="reviewer-a") is not None
        ok, reason = kb.request_changes(conn, tid, reason="found 3 issues")
        assert ok is False
        assert "implementer provenance" in (reason or "")


def test_repair_is_auditable_from_the_event_log(kanban_home: Path) -> None:
    """A repaired handoff says where the implementer came from."""
    with kbc.connect() as conn:
        tid, run_id = _worked_card(conn, assignee="cypher")
        _legacy_handoff(conn, tid, run_id=run_id)

        assert kb.claim_review_task(conn, tid, claimer="cypher") is not None
        ok, implementer = kb.request_changes(conn, tid, reason="needs work")
        assert ok is True
        assert implementer == "cypher"

        changes = _event(conn, tid, "changes_requested")
        assert changes["implementer"] == "cypher"
        assert changes["provenance_source"] == provenance.PROVENANCE_HANDOFF_RUN


def test_modern_handoff_records_no_provenance_source(kanban_home: Path) -> None:
    """A handoff that carried its own implementer is not a repair."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="modern", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        assert kb.request_review(
            conn, tid, summary="v1", reviewer="lead-reviewer",
            expected_run_id=claimed.current_run_id,
        ) is True

        assert kb.claim_review_task(conn, tid, claimer="lead-reviewer") is not None
        ok, implementer = kb.request_changes(conn, tid, reason="small fix")
        assert ok is True
        assert implementer == "worker"

        changes = _event(conn, tid, "changes_requested")
        assert changes["implementer"] == "worker"
        assert "provenance_source" not in changes


def test_target_assignee_overrides_the_recorded_implementer(kanban_home: Path) -> None:
    """An explicit target wins over the handoff's own provenance."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="modern", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(
            conn, tid, summary="v1", reviewer="lead-reviewer",
            expected_run_id=claimed.current_run_id,
        ) is True

        assert kb.claim_review_task(conn, tid, claimer="lead-reviewer") is not None
        ok, implementer = kb.request_changes(
            conn, tid, reason="route elsewhere", target_assignee="cypher",
        )
        assert ok is True
        assert implementer == "cypher"

        card = kb.get_task(conn, tid)
        assert card.status == "ready"
        assert card.assignee == "cypher"

        changes = _event(conn, tid, "changes_requested")
        assert changes["implementer"] == "cypher"
        assert changes["provenance_source"] == provenance.PROVENANCE_EXPLICIT


def test_target_assignee_rescues_a_handoff_with_no_provenance_at_all(
    kanban_home: Path,
) -> None:
    """The caller's word replaces provenance the board cannot supply."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="already applied", assignee="reviewer-a")
        assert kb.request_review(
            conn, tid, summary="review this", reviewer="reviewer-a",
        ) is True
        assert kb.claim_review_task(conn, tid, claimer="reviewer-a") is not None

        ok, implementer = kb.request_changes(
            conn, tid, reason="assign it explicitly", target_assignee="cypher",
        )
        assert ok is True
        assert implementer == "cypher"

        card = kb.get_task(conn, tid)
        assert card.status == "ready"
        assert card.assignee == "cypher"


def test_target_assignee_is_ignored_when_blank(kanban_home: Path) -> None:
    """A blank target is no target -- the handoff's provenance still decides."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="modern", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(
            conn, tid, summary="v1", reviewer="lead-reviewer",
            expected_run_id=claimed.current_run_id,
        ) is True

        assert kb.claim_review_task(conn, tid, claimer="lead-reviewer") is not None
        ok, implementer = kb.request_changes(
            conn, tid, reason="small fix", target_assignee="   ",
        )
        assert ok is True
        assert implementer == "worker"


def test_incident_card_replica_transitions_to_ready(kanban_home: Path) -> None:
    """A replica of ``t_5cfe58e6``'s stranded state, end to end.

    The live card was implemented by ``cypher``, handed to review by a writer
    that stamped no implementer, and its reviewer's ``request_changes`` was
    refused -- the card sat in review until an operator intervened. Here the
    same state must resolve on its own.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn,
            title="Brief pipeline: cluster-dedup near-duplicate researcher briefs",
            assignee="cypher",
        )
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None

        # The implementation run completed, then the legacy handoff landed.
        conn.execute(
            "UPDATE task_runs SET status = 'completed', outcome = 'completed' "
            "WHERE id = ?",
            (claimed.current_run_id,),
        )
        _legacy_handoff(
            conn, tid, run_id=claimed.current_run_id,
            summary="Opened PR #702 with 14 unit tests.",
        )

        review = kb.claim_review_task(conn, tid, claimer="cypher")
        assert review is not None

        ok, implementer = kb.request_changes(
            conn, tid,
            reason="PR #702 review completed with changes requested (3 defects).",
        )
        assert ok is True
        assert implementer == "cypher"

        card = kb.get_task(conn, tid)
        assert card.status == "ready"
        assert card.assignee == "cypher"
        assert card.current_run_id is None


def test_repaired_card_completes_the_loop_and_heals_its_own_handoff(
    kanban_home: Path,
) -> None:
    """After the repair the next handoff carries provenance of its own.

    The repair is not a one-off patch: the card's next review request is
    written by the current writer, so the provenance question does not recur.
    """
    with kbc.connect() as conn:
        tid, run_id = _worked_card(conn, assignee="cypher")
        _legacy_handoff(conn, tid, run_id=run_id)

        assert kb.claim_review_task(conn, tid, claimer="cypher") is not None
        ok, implementer = kb.request_changes(conn, tid, reason="needs work")
        assert ok is True
        assert implementer == "cypher"

        # The implementer picks the card up and hands it back for review.
        rework = kb.claim_task(conn, tid, claimer="cypher")
        assert rework is not None
        assert kb.request_review(
            conn, tid, summary="fixed", reviewer="lead-reviewer",
            expected_run_id=rework.current_run_id,
        ) is True

        handoff = _event(conn, tid, "review_requested")
        assert handoff["implementer"] == "cypher"
        assert handoff["reviewer"] == "lead-reviewer"

        # And the next verdict routes without any repair at all.
        assert kb.claim_review_task(conn, tid, claimer="lead-reviewer") is not None
        ok, implementer = kb.request_changes(conn, tid, reason="one more thing")
        assert ok is True
        assert implementer == "cypher"
        assert "provenance_source" not in _event(conn, tid, "changes_requested")


def test_resolver_refuses_without_any_evidence(kanban_home: Path) -> None:
    """The resolver itself refuses when nothing names an implementer."""
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="no evidence", assignee="reviewer-a")
        implementer, source = provenance.resolve_implementer(
            conn, tid,
            handoff_payload_raw={"reviewer": "reviewer-a"},
            handoff_run_id=None,
            assignee="reviewer-a",
            current_run_id=None,
        )
        assert implementer is None
        assert source is None


def test_tool_layer_accepts_target_assignee(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The native tool passes the explicit rework owner through to the board."""
    from tools import kanban_tools as tools

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    # The board refuses a delegated-child mutation, and this session runs
    # inside one; the tool surface is exercised as a dispatcher worker.
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    # ``request_review`` rejects a reviewer that is not an installed profile.
    for profile in ("lead-reviewer", "cypher"):
        (home / "profiles" / profile).mkdir(parents=True)
        (home / "profiles" / profile / "config.yaml").write_text("{}\n")
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="tool layer", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        assert claimed is not None
        assert kb.request_review(
            conn, tid, summary="v1", reviewer="lead-reviewer",
            expected_run_id=claimed.current_run_id,
        ) is True
        review = kb.claim_review_task(conn, tid, claimer="lead-reviewer")
        assert review is not None

    monkeypatch.setenv("HERMES_PROFILE", "lead-reviewer")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(review.current_run_id))
    out = json.loads(tools._handle_request_changes({
        "reason": "route the rework to cypher",
        "target_assignee": "cypher",
    }))

    assert out["ok"] is True
    assert out["implementer"] == "cypher"
    with kbc.connect() as conn:
        card = kb.get_task(conn, tid)
        assert card.status == "ready"
        assert card.assignee == "cypher"


@pytest.mark.parametrize("bad_payload", ["{not-json", "[]", None])
def test_corrupt_handoff_payload_fails_closed(kanban_home: Path, bad_payload) -> None:
    """A corrupt handoff event is not a legacy one: it refuses.

    A legacy event is a well-formed object that predates the ``implementer``
    field, and the run history can answer for it. A CORRUPT event -- one that
    cannot be read as an object at all -- means the board's record of the
    handoff is untrustworthy, so nothing is inferred from it, not even from
    the run history. The card must stay exactly where it was.
    """
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="corrupt handoff", assignee="builder")
        claimed = kb.claim_task(conn, tid)
        assert kb.request_review(
            conn, tid, summary="Ready.", reviewer="reviewer",
            expected_run_id=claimed.current_run_id,
        ) is True
        conn.execute(
            "UPDATE task_events SET payload = ? "
            "WHERE task_id = ? AND kind = 'review_requested'",
            (bad_payload, tid),
        )
        conn.commit()

        review = kb.claim_review_task(conn, tid, claimer="reviewer")
        assert review is not None

        ok, detail = kb.request_changes(conn, tid, reason="Needs changes.")
        assert ok is False
        assert "implementer provenance" in (detail or "")

        card = kb.get_task(conn, tid)
        assert card.status == "running"
        assert card.assignee == "reviewer"
        assert card.current_run_id == review.current_run_id
