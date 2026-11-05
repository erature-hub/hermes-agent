"""Dispatcher subprocess: submit one claim to its profile owner; never run an agent."""
# Spawned as a bare ``sys.executable -m``: in a PM install that interpreter carries no dependencies
# until hermes_bootstrap selects the committed environment, so it must be the first import.
try:
    import hermes_bootstrap
except ModuleNotFoundError as exc:
    if exc.name != "hermes_bootstrap":
        raise

import asyncio
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import time
from typing import Optional

from hermes_cli.kanban_worker_exit import PREBIND_TIMEOUT_EXIT_CODE

# A worker that loses its owner (a dropped socket, a restart, `hermes update` draining it or a new
# owner still starting) waits for it instead of exiting: the condition is the host's, not the card's,
# and a failed exit here spends the card's failure budget and lets the owner end the turn. Asking
# again is safe: ``kanban.run`` is idempotent per attempt and ``prompt.receipt`` is a read.
# ``OWNER_WAIT_S`` is the longest the owner may stay silent between answers before the worker
# gives up (the first ``kanban.run`` answer has its own wait, ``admission_answer_budget``).
OWNER_WAIT_S = 180.0
_TRANSIENT_OWNER_STATES = frozenset({'draining', 'starting'})
# In-band owner refusals that leave the attempt intact: a draining owner admits nothing new, and a
# retired authority's successor holds the admission the next connection reads.
_TRANSIENT_RPC_REASONS = frozenset({'runtime_draining', 'runtime_retired'})
# Control-socket answers a restarting owner gives (an ``inaccessible`` owner's reason, from
# discovery or the ticket fetch): a reset or half-written answer, or a replaced owner refusing a
# stale instance. Unsafe nodes or peers, a refused authorization, a bad pointer or a failed start
# stay final.
_RESTART_CONTROL_REASONS = frozenset({'invalid_control_peer', 'incomplete_control_response',
                                      'invalid_control_response'})
# Past the board lock wait: the owner's session setup and receipt before and after the write.
ADMISSION_ANSWER_MARGIN_S = 60.0

# A managed worker's boot is the OWNER's cost, and on this host it is a heavy one: a cold PM
# dependency probe alone measured 17.9 s, and a gateway-env boot 82-107 s. The owner's own hello
# deadline is derived from that baseline, so a fixed client-side budget would fire FIRST and cut a
# boot that was going to make it -- booking the card a crash for being slow. Scale with what the
# host has actually been paying: the p90 of recent ``owner_admitted`` -> ``worker_bound`` gaps on
# this board, floored by the platform prior and capped so one pathological boot cannot park a card
# behind a twenty-minute wait.
SPAWN_BASELINE_PRIORS_S = {'win32': 90.0, 'darwin': 45.0}
SPAWN_BASELINE_DEFAULT_S = 30.0
SPAWN_BASELINE_CEILING_S = 300.0
SPAWN_BASELINE_SAMPLES = 20
SPAWN_BASELINE_WINDOW_S = 24 * 3600
# How often the owner-answered receipt loop re-reads the board to see whether the attempt bound.
BIND_PROBE_S = 2.0


def admission_answer_budget():
    """Seconds to wait for ``kanban.run``'s answer: the longest the owner's admission write can
    wait on the board lock (``write_txn`` retries ``BEGIN IMMEDIATE`` ``_BUSY_MAX_RETRIES`` times,
    each under the busy timeout) plus ``ADMISSION_ANSWER_MARGIN_S``."""
    from hermes_cli import kanban_db_connect
    lock_wait = (kanban_db_connect._BUSY_MAX_RETRIES + 1) * kanban_db_connect._resolve_busy_timeout_ms() / 1000
    return lock_wait + ADMISSION_ANSWER_MARGIN_S


