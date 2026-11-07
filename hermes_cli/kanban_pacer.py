"""Hermes-side client for the JEV quota pacer (aisuite JEV-2 surface).

The pacer lives in the aisuite service and is reached over HTTP at
``POST /api/operations/micro-decisions/pacing``. It answers with a
``PacingResponse`` carrying three things:

``decision``
    The pacing decision for the NEXT tick: which cards to drain, in what
    order, and how many spawns the tick may make.
``receipt``
    The class (f) stall receipt. It is ENRICHMENT for the alarm bus's own
    class (f) event -- it is never a second alarm.
``advisory``
    The model tier's opinion. It is present only while the pacer's window is
    stalled (JEV-1 requirement 5); its absence is normal, not an error.

Three invariants this module exists to hold:

1. **The pacer advises; it never acts.** The pacer returns a drain order and
   a budget. It does not claim, unblock, or spawn. This module therefore
   returns advice to the caller and never mutates the board.

2. **A missing quota gate is ``UNKNOWN``, never ``ok``.** The Hermes-side
   fleet quota gate (``feat/kanban-fleet-quota-gate-t_074cc4fc``) is not
   merged, so ``DispatchResult`` carries no ``quota_state``. Reporting ``ok``
   would be a fabricated reading; ``UNKNOWN`` is the honest one, and the
   pacer's own policy weights ``UNKNOWN`` toward escalate rather than away.

3. **A 200 is not a decision.** The aisuite frontend serves a catch-all
   HTML page for unknown paths, so an unmounted route answers ``200
   text/html``. This client accepts a response only when the content type
   is JSON and the body carries a ``decision``; anything else is treated as
   "no advice", which is the same as the pacer being unreachable.

Nothing here raises. Every failure degrades to "no advice", because the
dispatcher must never be made slower or flakier by an advisory service.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

_log = logging.getLogger(__name__)

#: The JEV-2 pacing surface on the aisuite backend.
DEFAULT_PACER_URL = "http://127.0.0.1:8001/api/operations/micro-decisions/pacing"

#: Hard ceiling on the pacer round trip. The dispatcher calls this after the
#: dispatch lock is released, so a slow pacer must not hold a dispatcher
#: open. The pacer's own advisory budget is ~250 ms; 2.0 s is a generous
#: ceiling that still fails fast enough to be invisible on a 60 s tick.
DEFAULT_TIMEOUT_S = 2.0

#: Environment override for the endpoint (used by tests and by installs
#: whose backend is not on the default port).
PACER_URL_ENV = "HERMES_JEV_PACER_URL"

#: Environment override for the shared service token.
PACER_TOKEN_ENV = "HERMES_JEV_PACER_TOKEN"

#: Environment override for the request timeout, in seconds.
PACER_TIMEOUT_ENV = "HERMES_JEV_PACER_TIMEOUT_S"

#: The quota-gate states the aisuite pacer understands. These are the
#: ``FleetGateState`` wire values.
_QUOTA_STATES = ("ok", "warn", "tripped", "unknown")


def _resolve_timeout() -> float:
    raw = os.environ.get(PACER_TIMEOUT_ENV, "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_S
    return value if value > 0 else DEFAULT_TIMEOUT_S


@dataclass(frozen=True)
class DrainCandidate:
    """One claimable card offered to the pacer for ranking.

    Mirrors the aisuite ``DrainTarget``: the pacer ranks candidates
    deterministically (priority descending, then longest-waiting first, then
    task id) and returns the order. ``ready_since`` is optional: the pacer
    treats an unset value as "waiting longest", so omitting it is safe but
    ordering among equal-priority cards falls back to task id.
    """

    task_id: str
    priority: int = 0
    assignee: Optional[str] = None
    ready_since: Optional[float] = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task_id": self.task_id,
            "priority": int(self.priority),
        }
        if self.assignee:
            payload["assignee"] = self.assignee
        if self.ready_since is not None:
            payload["ready_since"] = float(self.ready_since)
        return payload


@dataclass(frozen=True)
class PacerDecision:
    """The pacer's advice for the next tick.

    ``drain_order`` is the ordered slice of cards the pacer wants spawned
    now; ``deferred_order`` is the ordered remainder. ``max_spawns_per_tick``
    is the ceiling on spawns this tick. ``action`` is one of the pacer's four
    closed actions: ``none``, ``held``, ``paced_drain``, ``escalate``.
    """

    action: str
    stalled: bool
    max_spawns_per_tick: int
    drain_order: tuple[str, ...] = ()
    deferred_order: tuple[str, ...] = ()
    hold_until: Optional[float] = None
    rationale: str = ""
    evidence: str = ""

    @property
    def is_intervention(self) -> bool:
        """True when the pacer wants the dispatcher to hold back.

        ``none`` means "no intervention required"; the other three all mean
        the pacer is asking for restraint.
        """
        return self.action != "none"

    def to_payload(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "stalled": self.stalled,
            "max_spawns_per_tick": self.max_spawns_per_tick,
            "drain_order": list(self.drain_order),
            "deferred_order": list(self.deferred_order),
            "hold_until": self.hold_until,
            "rationale": self.rationale,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ClassFReceipt:
    """The class (f) stall receipt, as enrichment for the alarm bus.

    The pacer's receipt is deliberately a *copy* of the pacer's own receipt
    with only its free-text ``evidence`` and ``operator_verify`` grown. The
    identity fields (``class_id``, ``class_name``, ``dedupe_key``) are
    carried verbatim so a consumer can tell that this receipt belongs to the
    SAME class (f) event the bus already raises -- the pacer does not get a
    vote on whether to page, and it does not get a second page.
    """

    class_id: str
    class_name: str
    dedupe_key: str
    should_notify: bool
    suppressed: bool
    headline: str = ""
    evidence: str = ""
    operator_verify: tuple[str, ...] = ()
    window_s: float = 0.0
    ready_count: int = 0
    barren_ticks: int = 0
    quota_deferred: int = 0
    quota_state: str = "unknown"
    observed_at: float = 0.0

    def to_payload(self) -> dict[str, Any]:
        return {
            "class_id": self.class_id,
            "class_name": self.class_name,
            "dedupe_key": self.dedupe_key,
            "should_notify": bool(self.should_notify),
            "suppressed": bool(self.suppressed),
            "headline": self.headline,
            "evidence": self.evidence,
            "operator_verify": list(self.operator_verify),
            "window_s": float(self.window_s),
            "ready_count": int(self.ready_count),
            "barren_ticks": int(self.barren_ticks),
            "quota_deferred": int(self.quota_deferred),
            "quota_state": self.quota_state,
            "observed_at": float(self.observed_at),
        }


@dataclass(frozen=True)
class PacerAdvisory:
    """The pacer's answer to one observed dispatch tick.

    ``receipt`` is ``None`` when the pacer reported no stall episode.
    ``model_opinion`` is the advisory model's action when the pacer chose to
    consult it (only while stalled) and ``None`` otherwise.
    """

    decision: PacerDecision
    receipt: Optional[ClassFReceipt] = None
    model_opinion: Optional[str] = None
    model_confidence: Optional[float] = None

    def to_payload(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_payload(),
            "receipt": self.receipt.to_payload() if self.receipt else None,
            "model_opinion": self.model_opinion,
            "model_confidence": self.model_confidence,
        }


def map_quota_state(result: Any) -> str:
    """Map a ``DispatchResult``'s quota gate state onto the pacer's wire values.

    The Hermes-side fleet quota gate is not merged into this checkout, so
    ``DispatchResult`` carries no ``quota_state`` at all. Absence is
    reported as ``unknown`` -- the honest reading, and the one the pacer's
    policy treats as a reason to escalate rather than to relax.

    A gate that IS present (``tripped``) is passed through verbatim; any
    value the pacer does not know is degraded to ``unknown`` rather than
    invented.
    """
    state = getattr(result, "quota_state", None)
    if not state:
        return "unknown"
    text = str(state).strip().lower()
    return text if text in _QUOTA_STATES else "unknown"


def map_quota_deferred(result: Any) -> int:
    """How many claims the quota gate withheld this tick.

    ``quota_deferred`` is a list of task ids on the quota-gate branch and
    absent on this checkout; either way the count is 0 when no gate ran.
    """
    deferred = getattr(result, "quota_deferred", None)
    if not deferred:
        return 0
    try:
        return len(deferred)
    except TypeError:
        return 0


def build_dispatch_tick(
    *,
    tick_index: int,
    observed_at: float,
    ready_count: int,
    todo_count: int = 0,
    spawned_count: int = 0,
    quota_deferred: int = 0,
    quota_state: str = "unknown",
    worker_cap: int = 0,
    spawn_budget: int = 0,
    drain_candidates: tuple[DrainCandidate, ...] = (),
) -> dict[str, Any]:
    """Build the ``DispatchTick`` body the JEV-2 pacing endpoint expects.

    The endpoint's model forbids extra fields, so the body is exactly the
    declared set. ``quota_state`` must be one of the pacer's four states;
    an unrecognized value is degraded to ``unknown`` rather than rejected.
    """
    state = str(quota_state or "unknown").strip().lower()
    if state not in _QUOTA_STATES:
        state = "unknown"
    return {
        "tick_index": int(max(0, tick_index)),
        "observed_at": float(observed_at),
        "ready_count": int(max(0, ready_count)),
        "todo_count": int(max(0, todo_count)),
        "spawned_count": int(max(0, spawned_count)),
        "quota_deferred": int(max(0, quota_deferred)),
        "quota_state": state,
        "worker_cap": int(max(0, worker_cap)),
        "spawn_budget": int(max(0, spawn_budget)),
        "drain_candidates": [c.to_payload() for c in drain_candidates],
    }


def _parse_decision(raw: Any) -> Optional[PacerDecision]:
    if not isinstance(raw, dict):
        return None
    action = str(raw.get("action") or "none").strip().lower()
    if action not in ("none", "held", "paced_drain", "escalate"):
        # An action the caller does not understand must not be guessed at.
        action = "escalate"
    try:
        max_spawns = int(raw.get("max_spawns_per_tick") or 0)
    except (TypeError, ValueError):
        max_spawns = 0
    hold_until = raw.get("hold_until")
    try:
        hold_until = float(hold_until) if hold_until is not None else None
    except (TypeError, ValueError):
        hold_until = None

    def _ids(key: str) -> tuple[str, ...]:
        value = raw.get(key) or ()
        if not isinstance(value, (list, tuple)):
            return ()
        return tuple(str(v) for v in value if v)

    return PacerDecision(
        action=action,
        stalled=bool(raw.get("stalled")),
        max_spawns_per_tick=max(0, max_spawns),
        drain_order=_ids("drain_order"),
        deferred_order=_ids("deferred_order"),
        hold_until=hold_until,
        rationale=str(raw.get("rationale") or ""),
        evidence=str(raw.get("evidence") or ""),
    )


def _parse_receipt(raw: Any) -> Optional[ClassFReceipt]:
    if not isinstance(raw, dict):
        return None
    dedupe_key = str(raw.get("dedupe_key") or "")
    class_name = str(raw.get("class_name") or "")
    if not class_name and not dedupe_key:
        return None
    verify = raw.get("operator_verify") or ()
    if not isinstance(verify, (list, tuple)):
        verify = ()
    return ClassFReceipt(
        class_id=str(raw.get("class_id") or "f"),
        class_name=class_name,
        dedupe_key=dedupe_key,
        should_notify=bool(raw.get("should_notify")),
        suppressed=bool(raw.get("suppressed")),
        headline=str(raw.get("headline") or ""),
        evidence=str(raw.get("evidence") or ""),
        operator_verify=tuple(str(v) for v in verify),
        window_s=float(raw.get("window_s") or 0.0),
        ready_count=int(raw.get("ready_count") or 0),
        barren_ticks=int(raw.get("barren_ticks") or 0),
        quota_deferred=int(raw.get("quota_deferred") or 0),
        quota_state=str(raw.get("quota_state") or "unknown"),
        observed_at=float(raw.get("observed_at") or 0.0),
    )


def parse_pacing_response(body: bytes) -> Optional[PacerAdvisory]:
    """Parse a pacing response body into an advisory.

    Returns ``None`` for anything that is not a pacing response -- a
    frontend HTML catch-all, an empty body, or JSON without a ``decision``.
    """
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    decision = _parse_decision(payload.get("decision"))
    if decision is None:
        return None
    receipt = _parse_receipt(payload.get("receipt"))
    advisory = payload.get("advisory")
    opinion: Optional[str] = None
    confidence: Optional[float] = None
    if isinstance(advisory, dict):
        raw_action = advisory.get("action")
        if raw_action:
            opinion = str(raw_action)
        raw_conf = advisory.get("confidence")
        if isinstance(raw_conf, (int, float)) and not isinstance(raw_conf, bool):
            confidence = float(raw_conf)
    return PacerAdvisory(
        decision=decision,
        receipt=receipt,
        model_opinion=opinion,
        model_confidence=confidence,
    )


class PacerClient:
    """A best-effort client for the JEV-2 pacing surface.

    Every method here is failure-tolerant by construction: the dispatcher
    consults the pacer as an observer, and an observer that raises is a
    dispatcher that raises.
    """

    def __init__(
        self,
        url: Optional[str] = None,
        token: Optional[str] = None,
        timeout_s: Optional[float] = None,
    ) -> None:
        self._url = url or os.environ.get(PACER_URL_ENV, "").strip() or DEFAULT_PACER_URL
        self._token = (
            token
            if token is not None
            else os.environ.get(PACER_TOKEN_ENV, "").strip()
        )
        self._timeout = timeout_s if timeout_s is not None else _resolve_timeout()

    @property
    def url(self) -> str:
        return self._url

    def observe(self, tick: dict[str, Any]) -> Optional[PacerAdvisory]:
        """Observe one dispatch tick with the pacer.

        Returns the advisory, or ``None`` when the pacer could not be
        reached, answered with something that is not a pacing response, or
        answered with an error. A ``None`` here means "no advice", which the
        caller must treat as "carry on as before".
        """
        if not self._url:
            return None
        body = json.dumps(tick).encode("utf-8")
        request = urllib.request.Request(
            self._url,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                **({"X-Erature-Service-Token": self._token} if self._token else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                content_type = (response.headers.get("Content-Type") or "").lower()
                raw = response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            # Unreachable, timed out, refused, or a bad status: all "no advice".
            _log.debug("jev pacer unreachable (%s): %s", type(exc).__name__, exc)
            return None
        if "json" not in content_type:
            # A 200 that is not JSON is the frontend catch-all, not advice.
            _log.debug("jev pacer returned non-JSON content type %r", content_type)
            return None
        advisory = parse_pacing_response(raw)
        if advisory is None:
            _log.debug("jev pacer returned a body that is not a pacing response")
        return advisory


__all__ = [
    "DEFAULT_PACER_URL",
    "DEFAULT_TIMEOUT_S",
    "ClassFReceipt",
    "DrainCandidate",
    "PacerAdvisory",
    "PacerClient",
    "PacerDecision",
    "build_dispatch_tick",
    "map_quota_deferred",
    "map_quota_state",
    "parse_pacing_response",
]
