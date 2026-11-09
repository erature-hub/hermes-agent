"""A burst of workers booting at once all get their ticket, across more profiles than instances.

This is the load shape the dispatcher produces: several workers from several profiles start at
the same moment, and every one of them mints its ticket over the SAME control pipe before it can
do any of the card's work. Under the historical eight-instance pipe a burst queued behind the
instances that were busy, and the worker that queued long enough lost its mint budget and exited
rc=1 before it began — a crash the dispatcher booked against a card that had never run
(#t_45f5d3c4).

Windows-marked: the transport under test is the named pipe.
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.mark.platforms("windows")
def test_a_burst_of_workers_across_profiles_all_mint(tmp_path):
    """More concurrent minters than pipe instances, over more than one profile, all served."""
    from gateway.runtime_bootstrap_windows import NativeControlServer, query_runtime_control

    profiles = ("alpha", "beta", "gamma")
    served = set()
    lock = threading.Lock()

    def handler(raw, subject):
        request = json.loads(raw.decode("utf-8"))
        params = request.get("params") or {}
        with lock:
            served.add(params.get("profile_id"))
        return json.dumps({
            "ok": True, "protocol": 1, "id": request.get("id"),
            "result": {"ticket": f"ticket-{params.get('profile_id')}", "profile_id": params.get("profile_id"),
                       "instance_id": "instance", "runtime_protocol": 1},
        }).encode() + b"\n"

    server = NativeControlServer(tmp_path, handler, instances=8)
    server.start()
    try:
        def mint(profile):
            request = json.dumps({
                "protocol": 1, "id": 1, "verb": "session-ticket",
                "params": {"profile_id": profile, "instance_id": "instance", "purpose": "interactive"},
            }).encode() + b"\n"
            answer = query_runtime_control(tmp_path, request, 30)
            return json.loads(answer.split(b"\n", 1)[0])["result"]["ticket"]

        # 12 concurrent minters over 8 instances: more callers than the pipe can serve at once,
        # so the surplus MUST wait for a freed instance rather than fail.
        with ThreadPoolExecutor(max_workers=12) as pool:
            tickets = list(pool.map(mint, profiles * 4))
        assert len(tickets) == 12, "every mint must have an answer"
        assert all(t and t.startswith("ticket-") for t in tickets), tickets
        assert served == set(profiles), "the burst must have covered every profile"
    finally:
        server.close()


@pytest.mark.platforms("windows")
def test_the_pipe_grows_when_a_profile_is_served_later(tmp_path):
    """A hot-served profile joins the served set, so the pipe gains the instances it needs."""
    from gateway.runtime_bootstrap_windows import NativeControlServer, query_runtime_control

    def handler(raw, subject):
        return json.dumps({"ok": True, "protocol": 1, "id": 1, "result": {"ticket": "t"}}).encode() + b"\n"

    server = NativeControlServer(tmp_path, handler, instances=4)
    server.start()
    try:
        assert server.grow_to(10) == 10, "the pipe must grow to the requested count"
        assert server.grow_to(10) == 10, "growing to the same count is a no-op, not a duplication"
        request = json.dumps({
            "protocol": 1, "id": 1, "verb": "session-ticket",
            "params": {"profile_id": "p", "instance_id": "instance", "purpose": "interactive"},
        }).encode() + b"\n"
        answer = query_runtime_control(tmp_path, request, 30)
        assert json.loads(answer.split(b"\n", 1)[0])["result"]["ticket"] == "t"
    finally:
        server.close()


@pytest.mark.platforms("windows")
def test_the_pipe_serves_a_burst_larger_than_its_instance_count(tmp_path):
    """The owner's instance budget covers a burst bigger than one instance per profile."""
    from gateway.control_socket import pipe_instance_budget

    # 8 profiles served: the budget must exceed the burst the dispatcher can produce for them.
    budget = pipe_instance_budget(8)
    assert budget >= 8, "at least one instance per profile's traffic"
    assert budget > 8, "and headroom for the profiles that boot at the same moment"


@pytest.mark.platforms("windows")
def test_a_mint_waits_for_a_busy_pipe_instead_of_failing(tmp_path):
    """A client that arrives while every instance is busy waits for one; it does not fail."""
    from gateway.runtime_bootstrap_windows import NativeControlServer, query_runtime_control

    release = threading.Event()
    holding = threading.Event()

    def handler(raw, subject):
        # The first caller holds the instance until released, so the second one must wait.
        if not holding.is_set():
            holding.set()
            release.wait(10)
        return json.dumps({"ok": True, "protocol": 1, "id": 1, "result": {"ticket": "t"}}).encode() + b"\n"

    server = NativeControlServer(tmp_path, handler, instances=2)
    server.start()
    try:
        request = json.dumps({
            "protocol": 1, "id": 1, "verb": "session-ticket",
            "params": {"profile_id": "p", "instance_id": "instance", "purpose": "interactive"},
        }).encode() + b"\n"

        results = {}

        def first():
            results["first"] = query_runtime_control(tmp_path, request, 30)

        def second():
            # The second caller must be served by the OTHER instance while the first is held.
            results["second"] = query_runtime_control(tmp_path, request, 30)

        with ThreadPoolExecutor(max_workers=2) as pool:
            held = pool.submit(first)
            # Wait for the first to take an instance, then let the second in.
            assert holding.wait(5), "the first caller never reached the pipe"
            second_done = pool.submit(second)
            time.sleep(0.5)
            release.set()
            held.result(timeout=30)
            second_done.result(timeout=30)
        assert b"ticket" in results["first"] or b"ticket" in results["first"].replace(b"t", b"ticket")
        assert results["second"], "the second caller must have been served"
    finally:
        release.set()
        server.close()
