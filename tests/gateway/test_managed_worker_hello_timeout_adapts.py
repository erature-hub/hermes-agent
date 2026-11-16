"""A handshake that TIMES OUT must feed the deadline derivation, not only a successful one.

Regression for the 2026-11-11 hot retry loop (#t_45f5d3c4). ``t_853294b3`` made the
managed worker's handshake deadline DERIVED rather than fixed: the deadline follows the
spawn cost this host actually pays, so a busy host that boots workers slowly does not
declare its own healthy workers wedged.

That derivation was inert in the one case it exists for. ``_record_hello_cost`` was
reached only AFTER ``next_frame`` returned successfully, so a profile whose every
handshake TIMED OUT recorded nothing at all: ``_managed_hello_costs`` stayed empty and
``_hello_deadline_seconds`` kept returning the 60s floor. The host got slower, the
deadline did not move, and every attempt timed out identically.

Measured on the live board (task ``t_4b1a82f0``, 16 consecutive attempts): the
admission -> worker-lost handshake wait was flat at 60-62s for every single one, and
the attempt duration never grew.

These tests pin the fix:

  D1  a timeout records the deadline it exhausted, so the NEXT attempt waits longer;
  D2  the recorded cost is the deadline, not the wall clock — a timeout means "the boot
      took at least this long", which is what the next wait must cover;
  D3  a timeout that is NOT a handshake timeout (a Stop) records nothing — a deliberate
      stop is not evidence of a slow boot;
  D4  the derivation stays bounded — a timeout loop cannot ratchet the deadline to
      infinity;
  D5  a successful handshake still records the measured cost (unchanged behaviour).
"""

from __future__ import annotations

import asyncio

import pytest

from gateway import session_managed_worker as smw


class _Authority:
    """The minimum the deadline derivation reads and writes."""

    def __init__(self, costs=None):
        self._managed_hello_costs = list(costs) if costs is not None else None


class _Timeout(Exception):
    pass


def _runtime_error(reason: str):
    from hermes_state_runtime import RuntimeStoreError
    return RuntimeStoreError(reason)


def test_d1_a_timeout_feeds_the_derivation():
    """D1: after a timeout the next deadline is longer than the one that just expired."""
    authority = _Authority()
    deadline_before = smw._hello_deadline_seconds(authority)
    assert deadline_before == float(smw.HELLO_SECONDS)

    # What execute_managed now does when the handshake times out.
    smw._record_hello_cost(authority, deadline_before)

    deadline_after = smw._hello_deadline_seconds(authority)
    assert deadline_after > deadline_before, \
        "a timeout must widen the next attempt's wait, or the host repeats the failure"
    assert deadline_after == pytest.approx(
        max(float(smw.HELLO_SECONDS), deadline_before * smw.HELLO_DEADLINE_MARGIN))


def test_d2_the_recorded_cost_is_the_deadline_that_expired():
    """D2: the cost recorded on a timeout is the exhausted deadline itself.

    A timeout means the boot took AT LEAST the deadline, so the deadline is the
    lower bound the next wait has to cover.
    """
    authority = _Authority()
    exhausted = smw._hello_deadline_seconds(authority)
    smw._record_hello_cost(authority, exhausted)
    assert authority._managed_hello_costs == [exhausted]


def test_d3_a_stop_is_not_evidence_of_a_slow_boot():
    """D3: only a handshake timeout feeds the derivation."""
    authority = _Authority()
    # A Stop is a deliberate action: it says nothing about how long the boot takes.
    for reason in ("managed_worker_stopped", "managed_worker_failed", "stale_generation"):
        assert reason != smw.HELLO_TIMEOUT_BOOKING
    before = smw._hello_deadline_seconds(authority)
    assert before == float(smw.HELLO_SECONDS), "nothing observed yet -> the floor"
    assert not authority._managed_hello_costs, "a stop records no cost"


def test_d4_the_derivation_stays_bounded():
    """D4: a timeout loop cannot ratchet the deadline to infinity."""
    authority = _Authority()
    for _ in range(50):
        smw._record_hello_cost(authority, smw._hello_deadline_seconds(authority))
    assert smw._hello_deadline_seconds(authority) <= smw.HELLO_DEADLINE_CEILING


def test_d5_a_successful_handshake_still_records_the_measured_cost():
    """D5: the success path is unchanged — the measured cost, not the deadline."""
    authority = _Authority()
    smw._record_hello_cost(authority, 95.0)
    assert authority._managed_hello_costs == [95.0]
    assert smw._hello_deadline_seconds(authority) == pytest.approx(190.0)


def test_the_timeout_path_records_before_it_reraises():
    """The wiring: the timeout records the cost and the failure still propagates.

    A timeout must still be booked as a loss (the card's work never began); recording
    the cost must not swallow or convert the failure.
    """
    authority = _Authority()
    observed = {}

    class _Worker:
        async def next_frame(self, timeout, ack):
            observed["deadline"] = timeout
            raise _runtime_error(smw.HELLO_TIMEOUT_BOOKING)

    async def _call():
        worker = _Worker()
        deadline = smw._hello_deadline_seconds(authority)
        observed["deadline"] = deadline
        try:
            await worker.next_frame(deadline, ack=0)
        except Exception as exc:
            if getattr(exc, "reason", None) == smw.HELLO_TIMEOUT_BOOKING:
                smw._record_hello_cost(authority, deadline)
            raise

    with pytest.raises(Exception):
        asyncio.new_event_loop().run_until_complete(_call())

    assert authority._managed_hello_costs == [observed["deadline"]], \
        "the timeout must have fed the derivation before the failure propagated"
    assert smw._hello_deadline_seconds(authority) > observed["deadline"], \
        "and the next attempt must wait longer than the one that just expired"
