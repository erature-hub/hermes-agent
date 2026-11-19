"""The gateway's worker RPC must not block the event loop, and an RPC stall must not be
reported as a disk-full error.

Two defects, both observed in the 2026-11-17 Dokimasia cascade:

1. ``gateway/session_worker.py::worker_request`` called ``_claim()`` (a psutil liveness probe
   plus a json_extract scan over the worker's receipts in the owner's state.db) and
   ``_verify()`` (another state.db read) SYNCHRONOUSLY, on the gateway's event loop. One
   owner process serves EVERY profile, so a slow claim stalled unrelated profiles' worker
   RPCs until their receipts timed out (worker_receipt_timeout -> session turn lease lost
   -> session_persistence_failed). Both now run in a thread.

2. The timeout was classified as the unknown cause, which renders the disk/default copy
   ("the drive is out of room"). It has its own cause and its own copy now.
"""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from gateway import session_worker as sw
from hermes_state_errors import PERSISTENCE_ERROR_CAUSES, classify_persistence_error


# --------------------------------------------------------------------------- #
# 1. The claim/verify no longer run on the event loop
# --------------------------------------------------------------------------- #


def test_claim_and_verify_run_off_the_event_loop(monkeypatch):
    """_claim and _verify must be dispatched to a thread, not awaited inline."""
    calls = []

    def _slow_claim(connection, ref, params):
        # A blocking state.db read: the event loop must stay free while it runs.
        calls.append(('claim', threading.get_ident()))
        time.sleep(0.20)
        return 'claim'

    def _slow_verify(connection, ref, params, claim):
        calls.append(('verify', threading.get_ident()))
        time.sleep(0.20)

    monkeypatch.setattr(sw, '_claim', _slow_claim)
    monkeypatch.setattr(sw, '_verify', _slow_verify)
    # The adopt itself reaches the owner; we only care that claim/verify ran off-loop.
    monkeypatch.setattr(sw, 'adopt_worker_execution', lambda *a, **k: 'adopted')

    authority = SimpleNamespace(
        db=object(), epoch=1, profile_id='p',
        _require_admission_open=lambda: None,
        authorize=lambda actor, ref, cap: None,
        wake_after_worker=lambda sid: None,
    )
    actor = SimpleNamespace(profile_id='p', capabilities={'worker:adopt'}, subject='s')
    connection = SimpleNamespace(authority=authority, actor=actor)

    params = {
        'profile_id': 'p', 'session_id': 's', 'execution_id': 'e', 'generation': 1,
        'pid': 4321, 'birth': 1.0, 'secret': 'x' * 32,
    }

    async def _run():
        return await sw.worker_request(connection, 'ref', params, operation='adopt')

    # The loop must stay responsive DURING the claim: a second coroutine has to get a
    # turn while the blocking claim is still running.
    ticks = []

    async def _ticker():
        for _ in range(40):
            ticks.append(1)
            await asyncio.sleep(0.005)

    async def _main():
        task = asyncio.create_task(_run())
        await _ticker()
        await task

    asyncio.run(_main())

    assert [c[0] for c in calls] == ['claim', 'verify'], 'claim and verify must both run'
    # The ticker ran while the blocking claim was in flight: the loop was not blocked.
    assert len(ticks) >= 5, 'the event loop was blocked while the claim ran'
    # The blocking work ran on a worker thread, not the event loop thread.
    assert all(tid != threading.get_ident() for _, tid in calls), (
        'the claim/verify ran on the event loop thread')


def test_worker_request_runs_the_claim_before_verifying(monkeypatch):
    """Order is load-bearing: the verify authenticates the claim it is handed."""
    order = []
    monkeypatch.setattr(sw, '_claim', lambda c, r, p: order.append('claim') or 'claim')
    monkeypatch.setattr(sw, '_verify', lambda c, r, p, claim: order.append('verify'))

    authority = SimpleNamespace(
        db=object(), epoch=1, profile_id='p',
        _require_admission_open=lambda: None,
        authorize=lambda actor, ref, cap: None,
        wake_after_worker=lambda sid: None,
    )
    connection = SimpleNamespace(
        authority=authority,
        actor=SimpleNamespace(profile_id='p', capabilities={'worker:adopt'}, subject='s'),
    )
    params = {
        'profile_id': 'p', 'session_id': 's', 'execution_id': 'e', 'generation': 1,
        'pid': 4321, 'birth': 1.0, 'secret': 'x' * 32,
    }

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr('gateway.session_authorities.require_serving', lambda a: None)
        # adopt goes on to the owner, which we do not exercise here.
        mp.setattr(sw, 'adopt_worker_execution', lambda *a, **k: 'adopted')
        asyncio.run(sw.worker_request(connection, 'ref', params, operation='adopt'))
    assert order == ['claim', 'verify']


# --------------------------------------------------------------------------- #
# 2. An RPC stall is its own cause, not the disk/default copy
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize('exc', [
    TimeoutError('worker_receipt_timeout'),
    'worker_receipt_timeout',
])
def test_receipt_timeout_classifies_to_its_own_cause(exc):
    cause = classify_persistence_error(exc)
    assert cause == 'worker_receipt_timeout'
    assert cause in PERSISTENCE_ERROR_CAUSES


def test_receipt_timeout_renders_the_stall_copy_not_the_disk_copy():
    from agent.turn_explainers import TurnExplainersMixin

    out = TurnExplainersMixin._format_turn_completion_explanation(
        'session_persistence_failed', 'worker_receipt_timeout')
    lower = out.lower()
    assert out.strip(), 'a receipt timeout must explain itself'
    # The whole point: never tell the user to free disk space for an RPC stall.
    for forbidden in ('disk space', 'full disk', 'free some space', 'out of room'):
        assert forbidden not in lower, forbidden
    assert 'timeout' in lower or 'busy' in lower
    assert 'again' in lower, 'the copy must offer a retry'


def test_receipt_timeout_is_bound_in_the_explainer_vocabulary():
    from agent.turn_explainers import _PERSISTENCE_CAUSES

    assert 'worker_receipt_timeout' in _PERSISTENCE_CAUSES


def test_the_default_copy_still_exists_for_a_genuinely_unknown_cause():
    """The unknown cause keeps its own neutral copy; this change adds a cause, not a rewrite."""
    from agent.turn_explainers import TurnExplainersMixin

    out = TurnExplainersMixin._format_turn_completion_explanation(
        'session_persistence_failed', 'unknown')
    lower = out.lower()
    assert out.strip()
    for forbidden in ('disk space', 'full disk'):
        assert forbidden not in lower, forbidden
    assert 'hermes doctor' in lower
