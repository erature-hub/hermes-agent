"""Tests for the Hermes-side JEV pacer client and the advice it carries.

The pacer lives in the aisuite service. These tests pin the Hermes side of
that contract: what we send, how we read the answer, and what happens when
the pacer is absent, slow, or answers with something that is not a pacing
response.

The fake server here speaks the JEV-2 contract (``POST /pacing`` taking a
``DispatchTick`` and answering ``PacingResponse``). It is deliberately
literal: the point of these tests is to pin the wire shape, not to re-implement
the pacer's policy.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hermes_cli import kanban_pacer as kp
from hermes_cli import kanban_pacer_advice as kpa


class _FakePacerHandler(BaseHTTPRequestHandler):
    """A minimal JEV-2 pacing endpoint.

    ``server.response`` is set by the test to the body to answer with, and
    ``server.requests`` collects what the client sent.
    """

    def log_message(self, *args):  # silence the test output
        pass

    def do_POST(self):  # BaseHTTPRequestHandler's name
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            self.server.requests.append(json.loads(raw.decode("utf-8")))
        except ValueError:
            self.server.requests.append({"__unparsed__": raw.decode("utf-8", "replace")})
        self.server.tokens.append(self.headers.get("X-Erature-Service-Token"))

        status, content_type, body = self.server.response
        payload = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def fake_pacer():
    """A running fake pacer plus the knobs the tests turn."""
    server = HTTPServer(("127.0.0.1", 0), _FakePacerHandler)
    server.requests = []
    server.tokens = []
    server.response = (200, "application/json", json.dumps({
        "decision": {
            "action": "paced_drain",
            "stalled": True,
            "max_spawns_per_tick": 1,
            "drain_order": ["t_a", "t_b"],
            "deferred_order": ["t_c"],
        },
        "receipt": {
            "class_id": "f",
            "class_name": "dispatcher_stalled_queue_full",
            "dedupe_key": "f:dispatcher_stalled_queue_full:1000.000",
            "should_notify": True,
            "suppressed": False,
            "headline": "Your board has had 3 card(s) waiting.",
            "evidence": "ready=3 (peak 3) todo=1 · barren 3",
            "operator_verify": ["hermes kanban list --status ready"],
            "window_s": 1800.0,
            "ready_count": 3,
            "barren_ticks": 3,
            "quota_deferred": 0,
            "quota_state": "unknown",
            "observed_at": 1000.0,
        },
        "advisory": {
            "available": True,
            "model": "test-model",
            "action": "held",
            "confidence": 0.8,
            "latency_ms": 12.0,
            "budget_ms": 250.0,
            "budget_met": True,
            "reason": "gate tripping",
        },
    }))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _url(server) -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}/api/operations/micro-decisions/pacing"


# --- request shape ------------------------------------------------------------


def test_dispatch_tick_carries_the_pacer_inputs():
    """The body is exactly the pacer's declared field set."""
    tick = kp.build_dispatch_tick(
        tick_index=7,
        observed_at=1000.5,
        ready_count=3,
        todo_count=1,
        spawned_count=0,
        quota_deferred=2,
        quota_state="tripped",
        worker_cap=4,
        spawn_budget=2,
        drain_candidates=(
            kp.DrainCandidate("t_a", priority=5, assignee="alice", ready_since=900.0),
        ),
    )
    assert tick == {
        "tick_index": 7,
        "observed_at": 1000.5,
        "ready_count": 3,
        "todo_count": 1,
        "spawned_count": 0,
        "quota_deferred": 2,
        "quota_state": "tripped",
        "worker_cap": 4,
        "spawn_budget": 2,
        "drain_candidates": [
            {"task_id": "t_a", "priority": 5, "assignee": "alice", "ready_since": 900.0},
        ],
    }


