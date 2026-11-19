"""``compact_store`` reclaims the payload that the retention prune cannot reach.

The profiles reached 2-3.5 GiB each while ``sessions.auto_prune`` was ON, because the prune
only removes rows that are OLD ENOUGH to be prunable. The bulk of those stores was RECENT
payload: the ``reasoning_content`` echo of ``reasoning`` (byte-identical rows), and read
receipts holding a full copy of the transcript or the session row. The prune correctly
reclaimed almost nothing, and the file only grew.

``compact_store`` is the pass that removes that payload. It must:

* reclaim the duplicated reasoning echo and the bulky read receipts;
* keep the digest (so a late duplicate still conflicts) and the receipt's replayability;
* VACUUM only when the store is provably quiet -- a rewrite under a live writer is what
  leaves every agent refusing turns (#110054);
* never raise: a compaction that cannot complete leaves the store as it found it.
"""

import json

from hermes_state import SessionDB
from hermes_state_runtime import (
    begin_runtime_epoch, mutate_worker_execution, register_worker_execution,
)
from hermes_state_runtime_workers import RETIRED_RECEIPT

READ = dict(target='s', include_ancestors=False, include_inactive=False,
            repair_alternation=False, include_row_ids=False, include_compacted=False)


def _db(tmp_path):
    return SessionDB(db_path=tmp_path / "state.db")


def _rows(db, sql, params=()):
    return db._read_all(sql, params)


def _add_message(db, session_id, role, content, timestamp, reasoning=None, reasoning_content=None,
                 tool_calls=None):
    """Insert one message row directly.

    The public append path pulls in the context compressor (and, through it, the Hermes
    bootstrap that reads the real home), which a unit test must not touch; the columns
    under test are plain columns either way.
    """
    def _write(conn):
        conn.execute(
            "INSERT INTO messages(session_id, role, content, timestamp, reasoning, reasoning_content, "
            "tool_calls) VALUES(?,?,?,?,?,?,?)",
            (session_id, role, content, timestamp, reasoning, reasoning_content, tool_calls))
    db._execute_write(_write)


def test_compact_deduplicates_byte_identical_reasoning(tmp_path):
    db = _db(tmp_path)
    try:
        db.create_session('s', 'cli')
        # One row whose reasoning_content is a byte-identical echo of reasoning (the legacy
        # duplication), and one whose reasoning_content genuinely differs.
        _add_message(db, 's', 'assistant', 'a', 1.0, 'REASON', 'REASON')
        _add_message(db, 's', 'assistant', 'b', 2.0, 'REASON', 'DIFFERENT')

        report = db.compact_store(idle_days=3650.0, drop_duplicate_reasoning=True, vacuum=False)
        assert report['reasoning_deduped'] == 1, 'only the byte-identical echo is waste'

        dup = _rows(db, "SELECT reasoning, reasoning_content FROM messages WHERE content='a'")
        assert dup[0]['reasoning_content'] is None, 'the duplicate echo must go'
        assert dup[0]['reasoning'] == 'REASON', 'the original reasoning must survive'

        kept = _rows(db, "SELECT reasoning, reasoning_content FROM messages WHERE content='b'")
        assert kept[0]['reasoning_content'] == 'DIFFERENT', 'a differing value must be kept'
    finally:
        db.close()


def test_compact_keeps_reasoning_when_asked_not_to(tmp_path):
    db = _db(tmp_path)
    try:
        db.create_session('s', 'cli')
        db._execute_write(lambda conn: conn.execute(
            "INSERT INTO messages(session_id, role, content, timestamp, reasoning, reasoning_content) "
            "VALUES('s','assistant','a',?,?,?)", (1.0, 'R', 'R')))
        report = db.compact_store(idle_days=3650.0, drop_duplicate_reasoning=False, vacuum=False)
        assert report['reasoning_deduped'] == 0
        assert _rows(db, "SELECT reasoning_content FROM messages")[0]['reasoning_content'] == 'R'
    finally:
        db.close()


def test_compact_shrinks_a_terminal_workers_transcript_receipt(tmp_path):
    """A terminal worker's read receipt carries a full transcript copy; it must shrink to the
    retirement marker while keeping its digest."""
    db = _db(tmp_path)
    try:
        db.create_session('s', 'cli')
        _add_message(db, 's', 'user', 'x' * 4000, 1.0)
        epoch = begin_runtime_epoch(db, instance_id='owner')
        scope = dict(epoch=epoch, execution_id='e1', session_id='s', generation=0)
        register_worker_execution(db, **scope, kind='compute', adoption_secret='secret')
        mutate_worker_execution(db, epoch=epoch, execution_id='e1', session_id='s',
                                generation=0, sequence=1, operation='compression.history',
                                payload=READ)
        # The execution is now terminal (a read receipt on a terminal execution).
        db._execute_write(lambda conn: conn.execute(
            "UPDATE worker_executions SET status='terminal' WHERE execution_id='e1'"))

        big = _rows(db, "SELECT LENGTH(result_json) L FROM worker_receipts")[0]['L']
        assert big > 1000, 'the history receipt should carry the transcript'

        report = db.compact_store(idle_days=3650.0, vacuum=False)
        assert report['receipts_compacted'] >= 1, 'the bulky read receipt must be compacted'

        row = _rows(db, "SELECT result_json, payload_digest FROM worker_receipts")[0]
        assert json.loads(row['result_json']) == RETIRED_RECEIPT
        assert row['payload_digest'], 'the digest must survive so a late duplicate still conflicts'
    finally:
        db.close()


