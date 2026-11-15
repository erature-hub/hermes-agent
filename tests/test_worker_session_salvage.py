"""A worker survives the loss of its owner: the rows are salvaged, the card is not charged.

Two failures met on the same card and made it retry forever. The worker's outbox latched the
owner's refusal and ``close()`` re-raised it, so a turn whose result had ALREADY been delivered
exited 1 and booked a ``managed_worker_failed`` crash. The dispatcher then read the attempt's
receipt as ``unknown`` -- the owner's epoch had moved under the started turn -- and gave up on
it, so the crash bought nothing and the next attempt met the same wall.

The salvage contract spans two modules, so it is proved here rather than beside either one:
``agent/runtime_session_store.py`` keeps the rows and closes cleanly, and
``hermes_cli/kanban_worker_client.py`` re-admits the same claim before it calls the attempt
lost.
"""

import asyncio
from contextlib import asynccontextmanager
import json

import pytest

from agent.runtime_session_store import RuntimeSessionStore, WorkerPersistenceError
from hermes_cli import gateway_client, kanban_worker_client


# --------------------------------------------------------------------------------------
# The shutdown latch: a lost owner is not a lost turn.
# --------------------------------------------------------------------------------------


def test_close_salvages_the_rows_a_lost_owner_never_took(tmp_path):
    """The journal holds the rows, the salvage names them, and the close is clean.

    The owner refused the persist, so the turn's transcript never landed. It is journalled
    before it is offered, so nothing is lost: the next execution that adopts this outbox
    replays it. Re-raising that refusal at close turned delivered work into a crash.
    """
    def owner_gone(method, **params):
        raise WorkerPersistenceError('owner_unavailable')

    store = RuntimeSessionStore(owner_gone, {'session_id': 's'}, tmp_path / 'private')
    with pytest.raises(WorkerPersistenceError, match='owner_unavailable'):
        store.append_messages_batch('s', [{'role': 'user', 'content': 'the work'}])
    assert store.failure == 'owner_unavailable'

    store.close()  # the clean close: no re-raise of a handshake fault

    assert store.salvaged_rows == 1
    salvaged = [json.loads(line) for line in store.salvage_path.read_text(encoding='utf-8').splitlines()]
    assert salvaged[-1]['message'] == {'role': 'user', 'content': 'the work'}
    assert salvaged[-1]['session_id'] == 's'
    # The journal still holds the entry: the salvage is the second copy, not the only one.
    assert json.loads(store.path.read_text(encoding='utf-8'))['pending']


def test_close_still_raises_a_fault_that_is_not_the_handshake(tmp_path):
    """A disk that refused the write is not salvageable: the close still fails."""
    def disk_gone(method, **params):
        raise OSError('disk unavailable')

    store = RuntimeSessionStore(disk_gone, {'session_id': 's'}, tmp_path / 'private')
    with pytest.raises(OSError, match='disk unavailable'):
        store.append_messages_batch('s', [{'role': 'user', 'content': 'the work'}])

    with pytest.raises(WorkerPersistenceError, match='disk unavailable'):
        store.close()
    assert store.salvaged_rows == 0


def test_a_recovered_owner_leaves_no_salvage_behind(tmp_path):
    """A handshake that succeeds is not a salvage: the rows are delivered, nothing is copied."""
    def owner_alive(method, **params):
        return {'count': 1, 'annotations': [{}]}

    store = RuntimeSessionStore(owner_alive, {'session_id': 's'}, tmp_path / 'private')
    try:
        store.append_messages_batch('s', [{'role': 'user', 'content': 'the work'}])
        store.close()
        assert store.salvaged_rows == 0
        assert not store.salvage_path.exists()
    finally:
        store._outbox_owner.close()


# --------------------------------------------------------------------------------------
# The re-admission handshake: an ``unknown`` receipt is not a lost attempt.
# --------------------------------------------------------------------------------------


class _Owner:
    """A gateway client whose first answer is the owner's epoch moving under the turn."""

    def __init__(self, calls, *, recovers=False):
        self.events = asyncio.Queue()
        self.calls = calls
        self._recovers = recovers

    async def rpc(self, method, **params):
        self.calls.append((method, params))
        if method == 'kanban.run':
            # The first admission is answered ``unknown``; a re-admission admits the attempt
            # again, and the owner that came back answers with the settled receipt.
            admissions = sum(1 for name, _ in self.calls if name == 'kanban.run')
            if admissions == 1 or not self._recovers:
                status = 'unknown'
            else:
                status = 'terminal'
            return {'session_id': 's1',
                    'receipt': {'status': status, 'admission_id': 'a1',
                                'result': {'exit_code': 0} if status == 'terminal' else {}}}
        if method == 'prompt.receipt':
            return {'status': 'unknown', 'admission_id': 'a1'}
        raise AssertionError(f'unexpected rpc {method}')


def _gateway(monkeypatch, owner):
    @asynccontextmanager
    async def connect():
        yield owner

    monkeypatch.setattr(gateway_client, "connect_gateway", connect)
    monkeypatch.setattr(kanban_worker_client.asyncio, "sleep", _no_sleep)
    monkeypatch.delenv("HERMES_TUI_GATEWAY_URL", raising=False)


async def _no_sleep(_seconds):
    return None


_PARAMS = {'board': 'b1', 'task_id': 't1', 'run_id': 42, 'claim_lock': 'lock-1'}


@pytest.mark.asyncio
async def test_an_unknown_receipt_is_readmitted_before_the_attempt_is_lost(monkeypatch, tmp_path):
    """The owner that came back answers the same claim, so the worker ends on the real exit code.

    Before the handshake, an ``unknown`` receipt ended the worker with exit code 1: the card
    booked a crash for work the owner had done, and the next attempt met the same wall.
    """
    calls = []
    _gateway(monkeypatch, _Owner(calls, recovers=True))
    from gateway import session_kanban
    monkeypatch.setattr(session_kanban, "worker_result", lambda *_: {'exit_code': 0, 'last_output': 'card closed'})

    assert await kanban_worker_client.run(dict(_PARAMS), str(tmp_path / 'kanban.db')) == 0

    admissions = [params for name, params in calls if name == 'kanban.run']
    assert len(admissions) == 2  # the admission, then ONE re-admission
    # The re-admission is the SAME attempt: its run_id, board and claim ride along.
    assert admissions[1]['run_id'] == 42
    assert admissions[1]['board'] == 'b1'
    assert admissions[1]['claim_lock'] == 'lock-1'


@pytest.mark.asyncio
async def test_the_readmission_is_bounded_carries_the_claim_and_then_the_attempt_is_lost(monkeypatch, tmp_path):
    """An owner that keeps answering ``unknown`` ends the attempt, and it ends quickly.

    The bound is the point: a worker that re-admits forever keeps a dead card's worker alive
    and its failure budget spent. Each re-admission is the card's OWN claim -- same run, same
    board, same claim lock -- so it is that attempt and not a new one.
    """
    calls = []
    _gateway(monkeypatch, _Owner(calls))

    with pytest.raises(gateway_client.GatewayClientError, match='Kanban execution unknown'):
        await kanban_worker_client.run(dict(_PARAMS), str(tmp_path / 'kanban.db'))

    admissions = [params for name, params in calls if name == 'kanban.run']
    assert len(admissions) == 1 + kanban_worker_client.READMISSION_ATTEMPTS
    for admission in admissions:
        assert admission['run_id'] == 42
        assert admission['board'] == 'b1'
        assert admission['task_id'] == 't1'
        assert admission['claim_lock'] == 'lock-1'