class PreBindTimeout(RuntimeError):
    """The attempt ended before anything about the card ran.

    Not a ``GatewayClientError``: those are the owner's answer about the attempt, while this is
    the board's verdict that there was never an attempt to answer for. ``_submit`` turns it into
    ``PREBIND_TIMEOUT_EXIT_CODE``, which the dispatcher books as a claim release instead of a
    crash -- a host too slow to boot a worker must stop spending the card's failure budget.
    """

    def __init__(self, message, *, elapsed, deadline, reason=''):
        super().__init__(message)
        self.elapsed = float(elapsed)
        self.deadline = float(deadline)
        self.reason = reason

    def diagnostic(self):
        """The one line the dispatcher surfaces as the run's last output: what missed, by how much."""
        why = f'; owner said {self.reason}' if self.reason else ''
        return (f'PreBindTimeout: worker boot took {self.elapsed:.0f}s > {self.deadline:.0f}s '
                f'(no worker_bound for this run; claim released, no failure counted{why})')


def spawn_baseline_prior(platform=None):
    """The cold-boot prior for *platform*, as data so a test never has to fake the host."""
    return SPAWN_BASELINE_PRIORS_S.get(platform or sys.platform, SPAWN_BASELINE_DEFAULT_S)


def _spawn_gaps(board_db):
    """Recent ``owner_admitted`` -> ``worker_bound`` gaps in seconds on this board, newest first.

    The board is the only durable witness of how long a boot actually took here, and the window
    is anchored on the board's own clock (its newest bind), not this process's: a worker spawned
    minutes later still reads "recent" as the board means it. Any read failure yields ``[]`` --
    an unreadable board must never invent a deadline.
    """
    import sqlite3
    try:
        uri = str(Path(board_db).resolve().as_uri()) + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            # Newest-first on the rowid: a bounded backward scan instead of a full-table MAX.
            since_row = conn.execute(
                'SELECT created_at FROM task_events WHERE kind = ? ORDER BY id DESC LIMIT 1',
                ('worker_bound',)).fetchone()
            since = since_row[0] if since_row else None
            if since is None:
                return []
            rows = conn.execute(
                'SELECT b.created_at - a.created_at AS gap FROM task_events b '
                'JOIN task_events a ON a.run_id = b.run_id AND a.kind = ? '
                'WHERE b.kind = ? AND b.run_id IS NOT NULL AND b.created_at >= ? '
                'ORDER BY b.created_at DESC LIMIT ?',
                ('owner_admitted', 'worker_bound', int(since) - SPAWN_BASELINE_WINDOW_S,
                 SPAWN_BASELINE_SAMPLES)).fetchall()
            return [float(row[0]) for row in rows if row[0] is not None and row[0] >= 0]
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        # A busy, missing, legacy or half-written board is not evidence of anything: fall back
        # to the prior rather than guessing how long a boot took.
        return []