def test_compact_leaves_a_live_workers_receipt_alone(tmp_path):
    """A RUNNING worker still needs its receipt: it is the replay record for its next call."""
    db = _db(tmp_path)
    try:
        db.create_session('s', 'cli')
        _add_message(db, 's', 'user', 'x' * 4000, 1.0)
        epoch = begin_runtime_epoch(db, instance_id='owner')
        scope = dict(epoch=epoch, execution_id='e2', session_id='s', generation=0)
        register_worker_execution(db, **scope, kind='compute', adoption_secret='secret')
        mutate_worker_execution(db, epoch=epoch, execution_id='e2', session_id='s',
                                generation=0, sequence=1, operation='compression.history',
                                payload=READ)

        db.compact_store(idle_days=3650.0, vacuum=False)
        row = _rows(db, "SELECT result_json FROM worker_receipts")[0]
        assert json.loads(row['result_json']) != RETIRED_RECEIPT, (
            'a live worker kept a retired receipt and lost its replay record')
    finally:
        db.close()


def test_compact_keeps_the_reasoning_echo_on_replay(tmp_path):
    """The dedup must be LOSSLESS on replay.

    Consumers read ``reasoning_content`` DIRECTLY -- the Anthropic converter builds its thinking
    block from ``m.get("reasoning_content")``, not from the ``_replayable_reasoning_text``
    fallback. So a compaction that dropped the echo without restoring it on read would silently
    lose the thinking block on every replayed tool-call turn.
    """
    from agent.anthropic_message_convert import _convert_assistant_message

    reason = "The user wants a fix; let me check the parser before editing."
    tool_calls = [{"id": "c1", "type": "function",
                   "function": {"name": "run", "arguments": "{}"}}]

    db = _db(tmp_path)
    try:
        db.create_session('s', 'cli')
        _add_message(db, 's', 'assistant', 'I ran the tests.', 1.0,
                     reasoning=reason, reasoning_content=reason,
                     tool_calls=json.dumps(tool_calls))

        before = _convert_assistant_message({
            "role": "assistant", "content": "I ran the tests.",
            "reasoning": reason, "reasoning_content": reason, "tool_calls": tool_calls})

        db.compact_store(idle_days=3650.0, drop_duplicate_reasoning=True, vacuum=False)

        # The row no longer carries the echo...
        assert _rows(db, "SELECT reasoning_content FROM messages")[0]['reasoning_content'] is None
        # ...but the READ restores it, so the replay is byte-identical.
        replayed = db.get_messages('s')[0]
        assert replayed['reasoning_content'] == reason
        after = _convert_assistant_message(replayed)

        before_types = [b.get('type') for b in before['content']]
        after_types = [b.get('type') for b in after['content']]
        assert before_types == after_types, 'the thinking block changed shape'
        assert 'thinking' in after_types, 'the thinking block was lost'
        before_think = next(b.get('thinking') for b in before['content'] if b.get('type') == 'thinking')
        after_think = next(b.get('thinking') for b in after['content'] if b.get('type') == 'thinking')
        assert after_think == before_think
    finally:
        db.close()


def test_compact_reports_the_bytes_it_freed(tmp_path):
    db = _db(tmp_path)
    try:
        db.create_session('s', 'cli')
        _add_message(db, 's', 'user', 'hello', 1.0)
        report = db.compact_store(idle_days=3650.0, vacuum=False)
        assert 'logical_bytes_before' in report and 'logical_bytes_after' in report
        assert report['freed_bytes'] >= 0
        assert report['pruned'] >= 0
    finally:
        db.close()


def test_compact_never_raises_on_a_busy_store(tmp_path):
    """A compaction that cannot complete must leave the store as it found it."""
    db = _db(tmp_path)
    try:
        db.create_session('s', 'cli')
        _add_message(db, 's', 'user', 'hello', 1.0)
        before = db._read_one('SELECT COUNT(*) FROM messages')[0]
        report = db.compact_store(idle_days=3650.0, vacuum=False)
        after = db._read_one('SELECT COUNT(*) FROM messages')[0]
        assert after <= before
        assert 'error' not in report or report.get('pruned') is not None
    finally:
        db.close()


def test_maybe_auto_compact_throttles_on_its_own_marker(tmp_path):
    """The second call inside the interval must be a no-op, so a restart loop cannot
    rewrite the store over and over."""
    db = _db(tmp_path)
    try:
        db.create_session('s', 'cli')
        _add_message(db, 's', 'user', 'hello', 1.0)
        first = db.maybe_auto_compact(idle_days=3650.0, min_interval_hours=24)
        assert first.get('skipped') is False
        assert db.get_meta('last_auto_compact')
        second = db.maybe_auto_compact(idle_days=3650.0, min_interval_hours=24)
        assert second.get('skipped') is True, 'the throttle must hold'
    finally:
        db.close()
