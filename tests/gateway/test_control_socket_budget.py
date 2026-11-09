"""The owner's control socket publishes what its own exchanges cost, and serves enough of them.

A worker mints its ticket over the owner's control socket BEFORE it has done any of the card's
work, so a mint that misses its budget ends a worker that never started. Two things make that
survivable: the owner publishes how long its own exchanges take (the client budgets its mint from
that), and the owner serves enough pipe instances for the profiles it answers for (a multiplexer
answers for every served profile through ONE pipe, so the instance count has to cover the served
set, not one profile's traffic) (#t_45f5d3c4).
"""

import json
import threading
import time
from pathlib import Path

import pytest

from gateway.control_socket import (
    CONTROL_BUDGET_CEILING_S,
    CONTROL_BUDGET_FLOOR_S,
    GatewayControlServer,
    build_identify_payload,
)


def test_the_owner_publishes_its_exchange_budget():
    """The ``identify`` answer carries the budget a client should allow one exchange."""
    payload = build_identify_payload()
    assert "control_budget_s" in payload, "a client cannot budget a mint without this"
    assert payload["control_budget_s"] >= CONTROL_BUDGET_FLOOR_S
    assert payload["control_budget_s"] <= CONTROL_BUDGET_CEILING_S


def test_the_published_budget_grows_with_what_the_owner_actually_pays(monkeypatch):
    """A slow owner publishes a longer window; a fast one keeps the floor."""
    from gateway import control_socket

    budget = control_socket.ControlBudget()
    assert budget.budget_seconds() == CONTROL_BUDGET_FLOOR_S
    budget.record(0.01)
    assert budget.budget_seconds() == CONTROL_BUDGET_FLOOR_S, "a fast owner keeps the floor"
    budget.record(20.0)
    assert budget.budget_seconds() > CONTROL_BUDGET_FLOOR_S, "a slow owner publishes a longer window"
    # Bounded: an old measurement from a different host state must not stretch today's budget.
    for _ in range(50):
        budget.record(0.01)
    assert budget.budget_seconds() == CONTROL_BUDGET_FLOOR_S


def test_the_pipe_serves_the_profiles_it_answers_for():
    """A multiplexer's one pipe carries every served profile's traffic, so it needs the instances."""
    from gateway.control_socket import (
        PIPE_INSTANCES,
        PIPE_INSTANCES_CEILING,
        pipe_instance_budget,
    )
    # A standalone owner keeps what it always had.
    assert pipe_instance_budget(0) == PIPE_INSTANCES
    assert pipe_instance_budget(1) == PIPE_INSTANCES
    # More profiles, more instances: the queue a busy multiplexer creates is what made a worker
    # lose its mint budget and die before it began.
    assert pipe_instance_budget(8) > PIPE_INSTANCES
    assert pipe_instance_budget(8) > pipe_instance_budget(4)
    # Bounded: a pathological profile count must not spawn an unbounded number of threads.
    assert pipe_instance_budget(10_000) <= PIPE_INSTANCES_CEILING


def test_the_control_server_sizes_its_pipe_from_the_served_set(tmp_path):
    """The server reads the served set off its ticket store — the profiles it answers for."""
    from gateway.runtime_bootstrap import TicketStore

    server = GatewayControlServer(tmp_path)
    server.ticket_store = TicketStore("instance", frozenset({f"profile-{i}" for i in range(6)}))
    assert server._pipe_instance_budget() > 8


@pytest.mark.platforms("windows")
def test_a_worker_mints_its_ticket_while_the_owner_is_stalled(tmp_path):
    """The regression: the owner takes longer than the historical budget, and the worker still mints.

    The owner here answers slowly on purpose — the shape of a gateway whose event loop is stalled
    under GIL pressure. The worker must still get its ticket rather than exiting rc=1 before it
    did any work.
    """
    from gateway.control_socket import build_identify_payload
    from gateway.runtime_bootstrap_windows import NativeControlServer, query_runtime_control

    slow = threading.Event()
    served = threading.Event()

    def handler(raw, subject):
        request = json.loads(raw.decode("utf-8"))
        # The first exchange is slow (the stall); the rest answer promptly.
        if not slow.is_set():
            slow.set()
            time.sleep(6)
        served.set()
        if request.get("verb") == "identify":
            return json.dumps(build_identify_payload()).encode() + b"\n"
        return json.dumps({"ok": True, "protocol": 1, "id": request.get("id"),
                           "result": {"ticket": "t"}}).encode() + b"\n"

    server = NativeControlServer(tmp_path, handler)
    server.start()
    try:
        request = json.dumps({
            "protocol": 1, "id": 1, "verb": "session-ticket",
            "params": {"profile_id": "p", "instance_id": "instance", "purpose": "interactive"},
        }).encode() + b"\n"
        # The owner published a budget that covers its own slow answer, so the mint waits it out.
        answer = query_runtime_control(tmp_path, request, 30)
        assert json.loads(answer.split(b"\n", 1)[0])["result"]["ticket"] == "t"
        assert served.is_set(), "the owner must have answered"
    finally:
        server.close()


@pytest.mark.platforms("windows")
def test_a_stalled_owner_publishes_a_budget_a_mint_can_survive(tmp_path):
    """The stall the card names: an exchange slower than five seconds, and a client that waits.

    Under the historical fixed five-second budget this mint died before the worker had done
    anything at all. The owner's own published budget — which now reflects the slow exchange it
    just served — is what the client budgets the mint from, so the worker survives.
    """
    from gateway.control_socket import GatewayControlServer, _CONTROL_BUDGET
    from gateway.runtime_bootstrap_windows import NativeControlServer, query_runtime_control

    stall_s = 6.0  # longer than the historical five-second budget
    _CONTROL_BUDGET._costs.clear()  # this owner's own record, not another test's

    def slow_mint(params):
        time.sleep(stall_s)
        return {"ticket": "t"}

    # The REAL server, so the budget recording and the identify payload are the live wiring.
    owner = GatewayControlServer(tmp_path, verb_handlers={"slow-mint": slow_mint})
    pipe = NativeControlServer(tmp_path, owner.handle_request_line)
    pipe.start()
    try:
        # The owner serves a slow exchange first, so it now knows what its exchanges cost.
        slow = json.dumps({
            "protocol": 1, "id": 1, "verb": "slow-mint",
            "params": {"profile_id": "p", "instance_id": "instance", "purpose": "interactive"},
        }).encode() + b"\n"
        query_runtime_control(tmp_path, slow, 30)

        # The identify now publishes a budget that covers the exchange the owner just served.
        identify = json.dumps({"protocol": 1, "verb": "identify", "id": 1}).encode() + b"\n"
        answer = json.loads(query_runtime_control(tmp_path, identify, 30).split(b"\n", 1)[0])
        published = answer["result"]["control_budget_s"]
        assert published > stall_s, \
            "the owner must publish a budget that covers the exchange it just served"

        # The mint is slow too, and the client's budget comes from the owner's own number.
        mint = json.dumps({
            "protocol": 1, "id": 1, "verb": "slow-mint",
            "params": {"profile_id": "p", "instance_id": "instance", "purpose": "interactive"},
        }).encode() + b"\n"
        answer = query_runtime_control(tmp_path, mint, published)
        assert json.loads(answer.split(b"\n", 1)[0])["result"]["ticket"] == "t", \
            "the worker must have survived the stall"

        # And under the historical fixed budget the same mint would have failed.
        with pytest.raises(TimeoutError):
            query_runtime_control(tmp_path, mint, 5)
    finally:
        pipe.close()
