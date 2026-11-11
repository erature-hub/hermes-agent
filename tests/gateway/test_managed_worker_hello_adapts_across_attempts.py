"""The real ``execute_managed`` must widen its handshake deadline after a timeout.

Regression for the 2026-11-11 hot retry loop (#t_45f5d3c4). The managed worker's
handshake deadline is DERIVED from the spawn cost this host actually pays
(#t_853294b3) — but the cost was recorded only after a SUCCESSFUL handshake, so a
profile whose every handshake timed out recorded nothing and the deadline stayed
pinned at its 60s floor forever.

Measured on the live board (task ``t_4b1a82f0``, 16 consecutive attempts): the
admission -> worker-lost handshake wait was flat at 60-62s for every attempt.

This test drives the REAL ``execute_managed`` (the spawn is stubbed, the handshake is
not) and asserts the deadline the SECOND attempt is given is wider than the one the
first attempt exhausted. Before the fix the second attempt got the same 60s.
"""

from __future__ import annotations

import asyncio

import pytest

from gateway import session_managed_worker as smw
from hermes_state_runtime import RuntimeStoreError


class _TimeoutWorker:
    """A managed worker whose handshake never arrives; it records the deadline it was given."""

    def __init__(self):
        self.deadlines: list[float] = []
        self.stop = asyncio.Event()
        self.closed = False
        self.worker = None

    async def next_frame(self, timeout, ack):
        self.deadlines.append(timeout)
        raise RuntimeStoreError(smw.HELLO_TIMEOUT_BOOKING)

    def close(self):
        self.closed = True


class _Authority:
    """Enough of the authority for the handshake path and the cost bookkeeping."""

    def __init__(self):
        self._managed_hello_costs = None
        self._managed_workers = {}
        self._managed_spawning = {}
        self.sessions = {}
        self.executing = {}
        self.pending_results = {}
        self.adopted = []

    def adopt_agent(self, session_id, generation, worker):
        self.adopted.append((session_id, generation))


class _Ref:
    session_id = "sess-1"


class _Row(dict):
    """The admitted row: a mapping, because the runtime reads it like one."""

    def __init__(self):
        super().__init__(payload={"text": "hello"}, admission_id="adm-1",
                         generation=1, principal_id="p-1")


class _Policy:
    kanban_json = None
    workspace = "."
    workspace_ready = True


@pytest.mark.asyncio
async def test_the_second_attempt_waits_longer_than_the_one_that_timed_out(monkeypatch):
    """The deadline adapts across attempts: attempt 2 waits longer than attempt 1."""
    authority = _Authority()
    ref, row = _Ref(), _Row()

    workers: list[_TimeoutWorker] = []

    async def _fake_spawn(_authority, _ref, _row):
        worker = _TimeoutWorker()
        workers.append(worker)
        return worker, object()

    async def _fake_text(*_args, **_kwargs):
        return "hello", None

    monkeypatch.setattr(smw, "_spawn_registered_worker", _fake_spawn)
    monkeypatch.setattr(smw, "_mark_spawning", lambda *_a, **_k: None)
    # ``managed_turn_text`` is imported INSIDE execute_managed, so patch its home module.
    import gateway.session_context_refs as refs
    monkeypatch.setattr(refs, "managed_turn_text", _fake_text)
    monkeypatch.setattr(smw, "_stopped_before_spawn", lambda *_a, **_k: False)

    # Two consecutive attempts against the same authority.
    for _ in range(2):
        with pytest.raises(RuntimeStoreError):
            await smw.execute_managed(authority, ref, row, _Policy())

    assert len(workers) == 2, "both attempts must have reached the handshake"
    first, second = workers[0].deadlines[0], workers[1].deadlines[0]

    assert first == float(smw.HELLO_SECONDS), \
        "the first attempt has no observation yet, so it gets the floor"
    assert second > first, (
        f"the second attempt must wait longer than the {first}s that just expired; "
        f"it waited {second}s — the host that is too slow to boot a worker is too slow "
        f"to boot the next one too, and the deadline has to follow"
    )
    assert smw._hello_deadline_seconds(authority) >= second, \
        "the derivation must reflect what the timeout cost"
