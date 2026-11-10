import json
import os

import pytest

from agent.runtime_session_store import (
    RuntimeSessionStore,
    WorkerOutboxFull,
    WorkerPersistenceError,
)


def test_outbox_freezes_failed_payload_and_reopens_exclusively(tmp_path):
    scope = {'session_id': 's', 'execution_id': 'w', 'epoch': 1}
    rows = [{'role': 'user', 'content': 'frozen'}]
    def unavailable(method, **params):
        rows[0]['content'] = 'caller changed'
        raise TimeoutError('offline')
    store = RuntimeSessionStore(unavailable, scope, tmp_path / 'private')
    try:
        with pytest.raises(TimeoutError):
            store.append_messages_batch('s', rows)
        disk = json.loads(store.path.read_text())
        assert disk['pending'][0]['payload']['messages'][0]['content'] == 'frozen'
        assert store.journal['pending'] == disk['pending']
        with pytest.raises(WorkerPersistenceError, match='outbox_in_use'):
            RuntimeSessionStore(unavailable, scope, tmp_path / 'private')
        with pytest.raises(WorkerPersistenceError, match='offline'):
            store.queue_token_counts('s', input_tokens=1)
    finally:
        with pytest.raises(WorkerPersistenceError, match='offline'):
            store.close()
    seen = []
    def commit(method, **params):
        seen.append(params)
        return {'count': 1, 'annotations': [{'_row_id': 42}]}
    restored = RuntimeSessionStore(commit, scope, tmp_path / 'private')
    try:
        assert restored.retry_pending() == []  # reopening already reconciled the acknowledged journal
        assert seen[0]['payload']['messages'][0]['content'] == 'frozen'
        assert json.loads(restored.path.read_text())['pending'] == []
    finally:
        restored.close()


@pytest.mark.platforms('posix')
def test_outbox_has_private_posix_mode(tmp_path):
    store = RuntimeSessionStore(lambda *a, **k: {}, {'session_id': 's'}, tmp_path / 'private')
    try:
        assert store.path.stat().st_mode & 0o077 == 0
    finally:
        store.close()


def test_outbox_capacity_failure_does_not_advance_or_discard(tmp_path):
    """A capacity refusal is decided BEFORE any write, so it must not latch the store.

    Latching it is what turned one oversized payload into a lost turn: the latched error then
    failed ``refresh_session_turn_lease`` (which hard-interrupted the agent mid-stream) and
    re-raised out of ``close()`` past a result that had already been produced (#t_8c013181).
    """
    def unavailable(*args, **kwargs):
        raise TimeoutError('offline')
    store = RuntimeSessionStore(unavailable, {'session_id': 's'}, tmp_path / 'private', max_bytes=500)
    try:
        before = store.path.read_bytes()
        with pytest.raises(WorkerOutboxFull, match='outbox_full'):
            store.append_messages_batch('s', [{'role': 'user', 'content': 'x' * 1000}])
        assert store.path.read_bytes() == before
        assert store.failure is None  # ...and nothing latched
    finally:
        store.close()  # no re-raise: a store that refused a payload is not a failed turn


def test_worker_turn_wait_callbacks_remain_local(tmp_path):
    attempts, notices = [], []
    def rpc(method, **params):
        attempts.append(params)
        return {'value': len(attempts) > 1}
    store = RuntimeSessionStore(rpc, {'session_id': 's'}, tmp_path / 'private')
    try:
        assert store.acquire_session_turn_lease('s', 'holder', wait_seconds=3,
            poll_interval_seconds=.01, on_wait=notices.append)
        assert notices and len(attempts) == 2
        assert all(set(row['payload']) == {'holder', 'ttl_seconds'} for row in attempts)
        assert not store.acquire_session_turn_lease('s', 'holder', should_abort=lambda: True)
    finally:
        store.close()