def test_an_unrecognized_quota_state_degrades_to_unknown():
    """A gate state the pacer does not know is never invented."""
    tick = kp.build_dispatch_tick(
        tick_index=0, observed_at=1.0, ready_count=1, quota_state="sideways",
    )
    assert tick["quota_state"] == "unknown"


def test_a_missing_quota_gate_is_unknown_not_ok():
    """No quota gate on this checkout means UNKNOWN, never a fabricated 'ok'."""
    class _Result:
        pass

    assert kp.map_quota_state(_Result()) == "unknown"
    assert kp.map_quota_deferred(_Result()) == 0


def test_a_present_quota_gate_is_passed_through():
    """A gate that IS present reports its real state and deferral count."""
    class _Result:
        quota_state = "TRIPPED"
        quota_deferred = ["t_a", "t_b"]

    assert kp.map_quota_state(_Result()) == "tripped"
    assert kp.map_quota_deferred(_Result()) == 2


# --- response shape -----------------------------------------------------------


def test_a_pacing_response_is_parsed_into_decision_and_receipt():
    body = json.dumps({
        "decision": {
            "action": "held",
            "stalled": True,
            "max_spawns_per_tick": 0,
            "drain_order": [],
            "deferred_order": ["t_a"],
            "hold_until": 1600.0,
            "rationale": "gate tripping",
            "evidence": "ready=3",
        },
        "receipt": {
            "class_id": "f",
            "class_name": "dispatcher_stalled_queue_full",
            "dedupe_key": "f:dispatcher_stalled_queue_full:1000.000",
            "should_notify": True,
            "suppressed": False,
            "evidence": "barren 3",
            "operator_verify": ["hermes kanban stats"],
        },
        "advisory": {"available": True, "action": "held", "confidence": 0.9},
    }).encode("utf-8")
    advisory = kp.parse_pacing_response(body)
    assert advisory is not None
    assert advisory.decision.action == "held"
    assert advisory.decision.stalled is True
    assert advisory.decision.max_spawns_per_tick == 0
    assert advisory.decision.deferred_order == ("t_a",)
    assert advisory.receipt is not None
    assert advisory.receipt.class_name == "dispatcher_stalled_queue_full"
    assert advisory.receipt.dedupe_key == "f:dispatcher_stalled_queue_full:1000.000"
    assert advisory.model_opinion == "held"
    assert advisory.model_confidence == 0.9


def test_an_absent_advisory_is_normal_not_an_error():
    """The model tier is consulted only while stalled; absence is normal."""
    body = json.dumps({
        "decision": {"action": "none", "stalled": False, "max_spawns_per_tick": 2},
        "receipt": {"class_name": "dispatcher_stalled_queue_full", "dedupe_key": ""},
        "advisory": None,
    }).encode("utf-8")
    advisory = kp.parse_pacing_response(body)
    assert advisory is not None
    assert advisory.model_opinion is None
    assert advisory.decision.action == "none"


def test_a_body_that_is_not_a_pacing_response_is_rejected():
    """HTML, junk, and JSON without a decision are all 'no advice'."""
    assert kp.parse_pacing_response(b"<html>frontend catch-all</html>") is None
    assert kp.parse_pacing_response(b"") is None
    assert kp.parse_pacing_response(b"not json") is None
    assert kp.parse_pacing_response(b'{"detail":"Not Found"}') is None
    assert kp.parse_pacing_response(b'[]') is None


def test_an_unknown_action_is_treated_as_escalate():
    """An action the client does not know must not be guessed as benign."""
    body = json.dumps({
        "decision": {"action": "have_a_nice_day", "stalled": True, "max_spawns_per_tick": 9},
    }).encode("utf-8")
    advisory = kp.parse_pacing_response(body)
    assert advisory is not None
    assert advisory.decision.action == "escalate"


# --- transport ----------------------------------------------------------------


