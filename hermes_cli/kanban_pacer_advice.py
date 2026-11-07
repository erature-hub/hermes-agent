"""Apply the JEV pacer's advice to the next dispatch tick.

The pacer advises; this module is where the advice becomes a constraint the
dispatcher honors. Two rules govern everything here:

**The pacer may only NARROW.** A pacer that says "drain these three cards,
in this order" replaces the dispatcher's own ordering for those cards. It
can never widen the tick's spawn budget beyond what the dispatcher already
allowed, and it can never make the dispatcher spawn a card the dispatcher
was not going to consider at all. A tighter budget wins; a looser one is
ignored.

**A missing or expired advisory changes nothing.** The pacer is an
observer. If it is unreachable, slow, or silent, the dispatcher behaves
exactly as it did before this module existed.

The advice is carried between ticks in a small host-side file so that the
pacer's ledger (which lives inside the aisuite container) and the dispatcher's
state (which lives on the host) never have to share a filesystem. The file
records only the pacer's own verdict plus the epoch second it expires; it is
the receipt of a conversation, not a second source of truth.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

_log = logging.getLogger(__name__)

#: Where the pending advice is kept. Overridable so tests (and installs
#: with a different home) do not share state.
ADVICE_DIR_ENV = "HERMES_JEV_PACER_STATE_DIR"

#: Filename of the pending-advice file, per board.
ADVICE_FILENAME = "pacing_advice.json"

#: How long a piece of advice stays actionable. The pacer's own hold
#: intervals are 5-1800 s; a stale advisory must never bind a tick that
#: happens hours later, so the ceiling is well under the longest hold.
MAX_ADVICE_TTL_S = 1800.0

#: A claim budget is a ceiling, never a floor. The dispatcher's own budget
#: always wins; this is the smallest value both agree on.
def _min_budget(*budgets: Optional[int]) -> Optional[int]:
    """The tightest of the given budgets, treating ``None`` as unbounded."""
    concrete = [b for b in budgets if b is not None]
    if not concrete:
        return None
    return min(concrete)


def _state_dir() -> Path:
    override = os.environ.get(ADVICE_DIR_ENV, "").strip()
    if override:
        return Path(override)
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()) / "state" / "jev-pacer"
    except Exception:  # pragma: no cover - defensive
        return Path(tempfile.gettempdir()) / "hermes-jev-pacer"


def _advice_path(board: Optional[str]) -> Path:
    slug = (board or "default").strip() or "default"
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in slug)
    return _state_dir() / f"{safe}.{ADVICE_FILENAME}"


def _atomic_write(path: Path, payload: str) -> None:
    """Write the advice file atomically.

    A half-written file would be read as garbage advice; a rename is
    atomic on every filesystem this runs on.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=f".{path.name}.", delete=False
    )
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except Exception as exc:  # pragma: no cover - defensive
        _log.debug("jev pacer advice write failed: %s", exc)
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def record_advice(
    advisory: Any,
    *,
    board: Optional[str] = None,
    now: Optional[float] = None,
    ttl_s: float = MAX_ADVICE_TTL_S,
) -> None:
    """Record the pacer's advice so the NEXT tick can act on it.

    Only the fields the dispatcher acts on are persisted: the drain order,
    the spawn ceiling, and enough of the receipt for the alarm bus to enrich
    its class (f) event. Nothing here raises; advice that cannot be stored
    is simply advice that will not be applied.
    """
    decision = getattr(advisory, "decision", None)
    if decision is None:
        return
    moment = time.time() if now is None else float(now)
    receipt = getattr(advisory, "receipt", None)
    payload: dict[str, Any] = {
        "action": str(getattr(decision, "action", "none")),
        "stalled": bool(getattr(decision, "stalled", False)),
        "max_spawns_per_tick": int(getattr(decision, "max_spawns_per_tick", 0) or 0),
        "drain_order": list(getattr(decision, "drain_order", ()) or ()),
        "deferred_order": list(getattr(decision, "deferred_order", ()) or ()),
        "hold_until": getattr(decision, "hold_until", None),
        "rationale": str(getattr(decision, "rationale", "") or ""),
        "evidence": str(getattr(decision, "evidence", "") or ""),
        "recorded_at": moment,
        "expires_at": moment + max(0.0, float(ttl_s)),
    }
    if receipt is not None:
        payload["receipt"] = receipt.to_payload() if hasattr(receipt, "to_payload") else dict(receipt)
        payload["model_opinion"] = getattr(advisory, "model_opinion", None)
    _atomic_write(_advice_path(board), json.dumps(payload))