def test_outbox_filesystem_failure_is_sticky(tmp_path, monkeypatch):
    store = RuntimeSessionStore(lambda *a, **k: {}, {'session_id': 's'}, tmp_path / 'private')
    def fail_replace(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr('agent.runtime_session_store.os.replace', fail_replace)
    try:
        with pytest.raises(OSError, match='disk unavailable'):
            store.queue_token_counts('s', input_tokens=1)
        assert store.failure == 'disk unavailable'
    finally:
        store._outbox_owner.close()


def test_outbox_journal_survives_a_cross_device_rename(tmp_path, monkeypatch):
    """The journal publishes through utils.atomic_replace: a bind-mounted outbox whose rename
    fails EXDEV falls back to copy+fsync instead of failing every worker write, and the
    journal stays owner-only (POSIX mode bits; Windows has none to check)."""
    import errno
    import stat
    real_replace = os.replace
    def exdev_replace(src, dst):
        if str(dst).endswith('pending.json'):
            raise OSError(errno.EXDEV, 'cross-device link')
        return real_replace(src, dst)
    monkeypatch.setattr(os, 'replace', exdev_replace)
    store = RuntimeSessionStore(lambda *a, **k: {}, {'session_id': 's'}, tmp_path / 'private')
    try:
        journal = store.path
        assert json.loads(journal.read_text())['scope'] == {'session_id': 's'}
        if os.name == 'posix':
            assert stat.S_IMODE(journal.stat().st_mode) == 0o600
        assert [p.name for p in journal.parent.iterdir() if p.name.startswith('.pending-')] == []
    finally:
        store._outbox_owner.close()


def test_worker_store_persists_and_restores_the_tools_prefix_pin(tmp_path):
    """persist_agent_tool_names writes the pin through the worker facade and a rebuilt worker
    agent reads it back from the same row (F26). Without the facade method the pin stays NULL
    and a config flip between turns silently forks the cached tools[] prefix."""
    from types import SimpleNamespace
    from hermes_state import SessionDB
    from hermes_state_runtime import begin_runtime_epoch, mutate_worker_execution, register_worker_execution
    from tools.mcp_tool_agent import persist_agent_tool_names
    db = SessionDB(tmp_path / 'state.db')
    try:
        db.create_session('owned', 'cli', system_prompt='prefix')
        epoch = begin_runtime_epoch(db, instance_id='fixture')
        scope = dict(epoch=epoch, execution_id='worker', session_id='owned', generation=0)
        register_worker_execution(db, **scope, kind='compute', adoption_secret='test-secret')
        store = RuntimeSessionStore(lambda method, **p: mutate_worker_execution(db, **p), scope, tmp_path / 'outbox')
        try:
            tools = [{'type': 'function', 'function': {'name': n, 'parameters': {}}}
                     for n in ('terminal', 'tool_search', 'tool_describe', 'tool_call')]
            persist_agent_tool_names(SimpleNamespace(_session_db=store, session_id='owned', tools=tools))
            db.create_session('direct', 'cli', system_prompt='prefix')
            persist_agent_tool_names(SimpleNamespace(_session_db=db, session_id='direct', tools=tools))
            # The same pin the direct store writes (the RPC canonicalizes key order for its receipt
            # digest): a worker-pinned session rebuilt in-process reads back version + tools intact.
            assert json.loads(db.get_session('owned')['tool_names']) == json.loads(db.get_session('direct')['tool_names'])
            assert json.loads(store.get_session('owned')['tool_names'])['tools'] == tools
            with pytest.raises(WorkerPersistenceError, match='permission_denied'):
                store.update_session_tool_names('foreign', ['terminal'])
            with pytest.raises(Exception, match='invalid_params'):
                store.update_session_tool_names('owned', [{'not': 'a name'}])
        finally:
            store._outbox_owner.close()
    finally:
        db.close()


def test_owner_refusal_does_not_latch_the_store_and_does_not_lose_the_turn(tmp_path):
    """The failing flush of #t_8c013181, reproduced end to end.

    A proactive tool-result prune ships a ``compression.archive`` receipt the owner refuses with
    ``invalid_params`` (its payload exceeded the receipt ceiling). The refusal is PRE-COMMIT: no
    receipt was written and the owner's ``last_sequence`` is unchanged, so the entry can never land
    as written.

    The refusal used to LATCH the store, and that latch is what destroyed the turn:

      * ``refresh_session_turn_lease`` -- the liveness-critical renewal -- failed, so the lease-loss
        guard hard-interrupted the agent MID-STREAM;
      * ``flush_token_counts`` then re-raised the latched error at close, past a result that had
        already been produced, and the card was booked ``managed_worker_failed``.

    So: the refused write is rolled back and the store stays healthy, the lease renewal still
    works, the terminal flush succeeds, and ``close()`` does not turn a completed turn into a
    failed one.
    """
    def owner(method, **params):
        operation = params['operation']
        if operation == 'compression.archive':
            raise WorkerPersistenceError('invalid_params')  # the owner's pre-commit refusal
        if operation == 'turn.renew':
            return {'value': True}
        return {'count': 1, 'annotations': [{'_row_id': 7}]}

    store = RuntimeSessionStore(owner, {'session_id': 's'}, tmp_path / 'private')
    try:
        with pytest.raises(WorkerPersistenceError, match='invalid_params'):
            store.archive_and_compact('s', [{'role': 'user', 'content': 'pruned'}])
        # The refusal rolled its entry back: nothing is pending and the sequence is reusable.
        assert store.failure is None
        assert store.journal['pending'] == []
        assert store.journal['next_sequence'] == 1
        # The call whose failure used to hard-interrupt the agent mid-stream.
        assert store.refresh_session_turn_lease('s', 'holder') is True
        # The store is usable again: the next write lands.
        assert store.append_messages_batch('s', [{'role': 'user', 'content': 'small'}]) == 1
        # The terminal flush the worker runs before it sends 'result' -- the one that used to
        # re-raise the latched refusal.
        store.flush_token_counts()
    finally:
        store.close()  # a completed turn is not failed by its store's cleanup


def test_a_refused_write_does_not_discard_the_transcript_the_turn_produced(tmp_path):
    """The refusal is scoped to the refused payload: the work already done stays durable.

    A worker that was interrupted mid-stream had already persisted rows. A refusal of a LATER
    write must not roll those back, and must not make the store refuse every later write either.
    """
    def owner(method, **params):
        operation = params['operation']
        if operation == 'compression.archive':
            raise WorkerPersistenceError('invalid_params')
        if operation == 'turn.renew':
            return {'value': True}
        return {'count': len(params['payload'].get('messages', [])), 'annotations': [
            {'_row_id': index + 1} for index, _ in enumerate(params['payload'].get('messages', []))]}

    store = RuntimeSessionStore(owner, {'session_id': 's'}, tmp_path / 'private')
    try:
        assert store.append_messages_batch('s', [{'role': 'user', 'content': 'first'}]) == 1
        with pytest.raises(WorkerPersistenceError, match='invalid_params'):
            store.archive_and_compact('s', [{'role': 'user', 'content': 'pruned'}])
        # The first write is still durable, and the store still accepts new ones.
        assert store.failure is None
        assert store.append_messages_batch('s', [{'role': 'user', 'content': 'second'}]) == 1
        assert store.refresh_session_turn_lease('s', 'holder') is True
    finally:
        store.close()


def test_the_prune_refusal_names_its_cause_on_the_card(tmp_path):
    """The board's error text must name the cause, not only ``managed_worker_failed``.

    The refusal that lost whole turns was a bounded code the owner already speaks
    (``invalid_params``), yet the card said only ``managed_worker_failed`` and the operator could
    not tell a refused payload from a dead interpreter (#t_8c013181).
    """
    from agent.managed_worker import WORKER_FAILURE_CAUSES, _bounded_cause
    from hermes_state_runtime import RuntimeStoreError

    # The worker's own persistence refusal and the owner's, both reach the card by name.
    assert _bounded_cause(WorkerPersistenceError('invalid_params')) == 'invalid_params'
    assert _bounded_cause(RuntimeStoreError('invalid_params')) == 'invalid_params'
    assert 'invalid_params' in WORKER_FAILURE_CAUSES
    # An exception whose text could carry a credential is NOT sent: the card text is
    # operator-facing, so an unrecognised cause is dropped rather than printed.
    assert _bounded_cause(OSError(r'C:\Users\someone\secret.db is locked')) == ''
    assert _bounded_cause(ValueError('api_key=sk-live-1234')) == ''
    assert _bounded_cause(RuntimeError()) == ''


def test_the_default_journal_ceiling_carries_a_compaction_commit(tmp_path):
    """A compaction commit ships the transcript TWICE, so its receipt is far larger than a message.

    The refused writes measured on this host ran 4.05MiB to 11.02MiB (49 refusals, every one of
    them a ``compression.archive`` whose ``held_for_cas`` projection shipped the full content and
    reasoning of every row). A 4MiB ceiling refused all of them, and the refusal is what latched
    the store and lost the turn (#t_8c013181). The ceiling must carry the real payload.
    """
    from agent.runtime_session_store import DEFAULT_OUTBOX_MAX_BYTES

    assert DEFAULT_OUTBOX_MAX_BYTES >= 16 * 1024 * 1024

    sent = []

    def commit(method, **params):
        rows = params['payload']['messages']
        sent.append(rows)
        return {'count': len(rows), 'annotations': [{'_row_id': 1} for _ in rows]}

    # A payload of the measured worst case (11.02MiB) must be accepted by the default ceiling.
    payload = [{'role': 'tool', 'content': 'x' * 60_000} for _ in range(190)]
    store = RuntimeSessionStore(commit, {'session_id': 's'}, tmp_path / 'private')
    try:
        store.append_messages_batch('s', payload)
        assert sent and len(sent[0]) == len(payload)
    finally:
        store.close()


def test_the_cas_projection_ships_only_what_the_fence_reads(tmp_path):
    """The refusal was a SIZE refusal, and ``held_for_cas`` was most of the size.

    The measured refused writes were ``compression.archive`` receipts of 4.05MiB to 11.02MiB whose
    row count was 72 to 470 -- far under the owner's 1000-row limit, so the payload SIZE was the
    trigger. ``held_for_cas`` is the projection the owner's CAS fence reads, and the fence compares
    a row by its stored snapshot (``_db_row_snapshot``) or, failing that, by its identity
    (``role``, ``content``, ``tool_call_id``, ``tool_calls``). It never reads the reasoning
    sidecars, so shipping them was pure size: the same projection cut one measured 11.02MiB
    receipt to 5.51MiB (#t_8c013181).
    """
    held = [{
        'role': 'assistant',
        'content': 'c' * 50_000,
        'reasoning': 'r' * 200_000,
        'reasoning_content': 'rc' * 100_000,
        'reasoning_details': [{'type': 'reasoning.text', 'text': 'd' * 100_000}],
        'codex_reasoning_items': [{'x': 'i' * 100_000}],
        '_row_id': 1,
        '_db_persisted': True,
        '_db_row_snapshot': 'snap-1',
        'message_uid': 'uid-1',
    }]
    compacted = [{'role': 'assistant', 'content': 'pruned', '_row_id': 1}]
    sent = []

    def commit(method, **params):
        sent.append(params['payload'])
        return {'value': 1, 'annotations': [{'_row_id': 1}]}

    store = RuntimeSessionStore(commit, {'session_id': 's'}, tmp_path / 'private')
    try:
        store.archive_and_compact('s', compacted, held_for_cas=held)
    finally:
        store.close()
    cas = sent[0]['held_for_cas'][0]
    # The fields the CAS compare reads are present...
    assert cas['_row_id'] == 1 and cas['_db_persisted'] is True
    assert cas['_db_row_snapshot'] == 'snap-1' and cas['message_uid'] == 'uid-1'
    assert cas['role'] == 'assistant'
    # ...and the fields it never reads (the reasoning sidecars) are NOT shipped.
    for absent in ('reasoning', 'reasoning_content', 'reasoning_details', 'codex_reasoning_items'):
        assert absent not in cas, f'{absent} is not read by the fence, so it is pure payload size'


def test_a_row_without_a_snapshot_still_carries_what_the_fence_falls_back_to(tmp_path):
    """Without a snapshot the fence compares identity fields, so those must still be shipped."""
    held = [{
        'role': 'tool', 'content': 'the result', 'tool_call_id': 'call-1',
        'tool_calls': [{'id': 'call-1', 'type': 'function', 'function': {'name': 'terminal'}}],
        'reasoning': 'r' * 100_000,
        '_row_id': 7, '_db_persisted': True, 'message_uid': 'uid-7',
    }]
    sent = []

    def commit(method, **params):
        sent.append(params['payload'])
        return {'value': 1, 'annotations': [{'_row_id': 7}]}

    store = RuntimeSessionStore(commit, {'session_id': 's'}, tmp_path / 'private')
    try:
        store.archive_and_compact('s', [{'role': 'tool', 'content': 'x', '_row_id': 7}], held_for_cas=held)
    finally:
        store.close()
    cas = sent[0]['held_for_cas'][0]
    assert cas['content'] == 'the result' and cas['tool_call_id'] == 'call-1'
    assert cas['tool_calls'] and cas['role'] == 'tool'
    assert 'reasoning' not in cas
