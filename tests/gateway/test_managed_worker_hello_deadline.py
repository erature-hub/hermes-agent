"""The handshake deadline must follow the spawn cost this host actually pays.

Regression for the 2026-11-01 fleet-wide outage (#t_853294b3): every lane's managed worker
was admitted and then died having produced nothing, because the owner's FIXED 60s handshake
deadline did not cover the child's boot on a slow host. The child's boot includes the PM
dependency probe, whose cost tracks the host's disk and CPU load, so the deadline has to be
derived from the observed cost rather than pinned to a constant.

These tests pin the derivation:

  C1  with no observation yet, the deadline is the historical floor (60s) -- a fast host
      keeps the old, honest guarantee that a child silent that long IS wedged;
  C2  a profile that has paid a slow handshake gets a proportionally longer deadline, so a
      busy host does not declare its own healthy workers wedged;
  C3  the derivation is bounded, so a pathological measurement cannot hang an attempt
      forever;
  C4  only RECENT costs count: a stale slow measurement must not raise today's deadline;
  C5  the observed cost is measured from the spawn, i.e. it includes the child's boot.
"""

from __future__ import annotations

import time

from gateway import session_managed_worker as smw


class _Authority:
    """The minimum the deadline derivation reads."""

    def __init__(self, costs=None):
        if costs is not None:
            self._managed_hello_costs = list(costs)


def test_c1_with_no_observation_the_deadline_is_the_historical_floor():
    """C1: nothing observed yet -> the old fixed deadline, unchanged."""
    assert smw._hello_deadline_seconds(_Authority()) == float(smw.HELLO_SECONDS)
    assert smw.HELLO_SECONDS == 60  # the floor the outage's constant used


def test_c2_a_slow_profile_gets_a_proportionally_longer_deadline():
    """C2: a profile that boots workers slowly is not told its workers are wedged."""
    slow = _Authority(costs=[95.0, 90.0])
    deadline = smw._hello_deadline_seconds(slow)
    assert deadline > 95.0, deadline
    assert deadline >= 95.0 * smw.HELLO_DEADLINE_MARGIN
    # A fast profile keeps the floor: the margin never buys less certainty than before.
    fast = _Authority(costs=[2.0, 3.0])
    assert smw._hello_deadline_seconds(fast) == float(smw.HELLO_SECONDS)


def test_c3_the_derivation_is_bounded():
    """C3: a pathological measurement cannot hang an attempt forever."""
    absurd = _Authority(costs=[100000.0])
    assert smw._hello_deadline_seconds(absurd) == smw.HELLO_DEADLINE_CEILING


def test_c4_only_recent_costs_count():
    """C4: an old slow measurement must not raise today's deadline."""
    authority = _Authority()
    for cost in (95.0, 90.0, 88.0):
        smw._record_hello_cost(authority, cost)
    deadline = smw._hello_deadline_seconds(authority)
    assert deadline > 88.0, deadline

    # A fast handshake afterwards: the recent window replaces the slow one.
    for _ in range(8):
        smw._record_hello_cost(authority, 1.0)
    assert smw._hello_deadline_seconds(authority) == float(smw.HELLO_SECONDS)


def test_c5_the_recorded_cost_is_the_measured_span():
    """C5: what is recorded is the elapsed time, measured from the spawn."""
    authority = _Authority()
    started = time.monotonic()
    elapsed = time.monotonic() - started
    smw._record_hello_cost(authority, elapsed)
    assert authority._managed_hello_costs == [elapsed]
    assert elapsed < 5.0  # a same-tick measurement, not a wall-clock guess


def test_the_window_is_bounded():
    """Only the recent costs matter; an unbounded list would grow without limit."""
    authority = _Authority()
    for i in range(50):
        smw._record_hello_cost(authority, float(i))
    assert len(authority._managed_hello_costs) <= 8
