"""A managed worker's cleanup path must not turn a completed turn into a lost one (#t_8c013181).

The failure this card is about: a store write the owner refused with ``invalid_params`` LATCHED
the store. The latch then failed ``refresh_session_turn_lease`` (the lease-loss guard
hard-interrupted the agent mid-stream) and ``flush_token_counts`` re-raised the latched error at
close -- past a result that had already been produced. The worker exited non-zero and the card was
booked ``managed_worker_failed`` even though the turn's work was done.

These tests pin the cleanup path itself: a store that refuses, or fails to flush, must not
replace the outcome of a turn that completed.
"""
import threading
from types import SimpleNamespace

import pytest

from tests.gateway.test_managed_turn_contract import assignment


def _worker_harness(monkeypatch, tmp_path, store):
    """The pieces ``agent.managed_worker.execute()`` needs, with the turn's work done."""
    from agent import managed_worker as worker

    class Agent:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.session_prompt_tokens = 10
            self.session_completion_tokens = 2
        def run_conversation(self, text, **kwargs):
            self.session_prompt_tokens += 1
            self.session_completion_tokens += 1
            return {'final_response': 'the work is done', 'completed': True, 'partial': False,
                    'turn_exit_reason': 'stop', 'prompt_tokens': 11, 'completion_tokens': 3,
                    'estimated_cost_usd': .01, 'model': 'fixture', 'provider': 'custom',
                    'messages': [{'role': 'assistant', 'content': 'done'}]}

    class Controls:
        def __init__(self, *args):
            self.stopped = threading.Event()
            self.finish = threading.Event()
            self.finish.set()
        def approval(self, data): pytest.fail('unexpected approval')
        def clarify(self, questions): pytest.fail('unexpected question')

    monkeypatch.setattr(worker, 'bind_worker_policy', lambda frame: None)
    monkeypatch.setattr(worker, 'discover_profile_mcp', lambda policy: None)
    monkeypatch.setattr(worker, 'retire_agent', lambda agent: None)
    monkeypatch.setattr(worker, 'WorkerControls', Controls)
    monkeypatch.setattr('agent.runtime_session_store.WorkerRPC', lambda home: lambda *a, **k: {'owner_epoch': 1})
    monkeypatch.setattr('agent.runtime_session_store.RuntimeSessionStore', store)
    monkeypatch.setattr('tools.process_registry.process_registry.recover_from_checkpoint', lambda: None)
    monkeypatch.setattr('run_agent.AIAgent', Agent)
    monkeypatch.setattr('agent.title_generator.wait_for_title_upgrades', lambda: None)
    return worker


def _frame(tmp_path, monkeypatch):
    """A real bootstrapped frame, so the policy the owner would freeze is the real one."""
    frame, _ = assignment(tmp_path, monkeypatch)
    return frame


def test_a_store_that_cannot_flush_does_not_fail_a_completed_turn(tmp_path, monkeypatch):
    """The flush and the close run AFTER the result is produced; neither may undo it."""
    from agent.runtime_session_store import WorkerPersistenceError

    class Store:
        def __init__(self, *args): pass
        def get_messages_as_conversation(self, sid): return []
        def flush_token_counts(self):
            raise WorkerPersistenceError('invalid_params')
        def retry_pending(self):
            return []
        @property
        def journal(self): return {'pending': []}
        def finish(self): pass
        def close(self):
            raise WorkerPersistenceError('invalid_params')

    worker = _worker_harness(monkeypatch, tmp_path, Store)
    frames = []
    worker.execute(_frame(tmp_path, monkeypatch),
                   SimpleNamespace(send=lambda kind, **payload: frames.append({'type': kind, **payload})))
    # The turn's result WAS delivered: a cleanup-path exception did not swallow it.
    result = next(f for f in frames if f['type'] == 'result')
    assert result['result']['final_response'] == 'the work is done'
    # ...and the worker reached 'finished', so the owner settles it as a completed turn.
    assert any(f['type'] == 'finished' for f in frames)
    # No error frame: the turn did not fail.
    assert not [f for f in frames if f['type'] == 'error']


def test_a_store_that_cannot_finish_does_not_fail_a_completed_turn(tmp_path, monkeypatch):
    """``execution.finish`` failing after the result was delivered cannot un-deliver it."""
    from agent.runtime_session_store import WorkerPersistenceError

    class Store:
        def __init__(self, *args): pass
        def get_messages_as_conversation(self, sid): return []
        def flush_token_counts(self): pass
        def finish(self):
            raise WorkerPersistenceError('invalid_params')
        def close(self): pass

    worker = _worker_harness(monkeypatch, tmp_path, Store)
    frames = []
    worker.execute(_frame(tmp_path, monkeypatch),
                   SimpleNamespace(send=lambda kind, **payload: frames.append({'type': kind, **payload})))
    assert next(f for f in frames if f['type'] == 'result')['result']['final_response'] == 'the work is done'
    assert any(f['type'] == 'finished' for f in frames)
    assert not [f for f in frames if f['type'] == 'error']


def test_the_worker_reports_the_bounded_cause_of_its_failure():
    """The board must be able to say WHICH refusal killed the turn (#t_8c013181).

    The worker sends the cause in a closed vocabulary, so the card text stays operator-safe: a
    refusal of the owner's payload is named, an exception whose text could carry a credential is
    not.
    """
    from agent.managed_worker import WORKER_FAILURE_CAUSES, _bounded_cause
    from agent.runtime_session_store import WorkerPersistenceError

    assert _bounded_cause(WorkerPersistenceError('invalid_params')) == 'invalid_params'
    assert _bounded_cause(WorkerPersistenceError('outbox_full')) == 'outbox_full'
    assert _bounded_cause(OSError(r'C:\Users\someone\state.db is locked')) == ''
    assert _bounded_cause(ValueError('api_key=sk-live-abcdef')) == ''
    assert 'invalid_params' in WORKER_FAILURE_CAUSES
