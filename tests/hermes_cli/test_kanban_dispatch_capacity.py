"""A saturated fleet must be reported as saturation, not as a broken fleet.

A tick refused by the concurrency cap and a tick whose worker cannot start
look identical from the outside: the ready queue is non-empty and nothing
spawned. The first is normal — the fleet is busy — and is fixed by a config
value; the second is a broken profile. The "dispatcher stuck" warning used to
print the profile-health checklist (venv, PATH, credentials) for BOTH, so an
operator chased re-provisioning and credential rotation that could never
change the outcome (#111910).

These tests pin the contract: the cap that held the tick back is named in the
result and in the warning, and the existing guard reasons still render.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_dispatch_suppression as kds


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_spawn_factory(spawns: list):
    def fake_spawn(task, workspace, board=None):
        spawns.append(task.id)
        return 42
    return fake_spawn


def _claim_running(conn, title: str, assignee: str) -> str:
    """A task that occupies a worker slot without ever having spawned one."""
    tid = kb.create_task(conn, title=title, assignee=assignee)
    assert kb.claim_task(conn, tid) is not None
    return tid


# ---------------------------------------------------------------------------
# 1. The global cap is carried into the tick result (acceptance criterion 1)
# ---------------------------------------------------------------------------


def test_global_cap_is_recorded_on_the_tick_result(kanban_home, all_assignees_spawnable):
    """``max_in_progress`` reached → the result names the cap and the count."""
    spawns: list = []
    with kbc.connect() as conn:
        for n in range(3):
            _claim_running(conn, f"busy-{n}", "alice")
        kb.create_task(conn, title="wants-to-run", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=3,
        )

    assert not spawns
    assert not res.spawned
    hold = res.capacity_holds["max_in_progress"]
    assert hold.scope == "max_in_progress"
    assert hold.limit == 3
    assert hold.running == 3
    assert hold.assignee is None


def test_global_cap_reason_names_the_cap_and_the_running_count(
    kanban_home, all_assignees_spawnable,
):
    """The rendered reason is ``capacity: max_in_progress=3 running=3``."""
    spawns: list = []
    with kbc.connect() as conn:
        for n in range(3):
            _claim_running(conn, f"busy-{n}", "alice")
        kb.create_task(conn, title="wants-to-run", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=3,
        )

    assert kds.capacity_reason([res]) == "capacity: max_in_progress=3 running=3"
    assert "capacity: max_in_progress=3 running=3" in kds.describe_suppression([res])


# ---------------------------------------------------------------------------
# 2. The per-profile cap names the assignee and its in-flight workers
#    (acceptance criterion 3)
# ---------------------------------------------------------------------------


def test_per_profile_cap_names_the_assignee_and_its_in_flight_count(
    kanban_home, all_assignees_spawnable,
):
    """``max_in_progress_per_profile`` held the card back → name the profile."""
    spawns: list = []
    with kbc.connect() as conn:
        _claim_running(conn, "busy", "alice")
        kb.create_task(conn, title="wants-to-run", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=5, max_in_progress_per_profile=1,
        )

    assert not spawns
    hold = res.capacity_holds["max_in_progress_per_profile:alice"]
    assert hold.scope == "max_in_progress_per_profile"
    assert hold.limit == 1
    assert hold.running == 1
    assert hold.assignee == "alice"
    reason = kds.capacity_reason([res])
    assert reason == "capacity: max_in_progress_per_profile=1 running=1 assignee=alice"


def test_per_profile_cap_holds_only_the_capped_assignee(
    kanban_home, all_assignees_spawnable,
):
    """A second profile with headroom still spawns — the cap is per profile."""
    spawns: list = []
    with kbc.connect() as conn:
        _claim_running(conn, "busy", "alice")
        kb.create_task(conn, title="alice-card", assignee="alice")
        kb.create_task(conn, title="bob-card", assignee="bob")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns),
            max_in_progress=5, max_in_progress_per_profile=1,
        )

    assert [t for t in spawns]
    assert res.spawned and res.spawned[0][1] == "bob"
    assert "max_in_progress_per_profile:alice" in res.capacity_holds


# ---------------------------------------------------------------------------
# 3. The warning states the constraint and drops the profile-health checklist
#    (acceptance criterion 2)
# ---------------------------------------------------------------------------


def test_capacity_warning_names_the_cap_and_drops_profile_health(
    kanban_home, all_assignees_spawnable,
):
    """A fleet at its cap is healthy: no venv/PATH/credential triage."""
    spawns: list = []
    with kbc.connect() as conn:
        for n in range(3):
            _claim_running(conn, f"busy-{n}", "alice")
        kb.create_task(conn, title="wants-to-run", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=3,
        )

    message = kds.stuck_message(27, [res])
    assert "max_in_progress=3" in message
    assert "running=3" in message
    assert "profile health" not in message
    assert "venv" not in message
    assert "credentials" not in message


def test_profile_health_guidance_stays_for_a_non_capacity_hold():
    """Without a capacity hold the operator guidance is unchanged."""
    res = kb.DispatchResult()
    res.respawn_guarded.append(("t_1", "active_pr"))

    message = kds.stuck_message(6, [res])
    assert "profile health" in message
    assert "Last tick held back: active_pr=1" in message
    assert "max_in_progress" not in message


def test_warning_without_any_reason_keeps_the_operator_guidance():
    """No guard and no cap: the bare zero-spawn report still guides."""
    message = kds.stuck_message(6, [kb.DispatchResult()])
    assert "profile health" in message
    assert "Last tick held back" not in message


# ---------------------------------------------------------------------------
# 4. The existing reasons render exactly as before (acceptance criterion 4)
# ---------------------------------------------------------------------------


def test_guard_reasons_still_render_unchanged():
    """active_pr / recent_success / rate_limited / skipped_locked / memory."""
    res = kb.DispatchResult()
    res.respawn_guarded.append(("t_1", "active_pr"))
    res.respawn_guarded.append(("t_2", "active_pr"))
    res.respawn_guarded.append(("t_3", "recent_success"))
    res.rate_limited.append("t_4")
    res.skipped_locked = True

    assert kds.describe_suppression([res]) == (
        "active_pr=2, rate_limited=1, recent_success=1, skipped_locked=1"
    )


def test_memory_pressure_still_renders():
    res = kb.DispatchResult()
    res.memory_pressure = "critical"

    assert kds.describe_suppression([res]) == "memory_pressure=critical"


def test_capacity_reason_is_additive_not_a_replacement():
    """Guard reasons and the capacity reason coexist in one rendering."""
    res = kb.DispatchResult()
    res.respawn_guarded.append(("t_1", "active_pr"))
    res.memory_pressure = "elevated"
    res.capacity_holds["max_in_progress"] = kds.CapacityHold(
        "max_in_progress", 3, 3,
    )

    rendered = kds.describe_suppression([res])
    assert "active_pr=1" in rendered
    assert "memory_pressure=elevated" in rendered
    assert "capacity: max_in_progress=3 running=3" in rendered


def test_capacity_reason_is_absent_without_a_hold():
    assert kds.capacity_reason([kb.DispatchResult()]) is None
    assert kds.capacity_reason([]) is None


# ---------------------------------------------------------------------------
# 5. The rendering survives a one-shot iterable
# ---------------------------------------------------------------------------


def test_describe_suppression_reads_a_generator_once():
    """The gateway hands over a one-shot iterable — the reason must survive."""
    res = kb.DispatchResult()
    res.capacity_holds["max_in_progress"] = kds.CapacityHold(
        "max_in_progress", 3, 3,
    )

    assert "capacity: max_in_progress=3 running=3" in kds.describe_suppression(
        r for r in (res,)
    )


# ---------------------------------------------------------------------------
# 6. The reported scenario, end to end
# ---------------------------------------------------------------------------


def test_reported_scenario_reads_as_capacity_not_broken_fleet(
    kanban_home, all_assignees_spawnable,
):
    """The card's measurement: running=3, max_in_progress=3, a full ready queue."""
    spawns: list = []
    with kbc.connect() as conn:
        for n in range(3):
            _claim_running(conn, f"busy-{n}", "alice")
        for n in range(24):
            kb.create_task(conn, title=f"ready-{n}", assignee="alice")
        res = kbd.dispatch_once(
            conn, spawn_fn=_fake_spawn_factory(spawns), max_in_progress=3,
        )

    message = kds.stuck_message(27, [res])
    assert "0 workers spawned" in message
    assert "capacity: max_in_progress=3 running=3" in message
    assert "profile health" not in message