def spawn_baseline(board_db=None):
    """This host's typical cold-boot cost: the p90 of its recent bind gaps, floored and capped.

    p90 rather than the median: a deadline drawn from the *typical* boot ignores exactly the slow
    ones that time out, and ``handshake_deadline`` doubles it for headroom.
    """
    gaps = sorted(_spawn_gaps(board_db)) if board_db else []
    observed = gaps[max(0, (len(gaps) * 9) // 10 - 1)] if gaps else 0.0
    return min(SPAWN_BASELINE_CEILING_S, max(spawn_baseline_prior(), observed))


def owner_hello_deadline():
    """The owner's own hello deadline, so this client never cuts a boot the owner still allows.

    A client budget tighter than the owner's is the same bug wearing the other hat: the owner
    waits for its child while this process declares the boot dead. Imported lazily and forgivingly
    -- the worker's startup path must not pull the gateway in, and an install without it (or a
    test double) simply contributes nothing.
    """
    try:
        from gateway.session_managed_worker import HELLO_SECONDS
    except (ImportError, AttributeError):
        # Not installed (or not a version that has it): no opinion, the baseline stands alone.
        return 0.0
    return float(HELLO_SECONDS)


def handshake_deadline(board_db=None):
    """How long a boot may take before it is a pre-bind timeout.

    ``max(OWNER_WAIT_S, owner_hello_deadline, 2 * spawn_baseline)``: never tighter than the budget
    every owner answer already had, never tighter than the deadline the owner itself grants its
    child, and twice what this host has been paying so one slow boot is absorbed rather than
    charged to the card.
    """
    return max(OWNER_WAIT_S, owner_hello_deadline(), spawn_baseline(board_db) * 2.0)


def _attempt_evidence(board_db, run_id):
    """``(spawned_at, bound_at, lost_reason)`` for *run_id*, read from the board.

    ``spawned_at`` is the run's own start (written when the dispatcher fired us), ``bound_at`` the
    ``worker_bound`` a bound interpreter writes under its claim, and ``lost_reason`` whatever the
    owner recorded when its managed worker died. ``(None, None, '')`` when the board is unreadable
    or the run unknown: missing evidence must never become a verdict.
    """
    if run_id is None:
        return (None, None, '')
    import json
    import sqlite3
    try:
        uri = str(Path(board_db).resolve().as_uri()) + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, timeout=2.0)
        try:
            row = conn.execute(
                'SELECT started_at FROM task_runs WHERE id = ?', (int(run_id),)).fetchone()
            started_at = float(row[0]) if row and row[0] is not None else None
            bound = conn.execute(
                'SELECT created_at FROM task_events WHERE run_id = ? AND kind = ? '
                'ORDER BY id DESC LIMIT 1', (int(run_id), 'worker_bound')).fetchone()
            lost = conn.execute(
                'SELECT payload FROM task_events WHERE run_id = ? AND kind = ? '
                'ORDER BY id DESC LIMIT 1', (int(run_id), 'worker_lost')).fetchone()
        finally:
            conn.close()
    except (sqlite3.Error, OSError, ValueError, TypeError):
        # An unreadable board cannot prove an attempt never bound, so it must not condemn one.
        return (None, None, '')
    reason = ''
    if lost and lost[0]:
        try:
            reason = str((json.loads(lost[0]) or {}).get('reason') or '')
        except (TypeError, ValueError, AttributeError):
            reason = ''
    return (started_at, float(bound[0]) if bound else None, reason)


def _unbound_elapsed(params, board_db):
    """``(seconds_since_spawn, owner_reason)`` when NOTHING bound this run, else ``None``.

    ``None`` is "cannot prove": no run id, an unreadable board, an unknown run, or a
    ``worker_bound`` event -- in each case the attempt's own exit speaks, not this deadline.
    """
    started_at, bound_at, reason = _attempt_evidence(board_db, params.get('run_id'))
    if bound_at is not None or started_at is None:
        return None
    return (time.time() - started_at, reason)


def _overdue_prebind(params, board_db, deadline):
    """The ``PreBindTimeout`` for a boot that is still unbound past *deadline*, else ``None``."""
    unbound = _unbound_elapsed(params, board_db)
    if unbound is None or unbound[0] < deadline:
        return None
    elapsed, reason = unbound
    return PreBindTimeout(
        f'Kanban pre-bind deadline missed: nothing bound this run in {elapsed:.0f}s',
        elapsed=elapsed, deadline=deadline, reason=reason)


def _settled_prebind(params, board_db, deadline):
    """The ``PreBindTimeout`` for a receipt that settled with nothing bound, else ``None``.

    No elapsed floor: the owner may give up on its own (shorter) hello deadline well before ours,
    and the settlement is proof the attempt is over, so the deadline only supplies context.
    """
    unbound = _unbound_elapsed(params, board_db)
    if unbound is None:
        return None
    elapsed, reason = unbound
    return PreBindTimeout(
        f'Kanban attempt settled without binding: {elapsed:.0f}s after spawn, no worker_bound',
        elapsed=elapsed, deadline=deadline, reason=reason)


@dataclass
class _Claim:
    """What the owner has said about this attempt, carried across connections."""
    answered: float  # monotonic time of the owner's last answer
    session_id: Optional[str] = None
    admission_id: Optional[str] = None

    def silent_for(self):
        return time.monotonic() - self.answered


async def run(params, board_db):
    from agent.retry_utils import jittered_backoff
    from hermes_cli.gateway_client import connect_gateway, GatewayClientError
    from websockets.exceptions import WebSocketException
    if os.environ.get('HERMES_TUI_GATEWAY_URL'):
        raise GatewayClientError('Kanban requires the assigned local profile owner')
    # A lost socket is not a lost attempt, and exiting here would let ``_watch_submitter`` end the
    # owner's in-flight turn, so the next connection resumes whatever the last one reached. The
    # wait is bounded from the owner's last answer: an owner that keeps answering keeps the
    # worker, and one that stays silent for ``OWNER_WAIT_S`` fails the worker honestly.
    claim = _Claim(answered=time.monotonic())
    # Never tighter than the answer budgets above, and longer when this host has been paying a
    # slow boot: the owner's own hello deadline is derived from the same baseline, so a fixed
    # budget here would cut a boot that was going to make it.
    deadline = handshake_deadline(board_db)
    failures = 0
    while True:
        seen = claim.answered
        try:
            async with connect_gateway() as client:
                # kanban.run subscribes this socket to the card's session, but only receipts matter
                # here: discard the turn's events, or they fill the client's bounded queue and its
                # reader stops, with the next receipt behind it.
                drain = asyncio.create_task(_discard(client.events))
                try:
                    receipt = await _await_receipt(client, params, board_db, claim, deadline=deadline)
                finally:
                    drain.cancel()
        # A restart also surfaces as a refused or reset dial, a failed handshake, or an answer
        # that never came.
        except (GatewayClientError, WebSocketException, OSError, TimeoutError) as exc:
            if not _restartable(exc):
                raise
            if claim.silent_for() >= deadline:
                # WebSocket errors can embed remote bodies; only our own messages are quoted.
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                detail = exc if isinstance(exc, GatewayClientError) else type(exc).__name__ + (
                    f' (HTTP {status})' if status else '')
                # Past the deadline, silence is a boot that never happened only while the board
                # holds no ``worker_bound`` for this run; a bound attempt is a live turn, and
                # failing it loudly is still the honest answer.
                timeout = _overdue_prebind(params, board_db, deadline)
                if timeout is not None:
                    raise timeout from exc
                raise GatewayClientError(f'Kanban owner silent for {claim.silent_for():.0f}s: {detail}') from exc
        else:
            return _exit_code(receipt, params, board_db)
        failures = 1 if claim.answered != seen else failures + 1
        # Jittered (1-2 s first, up to 16 s): workers of one dead owner do not all redial, and so
        # start a replacement, in the same instant.
        await asyncio.sleep(jittered_backoff(failures, base_delay=1.0, max_delay=8.0, jitter_ratio=1.0))


def _restartable(exc):
    """False when *exc* is the owner's own final answer; True for a restarting owner or a lost transport."""
    from hermes_cli.gateway_client import GatewayRPCError, GatewayUnavailableError
    if isinstance(exc, GatewayRPCError):
        return str(exc) in _TRANSIENT_RPC_REASONS
    if isinstance(exc, GatewayUnavailableError):
        return exc.state in _TRANSIENT_OWNER_STATES or exc.reason_code in _RESTART_CONTROL_REASONS
    return True


async def _discard(events):
    while True:
        await events.get()


async def _await_receipt(client, params, board_db, claim, *, deadline=OWNER_WAIT_S):
    """Submit the claim (or, on a later connection, re-read the admission it got) and return its
    settled receipt, recording each owner answer on *claim*.

    An answering owner keeps the attempt alive indefinitely, so the boot itself needs a bound:
    every ``BIND_PROBE_S`` the board is asked whether this run bound, and a boot still unbound
    past *deadline* ends here as a ``PreBindTimeout`` instead of a turn nobody can account for.
    """
    if claim.admission_id is None:
        db = str(await asyncio.to_thread(Path(board_db).resolve))
        # The owner answers only after it checks the claim and writes its ``owner_admitted`` marker
        # in one board write transaction. The board (the cross-profile dispatch bus) can make that
        # wait out its busy timeout, so the 30 s default left the owner admitting an attempt whose
        # submitter had exited (the later-bound worker's submitter-liveness watcher ended it, and the
        # admission was left ``unknown``). The wait is still finite: once the marker commits,
        # ``enforce_max_runtime`` and TTL reclaim skip the run (``owner_reclaim_paused``), so an owner
        # that stalls after admitting is bounded by this wait and ``OWNER_WAIT_S``.
        accepted = await client.rpc('kanban.run', _timeout=admission_answer_budget(), **dict(params, db=db))
        receipt = accepted['receipt']
        claim.session_id, claim.admission_id = accepted['session_id'], receipt['admission_id']
        print(f'Session: {claim.session_id}', file=sys.stderr)
    else:
        receipt = await _receipt(client, claim)
    claim.answered = time.monotonic()
    probe_at = time.monotonic()
    while receipt['status'] not in {'terminal', 'unknown'}:
        await asyncio.sleep(.2)
        receipt = await _receipt(client, claim)
        claim.answered = time.monotonic()
        if time.monotonic() - probe_at >= BIND_PROBE_S:
            probe_at = time.monotonic()
            timeout = _overdue_prebind(params, board_db, deadline)
            if timeout is not None:
                raise timeout
    return receipt


def _exit_code(receipt, params, board_db):
    from hermes_cli.gateway_client import GatewayClientError
    if receipt['status'] == 'unknown':
        raise GatewayClientError('Kanban execution unknown; retained by owner')
    # A settlement with no ``worker_bound`` behind it is the host's answer, not the card's: the
    # attempt died in its boot (the owner's hello deadline, an OOM-killed child) and the empty
    # turn's exit code would book a ``crashed`` that spends the card's retry budget.
    timeout = _settled_prebind(params, board_db, handshake_deadline(board_db))
    if timeout is not None:
        raise timeout
    from gateway.session_kanban import worker_result
    result = worker_result(board_db, params)
    # The attempt's reply ends the log, as a one-shot worker's did: crash diagnostics quote it.
    if result.get('last_output'):
        print(result['last_output'])
    return result.get('exit_code', 1)


async def _receipt(client, claim):
    return await client.rpc('prompt.receipt', session_id=claim.session_id, admission_id=claim.admission_id)


def main():
    # The dead-worker sweep books this process's exit from the trailer in its log (it never
    # reaps us), so every exit path writes it — the same contract as a quiet one-shot worker.
    from hermes_cli.quiet_single_query import KANBAN_WORKER_EXIT_TRAILER
    rc = 1
    try:
        rc = _submit()
    finally:
        print(f'\n{KANBAN_WORKER_EXIT_TRAILER}{int(rc)}', file=sys.stderr, flush=True)
    return rc


def _submit():
    try:
        params = {'board': os.environ['HERMES_KANBAN_BOARD'], 'task_id': os.environ['HERMES_KANBAN_TASK'],
            'run_id': int(os.environ['HERMES_KANBAN_RUN_ID']), 'claim_lock': os.environ['HERMES_KANBAN_CLAIM_LOCK']}
        board_db = os.environ['HERMES_KANBAN_DB']
        # Owner startup must never inherit this worker's task/tool/goal identity.
        for key in list(os.environ):
            # The busy timeout is a host tuning knob, not task identity: the answer budget reads it.
            if key.startswith('HERMES_KANBAN_') and key not in ('HERMES_KANBAN_HOME', 'HERMES_KANBAN_BUSY_TIMEOUT_MS'):
                os.environ.pop(key)
        for key in ('HERMES_SESSION_SOURCE', 'HERMES_TENANT', 'TERMINAL_CWD'):
            os.environ.pop(key, None)
        return asyncio.run(run(params, board_db))
    except PreBindTimeout as exc:
        # The dispatcher reads this as the run's last output and books the exit as a claim
        # release: name the deadline that was missed instead of exiting 1 with no message.
        print(exc.diagnostic(), file=sys.stderr)
        return PREBIND_TIMEOUT_EXIT_CODE
    except (KeyError, ValueError, OSError) as exc:
        print(f'Kanban owner refused: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