def test_the_client_posts_the_tick_and_returns_the_advisory(fake_pacer):
    client = kp.PacerClient(url=_url(fake_pacer), token="secret-token")
    advisory = client.observe(
        kp.build_dispatch_tick(tick_index=1, observed_at=10.0, ready_count=2)
    )
    assert advisory is not None
    assert advisory.decision.action == "paced_drain"
    assert fake_pacer.requests[0]["tick_index"] == 1
    assert fake_pacer.tokens == ["secret-token"]


def test_a_non_json_200_is_not_an_advisory(fake_pacer):
    """The frontend catch-all answers 200 with HTML. That is not advice."""
    fake_pacer.response = (200, "text/html; charset=utf-8", "<html>catch-all</html>")
    client = kp.PacerClient(url=_url(fake_pacer), token="secret-token")
    assert client.observe(
        kp.build_dispatch_tick(tick_index=1, observed_at=10.0, ready_count=2)
    ) is None


def test_a_pacing_body_served_as_html_is_not_an_advisory(fake_pacer):
    """A catch-all that answers with JSON but an HTML content type is still
    a catch-all. The content type is checked independently of the body, so
    a proxy that mislabels a pacing response as HTML cannot smuggle advice
    in, and a catch-all that mislabels its JSON cannot either."""
    fake_pacer.response = (
        200,
        "text/html; charset=utf-8",
        json.dumps({
            "decision": {
                "action": "held",
                "stalled": True,
                "max_spawns_per_tick": 0,
                "drain_order": [],
                "deferred_order": [],
            },
            "receipt": None,
            "advisory": None,
        }),
    )
    client = kp.PacerClient(url=_url(fake_pacer), token="secret-token")
    assert client.observe(
        kp.build_dispatch_tick(tick_index=1, observed_at=10.0, ready_count=2)
    ) is None


def test_an_error_status_is_not_an_advisory(fake_pacer):
    fake_pacer.response = (401, "application/json", json.dumps({"detail": "nope"}))
    client = kp.PacerClient(url=_url(fake_pacer), token="bad")
    assert client.observe(
        kp.build_dispatch_tick(tick_index=1, observed_at=10.0, ready_count=2)
    ) is None


def test_an_unreachable_pacer_is_not_an_advisory():
    """A closed port is 'no advice', not an exception."""
    client = kp.PacerClient(url="http://127.0.0.1:1/api/operations/micro-decisions/pacing")
    assert client.observe(
        kp.build_dispatch_tick(tick_index=1, observed_at=10.0, ready_count=2)
    ) is None


# --- advice carried to the next tick ------------------------------------------


@pytest.fixture
def advice_dir(tmp_path, monkeypatch):
    """An isolated advice store."""
    monkeypatch.setenv(kpa.ADVICE_DIR_ENV, str(tmp_path / "pacer-state"))
    return tmp_path / "pacer-state"


def _advisory(action="paced_drain", *, max_spawns=1, drain=("t_a", "t_b"), stalled=True):
    """An advisory shaped like the real pacer's.

    The real pacer returns a receipt with an EMPTY ``dedupe_key`` when it is
    not stalled -- it reports a draining queue rather than naming an episode
    -- and a populated one when it is.
    """
    decision = kp.PacerDecision(
        action=action,
        stalled=stalled,
        max_spawns_per_tick=max_spawns,
        drain_order=tuple(drain),
        deferred_order=("t_c",),
        hold_until=1600.0,
        rationale="r",
        evidence="e",
    )
    receipt = kp.ClassFReceipt(
        class_id="f",
        class_name="dispatcher_stalled_queue_full",
        dedupe_key=(
            "f:dispatcher_stalled_queue_full:1000.000" if stalled else ""
        ),
        should_notify=bool(stalled),
        suppressed=not stalled,
        evidence="ready=3" if stalled else "",
        operator_verify=("hermes kanban list --status ready",) if stalled else (),
    )
    return kp.PacerAdvisory(decision=decision, receipt=receipt)


