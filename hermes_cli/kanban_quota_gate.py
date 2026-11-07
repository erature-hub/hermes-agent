"""Fleet self-throttle at claim time: weekly cloud-token ledger gate.

The Hermes worker fleet talks to Ollama Cloud directly, so it never passes
through aisuite's sovereign router and the router's 5M/week breaker cannot see
the traffic it spends. On 2026-10-18 the account's provider-side weekly cap
429'd and took Paul's *interactive* crons (exec summaries, weekly security
brief) down with it. This gate gives the fleet teeth at the one seam every
worker passes through: the kanban dispatcher's claim step.

Policy (card t_074cc4fc, Paul-approved 2026-10-20):

* **Basis** — ``input_tokens + output_tokens`` for cloud-tagged models in the
  current quota week, summed across every profile's ``state.db``. Same rows,
  same window (Monday 00:00Z) and same over-counts-rather-than-under-counts
  ``last_seen >= week_start`` semantics as
  ``aisuite/backend/app/routing/sovereign_inference_router.py``; cache reads are
  excluded there and excluded here. Local traffic cannot 429 and is not
  counted.
* **States** — ``ok`` / ``warn`` (>= 70%) / ``tripped`` (>= 85%, both
  env-overridable) / ``unknown`` (no readable ledger).
* **TRIP defers the claim** — the card stays ``ready`` with a
  ``claim_deferred_quota`` event, other lanes keep ticking, and in-flight
  workers are never killed.
* **Allow lane** — ``priority >= 8`` (the DECISION lane) always claims: the
  point of the gate is to keep Paul's interactive work clear of the fleet's
  bulk.
* **One alert per transition** — an unchanged state is silent, so a 30s
  dispatcher tick cannot spam. Every alert is a WARNING log line; set
  ``HERMES_FLEET_QUOTA_ALERT_CMD`` (message on stdin) to also push it out, e.g.
  ``HERMES_FLEET_QUOTA_ALERT_CMD="hermes send --to telegram"``.
* **FAIL-OPEN** — an unreadable/missing/locked ledger yields ``unknown`` plus a
  WARNING; the fleet is never bricked by a broken telemetry store.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import shlex
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)

#: Weekly cloud budget, matching the aisuite sovereign router's policy default.
DEFAULT_WEEKLY_BUDGET_TOKENS = 5_000_000
DEFAULT_WARN_RATIO = 0.70
DEFAULT_TRIP_RATIO = 0.85
#: ``priority`` at or above this claims even while throttled (DECISION lane).
DEFAULT_ALLOW_PRIORITY = 8
#: Ledger re-read interval. The dispatcher ticks every ~30s; five state.db reads
#: per tick is real I/O for a number that moves by one worker's spend per tick.
DEFAULT_CACHE_SECONDS = 60

STATE_OK = "ok"
STATE_WARN = "warn"
STATE_TRIPPED = "tripped"
STATE_UNKNOWN = "unknown"

#: Cloud-tagged rows only — verbatim the router's ``_CLOUD_ROW_SQL``.
CLOUD_ROW_SQL = "(model LIKE '%:cloud%' OR model LIKE '%-cloud%')"

_FALSEY = frozenset({"0", "false", "no", "off", "disabled", "disable"})

_CACHE: dict[str, Any] = {"key": None, "at": 0.0, "reading": None}


# ─── Week window ────────────────────────────────────────────────────────────


def week_window(now: Optional[float] = None, *, reset_weekday: int = 0,
                reset_hour: int = 0) -> tuple[int, int]:
    """``(start_epoch, end_epoch)`` of the quota week containing ``now``.

    Anchored to ``reset_weekday`` (ISO: Monday=0) at ``reset_hour`` UTC and
    exactly 7 days long — the same semantics as the aisuite router's
    ``week_window``, which is what decides when the provider resets.
    """
    ts = time.time() if now is None else now
    moment = datetime.fromtimestamp(ts, tz=UTC)
    days_since = (moment.weekday() - reset_weekday) % 7
    start = (moment - timedelta(days=days_since)).replace(
        hour=reset_hour, minute=0, second=0, microsecond=0
    )
    if start > moment:
        start -= timedelta(days=7)
    start_epoch = int(start.timestamp())
    return start_epoch, start_epoch + 7 * 86_400


# ─── Configuration ──────────────────────────────────────────────────────────


def _env_flag(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = str(env.get(name, "")).strip().lower()
    if not raw:
        return default
    return raw not in _FALSEY


def _env_float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        log.warning("fleet-quota-gate: ignoring non-numeric %s=%r", name, raw)
        return default


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = str(env.get(name, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("fleet-quota-gate: ignoring non-numeric %s=%r", name, raw)
        return default


@dataclass(frozen=True)
class GateConfig:
    """Effective gate policy for one evaluation."""

    enabled: bool = True
    budget_tokens: int = DEFAULT_WEEKLY_BUDGET_TOKENS
    warn_ratio: float = DEFAULT_WARN_RATIO
    trip_ratio: float = DEFAULT_TRIP_RATIO
    allow_priority: int = DEFAULT_ALLOW_PRIORITY
    cache_seconds: int = DEFAULT_CACHE_SECONDS

    @property
    def warn_tokens(self) -> int:
        return int(self.budget_tokens * self.warn_ratio)

    @property
    def trip_tokens(self) -> int:
        return int(self.budget_tokens * self.trip_ratio)


def gate_config(env: Optional[Mapping[str, str]] = None) -> GateConfig:
    """Resolve the gate policy from the environment (defaults = policy)."""
    env = os.environ if env is None else env
    budget = _env_int(env, "HERMES_FLEET_WEEKLY_BUDGET_TOKENS", DEFAULT_WEEKLY_BUDGET_TOKENS)
    return GateConfig(
        # Default ON: the approval is recorded on the card, so an operator must
        # opt OUT explicitly rather than opt in.
        enabled=_env_flag(env, "HERMES_FLEET_QUOTA_GATE", True),
        budget_tokens=budget if budget > 0 else DEFAULT_WEEKLY_BUDGET_TOKENS,
        warn_ratio=_env_float(env, "HERMES_FLEET_QUOTA_WARN_RATIO", DEFAULT_WARN_RATIO),
        trip_ratio=_env_float(env, "HERMES_FLEET_TRIP_RATIO", DEFAULT_TRIP_RATIO),
        allow_priority=_env_int(
            env, "HERMES_FLEET_QUOTA_ALLOW_PRIORITY", DEFAULT_ALLOW_PRIORITY
        ),
        cache_seconds=_env_int(
            env, "HERMES_FLEET_QUOTA_CACHE_SECONDS", DEFAULT_CACHE_SECONDS
        ),
    )


# ─── Ledger ─────────────────────────────────────────────────────────────────


def home_root() -> Path:
    """Hermes root that owns every profile's ``state.db``.

    ``kanban_home()`` is the shared-across-profiles root BY DESIGN (it resolves
    ``HERMES_HOME`` — which is ``<root>/profiles/<name>`` inside a worker — back
    to ``<root>``), and it honours ``HERMES_KANBAN_HOME``, so a test that pins a
    temp home also pins the ledgers it reads.
    """
    try:
        from hermes_cli.kanban_db import kanban_home
        return kanban_home()
    except Exception:  # pragma: no cover — kanban_db import is always available at tick time
        from hermes_constants import get_default_hermes_root
        return get_default_hermes_root()


def ledger_paths(root: Optional[Path] = None) -> list[Path]:
    """``<root>/state.db`` (default profile) plus ``<root>/profiles/*/state.db``.

    Missing files are skipped: a host with no ledgers reads as ``unknown`` and
    fails open, which is the documented behaviour, not an error.
    """
    base = Path(root) if root is not None else home_root()
    paths: list[Path] = []
    default_db = base / "state.db"
    if default_db.is_file():
        paths.append(default_db)
    for found in sorted(glob.glob(str(base / "profiles" / "*" / "state.db"))):
        candidate = Path(found)
        if candidate.is_file() and candidate != default_db:
            paths.append(candidate)
    return paths


def read_ledger(path: Path, since_epoch: float) -> tuple[int, dict[str, int], Optional[str]]:
    """``(tokens, per_model, error)`` for one ``state.db`` this quota week.

    Tries a read-only connection first so uncheckpointed WAL frames are
    visible, then ``immutable=1`` (which cannot see them) for a ledger we cannot
    take a read lock on. Never raises: the error string is returned instead.
    """
    query = (
        "SELECT model, COALESCE(SUM(input_tokens),0), COALESCE(SUM(output_tokens),0) "
        f"FROM session_model_usage WHERE last_seen >= ? AND {CLOUD_ROW_SQL} "
        "GROUP BY model"
    )
    error: Optional[str] = None
    for suffix in ("", "&immutable=1"):
        conn = None
        try:
            conn = sqlite3.connect(
                f"file:{path}?mode=ro{suffix}", uri=True, timeout=2
            )
            rows = conn.execute(query, (float(since_epoch),)).fetchall()
        except sqlite3.Error as exc:
            error = f"{type(exc).__name__}: {exc}"
            continue
        finally:
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:  # pragma: no cover — close is best-effort
                    pass
        models = {str(model): int(inp or 0) + int(out or 0) for model, inp, out in rows}
        return sum(models.values()), {m: n for m, n in models.items() if n}, None
    return 0, {}, error or "unreadable ledger"


# ─── Evaluation ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class QuotaReading:
    """One evaluation of the fleet's weekly cloud spend."""

    state: str
    used_tokens: int
    budget_tokens: int
    warn_tokens: int
    trip_tokens: int
    week_start: int
    week_end: int
    enabled: bool
    allow_priority: int
    sources: tuple[dict[str, Any], ...] = ()
    errors: tuple[str, ...] = ()

    def blocks_claim(self, priority: Any) -> bool:
        """True when a claim at ``priority`` must be deferred this tick."""
        if not self.enabled or self.state != STATE_TRIPPED:
            return False
        try:
            value = int(priority or 0)
        except (TypeError, ValueError):
            value = 0
        return value < self.allow_priority

    def utilization(self) -> Optional[float]:
        if self.budget_tokens <= 0:
            return None
        return self.used_tokens / self.budget_tokens

    def defer_event_key(self) -> tuple[str, int]:
        """Stable identity of the ``claim_deferred_quota`` event for this reading.

        Deliberately excludes ``used_tokens``: spend moves on nearly every tick
        of a throttled week (in-flight workers plus the operator's own traffic),
        so a key containing it matches nothing and the event re-emits once a
        minute for the rest of the week.
        """
        return (self.state, int(self.week_start))

    def payload(self) -> dict[str, Any]:
        """Compact, log/event-safe view (no per-model detail)."""
        return {
            "state": self.state,
            "used_tokens": self.used_tokens,
            "budget_tokens": self.budget_tokens,
            "trip_tokens": self.trip_tokens,
            "week_start": self.week_start,
        }


def stored_defer_event_key(raw: Any) -> Optional[tuple[str, int]]:
    """Read back the stable key from a ``claim_deferred_quota`` event payload.

    ``raw`` is the JSON text stored in ``task_events.payload`` (a dict is also
    accepted). Anything missing, unparseable, or from another schema returns
    ``None`` so the caller treats it as "no matching event" and writes one.
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return None
    if not isinstance(raw, Mapping):
        return None
    state = raw.get("state")
    week_start = raw.get("week_start")
    if not isinstance(state, str) or not isinstance(week_start, (int, float, str)):
        return None
    try:
        return (state, int(week_start))
    except (TypeError, ValueError):
        return None


def evaluate(
    *,
    now: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
    root: Optional[Path] = None,
    use_cache: bool = True,
) -> QuotaReading:
    """Read every ledger and classify the fleet's weekly cloud spend."""
    cfg = gate_config(env)
    moment = time.time() if now is None else now
    week_start, week_end = week_window(moment)
    base = Path(root) if root is not None else home_root()

    cache_key = f"{base}|{week_start}|{cfg.budget_tokens}|{cfg.warn_ratio}|{cfg.trip_ratio}"
    if use_cache and cfg.cache_seconds > 0:
        cached = _CACHE.get("reading")
        if (cached is not None and _CACHE.get("key") == cache_key
                and moment - float(_CACHE.get("at") or 0.0) < cfg.cache_seconds):
            return cached

    total = 0
    sources: list[dict[str, Any]] = []
    errors: list[str] = []
    paths = ledger_paths(base)
    if not paths:
        # No ledger at all: fail open, but say so once per evaluation so a
        # genuinely broken install is visible instead of silently unlimited.
        errors.append(f"no usage ledger under {base}")
    for path in paths:
        tokens, models, error = read_ledger(path, week_start)
        sources.append({"path": str(path), "tokens": tokens, "models": models})
        if error:
            errors.append(f"{path}: {error}")
            log.warning("fleet-quota-gate: ledger read failed (%s): %s", path, error)
        total += tokens

    if errors:
        # FAIL-OPEN: a partially blind reading would silently under-count (and a
        # fully blind one would block the fleet forever). Report ``unknown`` and
        # let claims proceed.
        state = STATE_UNKNOWN
    elif total >= cfg.trip_tokens:
        state = STATE_TRIPPED
    elif total >= cfg.warn_tokens:
        state = STATE_WARN
    else:
        state = STATE_OK

    reading = QuotaReading(
        state=state,
        used_tokens=total,
        budget_tokens=cfg.budget_tokens,
        warn_tokens=cfg.warn_tokens,
        trip_tokens=cfg.trip_tokens,
        week_start=week_start,
        week_end=week_end,
        enabled=cfg.enabled,
        allow_priority=cfg.allow_priority,
        sources=tuple(sources),
        errors=tuple(errors),
    )
    if use_cache and cfg.cache_seconds > 0:
        _CACHE["key"] = cache_key
        _CACHE["at"] = moment
        _CACHE["reading"] = reading
    return reading


def reset_cache() -> None:
    """Drop the memoized reading (test seam / forced refresh)."""
    _CACHE["key"] = None
    _CACHE["at"] = 0.0
    _CACHE["reading"] = None


# ─── Alerting: one message per state transition ─────────────────────────────


def _state_file(root: Optional[Path] = None) -> Path:
    base = Path(root) if root is not None else home_root()
    return base / "kanban" / "fleet_quota_gate.state.json"


def _read_announced_state(path: Path) -> Optional[dict[str, Any]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, dict) else None


def _write_announced_state(path: Path, state: str, reading: QuotaReading,
                           now: float) -> None:
    payload = {
        "state": state,
        "week_start": reading.week_start,
        "used_tokens": reading.used_tokens,
        "budget_tokens": reading.budget_tokens,
        "announced_at": int(now),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    except OSError as exc:  # pragma: no cover — best-effort bookkeeping
        # A state file we cannot write costs a duplicate alert, never a claim.
        log.warning("fleet-quota-gate: could not persist alert state (%s): %s", path, exc)


def format_alert(reading: QuotaReading, previous: Optional[str],
                 previous_week: Optional[int]) -> str:
    """Operator-facing one-liner for a state transition."""
    used = f"{reading.used_tokens:,}"
    budget = f"{reading.budget_tokens:,}"
    util = reading.utilization()
    pct = f"{util * 100:.1f}%" if util is not None else "unmeasured"
    week_end = datetime.fromtimestamp(reading.week_end, tz=UTC).strftime("%Y-%m-%d %H:%MZ")
    reset = (
        f" New quota week (was {datetime.fromtimestamp(previous_week, tz=UTC).strftime('%Y-%m-%d')})."
        if previous_week and previous_week != reading.week_start else ""
    )
    trip_pct = f"{reading.trip_tokens / reading.budget_tokens * 100:.0f}%" if (
        reading.budget_tokens > 0) else "the trip ratio"
    if reading.state == STATE_TRIPPED:
        return (
            f"HERMES FLEET QUOTA TRIP — {used} / {budget} cloud tokens this quota week "
            f"({pct}, trip at {trip_pct}). Non-decision claims now DEFER: ready cards stay "
            f"ready, in-flight workers finish, DECISION lane (priority >= "
            f"{reading.allow_priority}) still claims. Resumes at {week_end}.{reset}"
        )
    if reading.state == STATE_WARN:
        return (
            f"HERMES FLEET QUOTA WARNING — {used} / {budget} cloud tokens this quota week "
            f"({pct}, trip at {trip_pct}). Claims continue; the fleet defers at trip.{reset}"
        )
    if reading.state == STATE_UNKNOWN:
        return (
            f"HERMES FLEET QUOTA STATE UNKNOWN — usage ledger unreadable "
            f"({'; '.join(reading.errors) or 'no source'}). Gate fails OPEN: claims proceed. "
            f"Fix the ledger to restore throttling."
        )
    return (
        f"HERMES FLEET QUOTA RECOVERED — {used} / {budget} cloud tokens ({pct}). "
        f"Deferred claims resume.{reset}"
    )


def _deliver(message: str, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Optional outbound hook: ``HERMES_FLEET_QUOTA_ALERT_CMD`` (message on stdin).

    Deliberately a seam rather than an import of the gateway's messaging stack:
    the dispatcher must stay importable (and testable) without a gateway, and
    the operator chooses the transport (e.g. a ``hermes send`` wrapper).
    """
    env = os.environ if env is None else env
    command = str(env.get("HERMES_FLEET_QUOTA_ALERT_CMD", "")).strip()
    if not command:
        return None
    try:
        argv = shlex.split(command, posix=os.name != "nt")
        if os.name == "nt":
            # Non-POSIX mode keeps the quoting it split on, so
            # `"C:\Program Files\...\python.exe" -c "..."` would hand
            # CreateProcess a quoted argv[0]. Strip one matching pair per token.
            argv = [
                tok[1:-1] if len(tok) > 1 and tok[0] == tok[-1] and tok[0] in "\"'"
                else tok
                for tok in argv
            ]
        subprocess.run(
            argv, input=message, text=True, timeout=10, check=False,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        log.warning("fleet-quota-gate: alert command failed (%s): %s", command, exc)
        return f"{type(exc).__name__}: {exc}"
    return None


def announce(reading: QuotaReading, *, root: Optional[Path] = None,
             env: Optional[Mapping[str, str]] = None,
             now: Optional[float] = None) -> Optional[str]:
    """Emit ONE alert when the gate state changed; ``None`` when unchanged.

    The previous state is persisted beside the board so a fresh
    ``hermes kanban dispatch`` process (one-shot CLI, not the daemon loop)
    does not re-announce a state it already reported.
    """
    if not reading.enabled:
        return None
    path = _state_file(root)
    previous = _read_announced_state(path)
    previous_state = (str(previous.get("state") or "") or None) if previous else None
    previous_week = previous.get("week_start") if previous else None
    if previous_state == reading.state:
        return None
    if previous_state is None and reading.state == STATE_OK:
        # First observation in a healthy state has nothing to report, but it
        # must still be persisted so the NEXT transition is measured from "ok"
        # rather than announced as a recovery.
        _write_announced_state(path, reading.state, reading,
                              time.time() if now is None else now)
        return None
    message = format_alert(reading, previous_state, previous_week)
    log.warning("fleet-quota-gate: %s", message)
    _write_announced_state(path, reading.state, reading,
                          time.time() if now is None else now)
    _deliver(message, env)
    return message


def tick(*, now: Optional[float] = None, env: Optional[Mapping[str, str]] = None,
         root: Optional[Path] = None, announce_transition: bool = True
         ) -> Optional[QuotaReading]:
    """Dispatcher entry point: evaluate once per tick, announce transitions.

    Returns ``None`` when the gate is switched off — or when the evaluation
    itself blew up — so the caller can skip the per-row check entirely and
    behave exactly as before. A gate that cannot read its ledger must FAIL
    OPEN: throttling is an optimisation for the provider's weekly allowance,
    while a dispatcher tick that raises stops every worker on the board.
    """
    cfg = gate_config(env)
    if not cfg.enabled:
        return None
    try:
        reading = evaluate(now=now, env=env, root=root)
        if announce_transition:
            announce(reading, root=root, env=env, now=now)
    except Exception as exc:
        log.warning(
            "fleet-quota-gate: evaluation failed, failing OPEN (claims proceed): %s",
            exc, exc_info=True,
        )
        return None
    return reading
