"""Why a dispatch tick held ready work back — the reason the warnings quote.

The dispatcher's health warnings fire when the ready queue is non-empty but a
tick spawned nobody. Two very different causes look identical from the
outside: a BROKEN fleet (the worker cannot start at all) and a SATURATED one
(the concurrency cap had no slot left). Only the second is normal, and only
the second is fixed by a config value — so the cap has to be visible in the
warning, not just in ``hermes kanban tail`` (#111910).

Owns the capacity hold a tick records, the one-line rendering of the reasons
and the text of the "dispatcher stuck" warning. Both surfaces that raise that
warning — the standalone daemon and the embedded gateway dispatcher — build it
here so the two can never drift apart again.

Split out of ``hermes_cli.kanban_db_dispatch``; the tick result is reached by
duck type so this module stays import-cycle free.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from typing import Optional
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from hermes_cli.kanban_db_dispatch import DispatchResult


# Operator guidance for a tick held back by something the operator cannot see
# in the board state: a worker that cannot start at all.
PROFILE_HEALTH_GUIDANCE = (
    "Check profile health (venv, PATH, credentials) and `hermes kanban list "
    "--status ready`."
)

# Operator guidance for a tick held back by the concurrency cap: the fleet is
# saturated, and the fix is a config value — never a re-provisioned profile.
CAPACITY_GUIDANCE = (
    "The dispatcher is at its concurrency cap — the fleet is busy, not broken: "
    "raise the cap in config.yaml (kanban.max_in_progress / "
    "kanban.max_in_progress_per_profile) or wait for a running worker to finish."
)


@dataclass(frozen=True)
class CapacityHold:
    """A concurrency cap that refused a spawn this tick.

    ``scope`` names the cap (``max_in_progress`` host-wide, ``max_spawn``
    per-board, ``max_in_progress_per_profile`` per profile), ``limit`` its
    configured value, ``running`` the in-flight workers that filled it, and
    ``assignee`` the profile at its per-profile ceiling (``None`` for the
    host-wide and per-board caps).
    """

    scope: str
    limit: int
    running: int
    assignee: Optional[str] = None

    def render(self) -> str:
        """``max_in_progress=3 running=3`` — the cap and what filled it."""
        text = f"{self.scope}={self.limit} running={self.running}"
        if self.assignee:
            text += f" assignee={self.assignee}"
        return text


def _capacity_holds(results: Iterable[Optional[DispatchResult]]) -> list[CapacityHold]:
    """Every capacity hold the given tick results recorded, in tick order."""
    holds: list[CapacityHold] = []
    for res in results:
        if res is None:
            continue
        holds.extend(res.capacity_holds.values())
    return holds


def capacity_reason(results: Iterable[Optional[DispatchResult]]) -> Optional[str]:
    """The capacity constraint that held the tick(s) back, or ``None``.

    ``capacity: max_in_progress=3 running=3`` — the cap that was full and the
    in-flight workers that filled it. ``None`` means no cap refused a spawn, so
    the tick had budget and something else stopped it.
    """
    holds = _capacity_holds(results)
    if not holds:
        return None
    return "capacity: " + "; ".join(hold.render() for hold in holds)


def describe_suppression(results: Iterable[Optional[DispatchResult]]) -> str:
    """One line naming why the tick(s) held ready work back, or ``""``.

    ``active_pr=1, recent_success=2, rate_limited=1, skipped_locked=1,
    memory_pressure=critical`` — the respawn-guard reasons counted per task
    plus the tick-level holds. A tick refused by a concurrency cap adds
    ``capacity: max_in_progress=3 running=3`` (or the per-profile cap with the
    capped assignee): a saturated fleet is NOT a broken one, and the cap is the
    constraint the operator has to see. Feeds the "dispatcher stuck" warnings
    of the standalone daemon and the embedded gateway dispatcher, which
    otherwise report a bare zero-spawn count while ``hermes kanban tail`` is
    the only place the guard reason is written (#111910).
    """
    results = list(results)
    counts: dict[str, int] = {}
    pressure: Optional[str] = None
    for res in results:
        if res is None:
            continue
        for _task_id, reason in res.respawn_guarded:
            counts[reason] = counts.get(reason, 0) + 1
        if res.rate_limited:
            counts["rate_limited"] = counts.get("rate_limited", 0) + len(res.rate_limited)
        if res.skipped_locked:
            counts["skipped_locked"] = counts.get("skipped_locked", 0) + 1
        if res.memory_pressure:
            pressure = res.memory_pressure
    parts = [f"{k}={v}" for k, v in sorted(counts.items())]
    if pressure:
        parts.append(f"memory_pressure={pressure}")
    capacity = capacity_reason(results)
    if capacity:
        parts.append(capacity)
    return ", ".join(parts)


def stuck_message(
    bad_ticks: int,
    results: Iterable[Optional[DispatchResult]],
    *,
    profile_guidance: str = PROFILE_HEALTH_GUIDANCE,
) -> str:
    """The body of the "dispatcher stuck" warning for a run of zero-spawn ticks.

    A tick refused by a concurrency cap is SATURATION, not a broken fleet: the
    message then names the cap and drops the profile-health checklist, which
    sends the operator after venv/PATH/credential fixes that cannot change the
    outcome (#111910). ``profile_guidance`` is the text used when no cap
    refused the spawn — the caller owns its wording.
    """
    results = list(results)
    held = describe_suppression(results)
    held_clause = f" Last tick held back: {held}." if held else ""
    guidance = CAPACITY_GUIDANCE if capacity_reason(results) else profile_guidance
    return (
        f"ready queue non-empty for {bad_ticks} consecutive ticks but 0 workers "
        f"spawned.{held_clause} {guidance}"
    )
