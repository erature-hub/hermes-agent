"""The gateway's "dispatcher stuck" warning must read as saturation at the cap.

The embedded gateway dispatcher is the surface that produced the reported
"kanban dispatcher stuck … Check profile health (venv, PATH, credentials)"
storm on a fleet that was merely BUSY. A fleet at its concurrency cap is
healthy: the warning must name the cap instead of sending the operator after
profile triage that cannot change the outcome (#111910).
"""

from __future__ import annotations

import logging

from gateway.kanban_watchers_dispatcher import log_dispatcher_stuck
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_dispatch_suppression as kds


def _capacity_result() -> kb.DispatchResult:
    res = kb.DispatchResult()
    res.capacity_holds["max_in_progress"] = kds.CapacityHold(
        "max_in_progress", 3, 3,
    )
    return res


def test_gateway_warning_states_the_capacity_constraint(caplog):
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        log_dispatcher_stuck(27, [("default", _capacity_result())])

    text = caplog.text
    assert "kanban dispatcher stuck" in text
    assert "max_in_progress=3" in text
    assert "running=3" in text
    assert "27 consecutive ticks" in text


def test_gateway_warning_drops_profile_health_at_the_cap(caplog):
    """The broken-fleet checklist must NOT appear for a saturated fleet."""
    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        log_dispatcher_stuck(27, [("default", _capacity_result())])

    text = caplog.text
    assert "profile health" not in text
    assert "venv" not in text
    assert "PATH" not in text
    assert "credentials" not in text


def test_gateway_warning_names_the_per_profile_cap(caplog):
    res = kb.DispatchResult()
    res.capacity_holds["max_in_progress_per_profile:cypher"] = kds.CapacityHold(
        "max_in_progress_per_profile", 1, 1, "cypher",
    )

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        log_dispatcher_stuck(27, [("default", res)])

    text = caplog.text
    assert "max_in_progress_per_profile=1" in text
    assert "assignee=cypher" in text
    assert "profile health" not in text


def test_gateway_warning_keeps_the_profile_checklist_for_a_real_hold(caplog):
    """A guard-held tick is not saturation: the operator guidance stays."""
    res = kb.DispatchResult()
    res.respawn_guarded.append(("t_1", "active_pr"))

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        log_dispatcher_stuck(27, [("default", res)])

    text = caplog.text
    assert "profile health" in text
    assert "Last tick held back: active_pr=1" in text
    assert "max_in_progress" not in text
