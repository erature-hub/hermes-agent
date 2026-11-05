"""The exit contract between a Kanban worker process and the dispatcher that books it.

An exit code is the only evidence the dispatcher has about a death it did not witness, and it
decides the card's fate: ``KANBAN_RATE_LIMIT_EXIT_CODE`` requeues without charging the retry
budget, a terminal provider code trips the breaker on the first occurrence, and
``PREBIND_TIMEOUT_EXIT_CODE`` releases the claim of an attempt that never bound — a host too slow
to boot a worker is not a card that failed.

This module belongs to neither process on purpose. The worker client produces the pre-bind code,
the dispatcher maps it back to a booking, and the two must agree without importing each other's
world. Everything needed from ``kanban_db_dispatch`` is late-imported inside the function that
uses it, so importing this from the worker process pulls in the standard library and nothing else.

Split out of ``kanban_db_dispatch`` (which is far past the ``FILE_LINES`` ratchet) because it is
the one seam both sides of the spawn already share.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# "The attempt never bound its claim inside its handshake deadline": the boot, not the work, ran
# out of time (a loaded host, a cold dependency probe). Nothing about the card ran, so the
# dispatcher releases the claim WITHOUT counting a failure — the same no-charge policy as a quota
# wall, kept as its own code so the board can tell a slow host from a throttled provider. 76 is
# unused by every other Hermes exit path (75 EX_TEMPFAIL = rate limit, 78 EX_CONFIG = terminal).
PREBIND_TIMEOUT_EXIT_CODE = 76


@dataclass
class _DeadWorker:
    """How ``detect_crashed_workers`` should book one dead worker."""

    kind: str
    code: Optional[int]
    error_text: str
    event_kind: str
    event_payload: dict
    protocol_violation: bool = False
    rate_limited: bool = False
    terminal_provider: bool = False
    """``KANBAN_TERMINAL_PROVIDER_EXIT_CODE``: the provider rejected the worker's
    credential/model — trips the breaker on this first occurrence."""
    prebind_timeout: bool = False
    """``PREBIND_TIMEOUT_EXIT_CODE``: the attempt never bound its claim, so nothing about the
    card ran. Released to its source phase uncounted, so a loaded host cannot park a runnable
    card behind the failure breaker."""

    @property
    def run_outcome(self) -> str:
        # A requeue that must not read as a task failure is recorded under its own outcome, so
        # board history shows what actually happened instead of a phantom crash.
        if self.rate_limited:
            return "rate_limited"
        if self.prebind_timeout:
            return "prebind_timeout"
        return "crashed"


def _classify_dead_worker(
    pid: int, claimer: Optional[str], exit_code=None, *, task_id: Optional[str] = None,
    board: Optional[str] = None,
) -> _DeadWorker:
    """Map a dead worker's reaped exit status to its reclaim bookkeeping.

    A clean exit or a crash carries the worker's own last output (``worker_output`` in the event
    payload, appended to the error text) so the board and the retry worker see WHY instead of a
    bare label; a rate-limited requeue does not need it. A pre-bind timeout keeps it: the
    worker's ``PreBindTimeout:`` line is the whole diagnosis of a boot that never made it.
    """
    from hermes_cli import kanban_db_dispatch as dispatch

    dead = _classify_dead_worker_exit(pid, claimer, exit_code, task_id=task_id, board=board)
    if task_id and not dead.rate_limited:
        worker_output = dispatch._worker_final_output(task_id, board=board)
        if worker_output:
            dead.error_text += f"{dispatch.WORKER_OUTPUT_SEP}{worker_output!r}"
            dead.event_payload["worker_output"] = worker_output
    return dead


def _classify_dead_worker_exit(
    pid: int,
    claimer: Optional[str],
    exit_code=None,
    *,
    task_id: Optional[str] = None,
    board: Optional[str] = None,
) -> _DeadWorker:
    """Exit status -> reclaim bookkeeping, before the worker's own words are folded in.

    ``exit_code`` is the authority's exact-run result for a managed worker (a child of the
    profile owner, never of this dispatcher, so it cannot be reaped here). Otherwise the reap
    registry only knows children of THIS process; a per-tick dispatcher reads the exit trailer
    the worker left in its log instead, so the same death gets the same booking (protocol
    violation / rate-limit requeue / pre-bind release / crash) as under the gateway-embedded
    dispatcher. A worker that never reached its exit epilogue (killed, OOM) leaves no trailer and
    stays a plain crash.
    """
    from hermes_cli import kanban_db_dispatch as dispatch

    kind, code = dispatch._classify_worker_exit(pid)
    if exit_code is not None:
        kind, code = dispatch._exit_code_kind(int(exit_code))
    elif kind == "unknown" and task_id:
        logged = dispatch._worker_log_exit_code(task_id, board=board)
        if logged is not None:
            kind, code = dispatch._exit_code_kind(logged)
    if kind == "prebind_timeout":
        # The host, not the card: the worker exited before anything about the attempt ran, so a
        # retry (not a failure) is the correct booking. The worker's own ``PreBindTimeout:``
        # diagnostic reaches the board through the output tail `_classify_dead_worker` appends.
        return _DeadWorker(
            kind, code,
            f"pid {pid} missed its pre-bind deadline (exit {code}): the attempt never bound "
            "its claim, so it was released without counting a failure",
            "prebind_timeout",
            {"pid": pid, "claimer": claimer, "exit_kind": kind, "exit_code": code,
             "prebind_timeout": True},
            prebind_timeout=True,
        )
    if kind == "clean_exit":
        # rc=0 while still ``running``: usually the work succeeded and only the
        # paperwork was skipped; the corrective sentence reaches the retry
        # worker via ``build_worker_context``.
        return _DeadWorker(
            kind, code, dispatch._PROTOCOL_VIOLATION_ERROR, "protocol_violation",
            # ``protocol_violation`` is the durable marker for
            # _protocol_violation_streak: _end_run copies this payload into the
            # run metadata.
            {"pid": pid, "claimer": claimer, "exit_code": code, "protocol_violation": True},
            protocol_violation=True,
        )
    if kind == "rate_limited":
        # Quota wall — NOT a task failure. Release to the source phase and do
        # NOT count a failure so a long quota window can't trip the breaker.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited rate-limited (quota wall) — requeued without counting a failure",
            "rate_limited",
            {"pid": pid, "claimer": claimer, "exit_code": code},
            rate_limited=True,
        )
    if kind == "terminal_provider":
        # The worker classified its own provider failure as unhealable (credential
        # revoked, model gone): every further spawn would hit the same wall, so
        # ``_account_crashes`` trips the breaker now instead of after ``failure_limit``.
        return _DeadWorker(
            kind, code,
            f"pid {pid} exited on a terminal provider error (exit {code}): the provider rejected "
            "this profile's credential or model — fix the configuration, then unblock.",
            "crashed",
            {"pid": pid, "claimer": claimer, "exit_kind": kind, "exit_code": code, "terminal_provider": True},
            terminal_provider=True,
        )
    if kind == "nonzero_exit":
        error_text = f"pid {pid} exited with code {code}"
    elif kind == "signaled":
        error_text = f"pid {pid} killed by signal {code}"
    else:
        error_text = f"pid {pid} not alive"
    event_payload = {"pid": pid, "claimer": claimer}
    if code is not None and kind != "unknown":
        event_payload["exit_kind"] = kind
        event_payload["exit_code"] = code
    return _DeadWorker(kind, code, error_text, "crashed", event_payload)
