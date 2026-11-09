"""A worker that cannot mint its ticket on the first try waits the owner out instead of dying.

The ticket mint is the worker's FIRST act: nothing about the card has run yet, so a mint that
misses its budget is the host being busy, not the card failing. The owner publishes how long its
own control exchanges actually take (``control_budget_s`` on its ``identify`` answer), and the
client budgets its mint from that — retrying across the pipe's instances — so a worker booting
during the owner's stall survives it instead of exiting rc=1 (#t_45f5d3c4).

These tests pin the CLIENT half of that contract (the owner half is in
``tests/gateway/test_control_socket_budget.py``).
"""

import json
import time
from types import SimpleNamespace

import pytest

from hermes_cli import gateway_client


@pytest.fixture(autouse=True)
def _reset_published_budget():
    """Each test starts from the floor: no owner has published a budget yet."""
    gateway_client.publish_control_budget(None)
    gateway_client._CONTROL_BUDGET_S = None
    gateway_client._CLIENT_OBSERVED_S.clear()
    yield
    gateway_client._CONTROL_BUDGET_S = None
    gateway_client._CLIENT_OBSERVED_S.clear()


def _endpoint(**overrides):
    """The discovery endpoint a client holds after identifying its owner."""
    fields = {
        "profile_id": "default",
        "instance_id": "owner",
        "authority_epoch": 1,
        "runtime_protocol": 1,
        "api_origin": "http://127.0.0.1:1",
        "supervisor": "none",
        "capabilities": frozenset({"session-authority-v1"}),
        "control_home": None,
        "code_sha": None,
        "control_budget_s": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_a_published_budget_scales_the_client_budget():
    """The owner's own number is what the client waits, between the floor and the ceiling."""
    gateway_client.publish_control_budget(30.0)
    assert gateway_client.control_query_budget() == 30.0
    # Below the floor, the client keeps the floor: a fast owner should not wait minutes.
    gateway_client.publish_control_budget(0.5)
    assert gateway_client.control_query_budget() == gateway_client.CONTROL_QUERY_FLOOR_S
    # Above the ceiling, the client's bound holds: a pathological measurement cannot hang a boot.
    gateway_client.publish_control_budget(10_000.0)
    assert gateway_client.control_query_budget() == gateway_client.CONTROL_QUERY_CEILING_S


def test_no_published_budget_means_the_floor():
    gateway_client._CONTROL_BUDGET_S = None
    assert gateway_client.control_query_budget() == gateway_client.CONTROL_QUERY_FLOOR_S


def test_a_client_observed_round_trip_widens_the_budget():
    """The client's own round trip is the wait the owner cannot see.

    The owner measures its own handling time, so it never sees the time a client spends
    queued behind a busy pipe — and that queue wait is what ends a worker that never began
    its work. The client's own measurement does see it, so the budget has to grow with it.
    """
    assert gateway_client.control_query_budget() == gateway_client.CONTROL_QUERY_FLOOR_S
    gateway_client.record_control_exchange(20.0)
    assert gateway_client.control_query_budget() > gateway_client.CONTROL_QUERY_FLOOR_S
    assert gateway_client.control_query_budget() >= 20.0


def test_the_larger_of_the_two_sources_wins():
    """The owner publishing a short budget does not shrink what this client measured."""
    gateway_client.record_control_exchange(30.0)
    gateway_client.publish_control_budget(6.0)
    assert gateway_client.control_query_budget() >= 30.0
    # And the owner's larger number wins when it is the larger one.
    gateway_client._CLIENT_OBSERVED_S.clear()
    gateway_client.publish_control_budget(90.0)
    assert gateway_client.control_query_budget() == 90.0


def test_a_timeout_is_retried_across_the_pipe_instances(monkeypatch):
    """A busy pipe is retried, not fatal: the next attempt finds a free instance."""
    attempts = []

    def fake_once(home, request, timeout):
        attempts.append(timeout)
        if len(attempts) < 3:
            raise TimeoutError("runtime control deadline exceeded")
        return b'{"ok": true}\n'

    monkeypatch.setattr(gateway_client, "_private_control_query_once", fake_once)
    answer = gateway_client.private_control_query(
        __import__("pathlib").Path("/tmp/home"), b"request\n", timeout=20.0)
    assert answer == b'{"ok": true}\n'
    assert len(attempts) == 3, "the mint must be retried, not abandoned on the first timeout"


def test_a_refusal_is_not_retried(monkeypatch):
    """An owner's own verdict (a refusal, an absent pipe) is final: retrying cannot change it."""
    attempts = []

    def fake_once(home, request, timeout):
        attempts.append(timeout)
        raise PermissionError("runtime control pipe inaccessible")

    monkeypatch.setattr(gateway_client, "_private_control_query_once", fake_once)
    with pytest.raises(PermissionError):
        gateway_client.private_control_query(
            __import__("pathlib").Path("/tmp/home"), b"request\n", timeout=20.0)
    assert len(attempts) == 1, "a refusal must not be retried"


def test_exhausted_attempts_raise_the_timeout(monkeypatch):
    """When every instance stayed busy, the caller still hears a timeout."""
    attempts = []

    def fake_once(home, request, timeout):
        attempts.append(timeout)
        raise TimeoutError("runtime control deadline exceeded")

    monkeypatch.setattr(gateway_client, "_private_control_query_once", fake_once)
    monkeypatch.setattr(gateway_client.time, "sleep", lambda _seconds: None)
    with pytest.raises(TimeoutError):
        gateway_client.private_control_query(
            __import__("pathlib").Path("/tmp/home"), b"request\n", timeout=1.0)
    assert len(attempts) == gateway_client.CONTROL_QUERY_ATTEMPTS


def test_the_attempts_share_one_deadline(monkeypatch):
    """The retries do not multiply the budget without limit: the whole mint is bounded.

    A mint that retried over the budget without limit would outlast the worker's own boot
    envelope, so the attempts share one deadline and each one gets at most one exchange's worth.
    """
    seen = []

    def fake_once(home, request, timeout):
        seen.append(timeout)
        raise TimeoutError("runtime control deadline exceeded")

    monkeypatch.setattr(gateway_client, "_private_control_query_once", fake_once)
    monkeypatch.setattr(gateway_client.time, "sleep", lambda _seconds: None)
    with pytest.raises(TimeoutError):
        gateway_client.private_control_query(
            __import__("pathlib").Path("/tmp/home"), b"request\n", timeout=40.0)
    assert seen, "the mint must have been attempted"
    assert seen[0] == pytest.approx(40.0), "the first attempt gets the full budget"
    assert all(window <= 40.0 + 0.5 for window in seen), \
        "no attempt may exceed one exchange's worth"
    assert len(seen) == gateway_client.CONTROL_QUERY_ATTEMPTS, \
        "the retries must actually happen"


def test_the_mint_is_bounded_by_the_total_ceiling(monkeypatch):
    """A published budget at the ceiling still cannot make the mint outlive the envelope.

    The attempts block for the window they are granted, the way a real pipe call does, so this
    measures the wall clock a stalled owner would actually cost the worker.
    """
    spent = []

    def fake_once(home, request, timeout):
        spent.append(timeout)
        time.sleep(timeout)  # a blocking attempt burns its whole window
        raise TimeoutError("runtime control deadline exceeded")

    monkeypatch.setattr(gateway_client, "_private_control_query_once", fake_once)
    monkeypatch.setattr(gateway_client.time, "sleep", lambda _seconds: None)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        gateway_client.private_control_query(
            __import__("pathlib").Path("/tmp/home"), b"request\n",
            timeout=gateway_client.CONTROL_QUERY_CEILING_S)
    elapsed = time.monotonic() - started
    assert elapsed <= gateway_client.CONTROL_QUERY_TOTAL_CEILING_S + 1.0, \
        "the mint must not outlive its total ceiling"
    assert spent, "the mint must have been attempted"


def test_the_mint_is_budgeted_from_the_endpoint(monkeypatch):
    """The mint waits what the endpoint's owner published, not a fixed few seconds."""
    seen = {}

    def fake_once(home, request, timeout):
        seen["timeout"] = timeout
        return json.dumps({
            "ok": True, "protocol": 1, "id": 1,
            "result": {"ticket": "t", "profile_id": "default", "instance_id": "owner",
                       "runtime_protocol": 1},
        }).encode() + b"\n"

    monkeypatch.setattr(gateway_client, "_private_control_query_once", fake_once)
    ticket = gateway_client._session_ticket(
        __import__("pathlib").Path("/tmp/home"), _endpoint(control_budget_s=45.0))
    assert ticket == "t"
    assert seen["timeout"] == pytest.approx(45.0, abs=0.5), \
        "the mint must use the owner's published budget"


def test_the_mint_falls_back_to_the_floor_without_a_published_budget(monkeypatch):
    """An owner that publishes no budget (an older one) still gets the floor."""
    seen = {}

    def fake_once(home, request, timeout):
        seen["timeout"] = timeout
        return json.dumps({
            "ok": True, "protocol": 1, "id": 1,
            "result": {"ticket": "t", "profile_id": "default", "instance_id": "owner",
                       "runtime_protocol": 1},
        }).encode() + b"\n"

    monkeypatch.setattr(gateway_client, "_private_control_query_once", fake_once)
    gateway_client._session_ticket(
        __import__("pathlib").Path("/tmp/home"), _endpoint(control_budget_s=None))
    assert seen["timeout"] == pytest.approx(gateway_client.CONTROL_QUERY_FLOOR_S, abs=0.5)