def test_recorded_advice_is_read_back(advice_dir):
    kpa.record_advice(_advisory(), board="b1")
    advice = kpa.read_advice(board="b1")
    assert advice is not None
    assert advice["action"] == "paced_drain"
    assert advice["drain_order"] == ["t_a", "t_b"]
    assert advice["receipt"]["class_name"] == "dispatcher_stalled_queue_full"


def test_advice_is_scoped_per_board(advice_dir):
    kpa.record_advice(_advisory(), board="b1")
    assert kpa.read_advice(board="b2") is None


def test_expired_advice_is_not_applied(advice_dir):
    """A stale verdict must never bind a later tick."""
    kpa.record_advice(_advisory(), board="b1", now=1000.0, ttl_s=10.0)
    assert kpa.read_advice(board="b1", now=1005.0) is not None
    assert kpa.read_advice(board="b1", now=2000.0) is None


def test_the_drain_order_reorders_the_candidates(advice_dir):
    kpa.record_advice(_advisory(drain=("t_b", "t_a")), board="b1")
    advice = kpa.read_advice(board="b1")
    assert kpa.drain_order_for(advice, ["t_a", "t_b", "t_c"]) == ["t_b", "t_a", "t_c"]


def test_a_card_the_pacer_named_but_that_is_gone_is_dropped(advice_dir):
    """The pacer ranked a queue that has since moved."""
    kpa.record_advice(_advisory(drain=("t_z", "t_b")), board="b1")
    advice = kpa.read_advice(board="b1")
    assert kpa.drain_order_for(advice, ["t_a", "t_b"]) == ["t_b", "t_a"]


def test_the_pacer_can_narrow_the_budget_but_never_widen_it(advice_dir):
    """The pacer tightens the tick's spawn budget; it cannot widen it."""
    kpa.record_advice(_advisory(max_spawns=1), board="b1")
    advice = kpa.read_advice(board="b1")
    assert kpa.claim_budget_for(advice, 5) == 1
    assert kpa.claim_budget_for(advice, 0) == 0
    # A pacer asking for MORE than the dispatcher allows is clamped down.
    kpa.record_advice(_advisory(max_spawns=99), board="b1")
    advice = kpa.read_advice(board="b1")
    assert kpa.claim_budget_for(advice, 2) == 2


def test_a_pacer_that_asks_for_nothing_intervention_leaves_the_budget_alone(advice_dir):
    """'none' means no intervention required, so the budget stands."""
    kpa.record_advice(_advisory(action="none", max_spawns=0), board="b1")
    advice = kpa.read_advice(board="b1")
    assert kpa.claim_budget_for(advice, 4) == 4


def test_no_advice_leaves_the_budget_and_order_untouched(advice_dir):
    assert kpa.claim_budget_for(None, 4) == 4
    assert kpa.drain_order_for(None, ["t_a", "t_b"]) == ["t_a", "t_b"]


def test_the_receipt_is_carried_verbatim_for_enrichment(advice_dir):
    """The receipt enriches the bus's event; the bus decides whether to page."""
    kpa.record_advice(_advisory(), board="b1")
    advice = kpa.read_advice(board="b1")
    receipt = kpa.receipt_enrichment(advice)
    assert receipt is not None
    assert receipt["class_name"] == "dispatcher_stalled_queue_full"
    assert receipt["dedupe_key"] == "f:dispatcher_stalled_queue_full:1000.000"
    assert receipt["operator_verify"] == ["hermes kanban list --status ready"]
    # The receipt's own should_notify is NOT the paging decision; it travels
    # as data so the bus can cross-check it against its own ledger.
    assert receipt["should_notify"] is True


def test_no_receipt_when_there_is_no_stall(advice_dir):
    """A draining queue names no episode, so there is nothing to enrich."""
    kpa.record_advice(_advisory(action="none", stalled=False), board="b1")
    advice = kpa.read_advice(board="b1")
    assert kpa.receipt_enrichment(advice) is None