def read_advice(
    *,
    board: Optional[str] = None,
    now: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Read the pending advice, or ``None`` when there is none to apply.

    Expired advice reads as ``None``: the pacer's verdict was about a moment
    that has passed, and a stale hold must not bind a later tick.
    """
    path = _advice_path(board)
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    moment = time.time() if now is None else float(now)
    expires_at = payload.get("expires_at")
    try:
        if expires_at is not None and moment > float(expires_at):
            return None
    except (TypeError, ValueError):
        return None
    return payload


def clear_advice(*, board: Optional[str] = None) -> None:
    """Drop the pending advice (used once it has been acted on)."""
    try:
        _advice_path(board).unlink()
    except OSError:
        pass


def drain_order_for(
    advisory: Optional[dict[str, Any]],
    candidates: list[str],
) -> list[str]:
    """Order ``candidates`` the way the pacer asked.

    Cards the pacer named come first, in the pacer's order. Cards the pacer
    did not name keep their existing relative order and follow. A card the
    pacer named but that is no longer a candidate is dropped: the pacer
    ranked a queue that has since moved.
    """
    if not advisory:
        return list(candidates)
    named = [str(t) for t in (advisory.get("drain_order") or ())]
    if not named:
        return list(candidates)
    available = set(candidates)
    ordered = [t for t in named if t in available]
    seen = set(ordered)
    ordered.extend(t for t in candidates if t not in seen)
    return ordered


def claim_budget_for(
    advisory: Optional[dict[str, Any]],
    own_budget: Optional[int],
) -> Optional[int]:
    """The spawn ceiling for the next tick.

    The pacer's ceiling and the dispatcher's own ceiling are combined by
    taking the tighter of the two. The pacer can hold the dispatcher back;
    it can never let the dispatcher exceed its own limits.
    """
    if not advisory:
        return own_budget
    if str(advisory.get("action") or "none") == "none":
        # "No intervention required" -- the pacer is explicitly not asking
        # for restraint, so the dispatcher's own budget stands untouched.
        return own_budget
    pacer_limit = advisory.get("max_spawns_per_tick")
    try:
        pacer_budget = int(pacer_limit) if pacer_limit is not None else None
    except (TypeError, ValueError):
        pacer_budget = None
    return _min_budget(own_budget, pacer_budget)


def receipt_enrichment(
    advisory: Optional[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """The class (f) receipt, as enrichment for the bus's existing event.

    Returns the receipt exactly as the pacer issued it. The pacer's
    ``should_notify`` is deliberately NOT consulted here: whether to page is
    the alarm bus's decision, made against its own host-side ledger. The
    pacer contributes evidence to that event; it never raises a second one.

    A receipt with an EMPTY ``dedupe_key`` is the pacer's way of saying
    "no stall episode" -- it reports a draining queue rather than naming an
    episode -- so it is not enrichment for anything and is dropped here.
    """
    if not advisory:
        return None
    receipt = advisory.get("receipt")
    if not isinstance(receipt, dict) or not receipt:
        return None
    if not str(receipt.get("dedupe_key") or ""):
        return None
    return receipt


__all__ = [
    "ADVICE_DIR_ENV",
    "ADVICE_FILENAME",
    "MAX_ADVICE_TTL_S",
    "claim_budget_for",
    "clear_advice",
    "drain_order_for",
    "read_advice",
    "receipt_enrichment",
    "record_advice",
]
