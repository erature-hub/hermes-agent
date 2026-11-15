"""Tests for the dispatcher's end of the JEV pacer wiring.

These pin the behaviour the pacer wiring exists for:

* a tick is OBSERVED by the pacer, with the counts the pacer reasons about;
* the pacer's advice is applied to the NEXT tick, not the current one;
* the pacer can only NARROW -- it can never make the dispatcher spawn more
  than it would have;
* the pacer's class (f) receipt travels out with the tick hook payload as
  ENRICHMENT, and never as a second alarm;
* a pacer that is absent, slow, or wrong changes nothing.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_pacer as kp
from hermes_cli import kanban_pacer_advice as kpa


class _FakePacerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence the test output
        pass

    def do_POST(self):  # BaseHTTPRequestHandler's name
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        try:
            self.server.requests.append(json.loads(raw.decode("utf-8")))
        except ValueError:
            self.server.requests.append({})
        body = json.dumps(self.server.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(autouse=True)
def _isolated_worker_proc_registry(monkeypatch):
    """Keep the dispatcher's process registry out of other tests' way.

    ``_default_spawn`` registers the child it started in the module-global
    ``_live_worker_procs``, and ``reap_worker_zombies`` polls every entry.
    A test that starts a worker through a fake ``Popen`` (no ``poll()``
    method) leaves that fake in the global registry, and the NEXT test that
    runs a dispatch tick trips over it in ``reap_worker_zombies``. That leak
    is pre-existing in this repo and it is order-dependent -- it only shows
    up when a dispatch test runs after a worker-spawn test.

    This fixture gives each test in this file its own registry, so the file
    passes whatever order the suite runs it in, and so it does not inherit
    a fake left behind by a test in another file.
    """
    monkeypatch.setattr(kbd, "_live_worker_procs", {})
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})


@pytest.fixture
def fake_pacer():
    server = HTTPServer(("127.0.0.1", 0), _FakePacerHandler)
    server.requests = []
    server.payload = {
        "decision": {
            "action": "paced_drain",
            "stalled": True,
            "max_spawns_per_tick": 1,
            "drain_order": [],
            "deferred_order": [],
        },
        "receipt": {
            "class_id": "f",
            "class_name": "dispatcher_stalled_queue_full",
            "dedupe_key": "f:dispatcher_stalled_queue_full:1000.000",
            "should_notify": True,
            "suppressed": False,
            "evidence": "ready=3",
            "operator_verify": ["hermes kanban list --status ready"],
        },
        "advisory": None,
    }
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _url(server) -> str:
    host, port = server.server_address[0], server.server_address[1]
    return f"http://{host}:{port}/api/operations/micro-decisions/pacing"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def advice_dir(tmp_path, monkeypatch):
    """An isolated advice store, so a test never reads a real one."""
    monkeypatch.setenv(kpa.ADVICE_DIR_ENV, str(tmp_path / "pacer-state"))
    return tmp_path / "pacer-state"


@pytest.fixture
def captured_ticks(monkeypatch):
    """Capture the tick hook payload."""
    from hermes_cli.plugins import get_plugin_manager

    mgr = get_plugin_manager()
    events: list[dict] = []
    saved = {k: list(v) for k, v in mgr._hooks.items()}
    mgr._hooks.setdefault("on_kanban_dispatch_tick", []).append(
        lambda **kw: events.append(kw)
    )
    try:
        yield events
    finally:
        mgr._hooks = saved


# --- observation --------------------------------------------------------------


def test_a_tick_is_observed_by_the_pacer(kanban_home, all_assignees_spawnable, fake_pacer, advice_dir, monkeypatch):
    """The pacer is told what the tick actually did."""
    monkeypatch.setenv(kp.PACER_URL_ENV, _url(fake_pacer))
    conn = kbc.connect()
    try:
        kb.create_task(conn, title="t", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
    finally:
        conn.close()
    assert fake_pacer.requests, "the pacer was never asked about the tick"
    observed = fake_pacer.requests[-1]
    assert observed["tick_index"] >= 0
    assert observed["spawned_count"] == 1, "the pacer must see what the tick spawned"
    assert observed["quota_state"] == "unknown", (
        "with no quota gate on this checkout the state is unknown, never 'ok'"
    )
    assert isinstance(observed["drain_candidates"], list)


def test_a_dry_run_tick_is_not_observed_as_a_spawn(kanban_home, all_assignees_spawnable, fake_pacer, advice_dir, monkeypatch):
    """A dry run spawns nothing, so it must not be reported as a stall."""
    monkeypatch.setenv(kp.PACER_URL_ENV, _url(fake_pacer))
    conn = kbc.connect()
    try:
        kb.create_task(conn, title="t", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242, dry_run=True)
    finally:
        conn.close()
    assert fake_pacer.requests
    observed = fake_pacer.requests[-1]
    assert observed["spawned_count"] == 1, (
        "a dry run reports what it WOULD have spawned; it must not read as a "
        "stall caused by the dispatcher doing nothing"
    )


def test_an_unreachable_pacer_does_not_break_the_tick(kanban_home, all_assignees_spawnable, monkeypatch):
    """The pacer is an advisor: its absence must not fail the dispatch."""
    monkeypatch.setenv(kp.PACER_URL_ENV, "http://127.0.0.1:1/api/operations/micro-decisions/pacing")
    conn = kbc.connect()
    try:
        kb.create_task(conn, title="t", assignee="alice")
        result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
    finally:
        conn.close()
    assert len(result.spawned) == 1, "the tick must complete normally without a pacer"


# --- the receipt travels with the tick ---------------------------------------


def test_the_receipt_rides_out_with_the_tick_hook(kanban_home, fake_pacer, advice_dir, captured_ticks, monkeypatch):
    """The class (f) receipt reaches the bus with the tick it describes."""
    monkeypatch.setenv(kp.PACER_URL_ENV, _url(fake_pacer))
    conn = kbc.connect()
    try:
        kb.create_task(conn, title="t", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
    finally:
        conn.close()
    assert captured_ticks, "the tick hook did not fire"
    receipt = captured_ticks[-1].get("pacing_receipt")
    assert receipt is not None, "the pacer's receipt did not reach the bus"
    assert receipt["class_name"] == "dispatcher_stalled_queue_full"
    assert receipt["dedupe_key"] == "f:dispatcher_stalled_queue_full:1000.000"
    assert receipt["operator_verify"] == ["hermes kanban list --status ready"]


def test_no_pacer_means_no_receipt_on_the_hook(kanban_home, captured_ticks, monkeypatch):
    """With no pacer, the hook carries no receipt and still fires."""
    monkeypatch.setenv(kp.PACER_URL_ENV, "http://127.0.0.1:1/api/operations/micro-decisions/pacing")
    conn = kbc.connect()
    try:
        kb.create_task(conn, title="t", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
    finally:
        conn.close()
    assert captured_ticks
    assert captured_ticks[-1].get("pacing_receipt") is None


# --- the advice applies to the NEXT tick --------------------------------------


def test_the_pacer_can_hold_the_next_tick(kanban_home, all_assignees_spawnable, fake_pacer, advice_dir, monkeypatch):
    """A held decision means the NEXT tick spawns nothing."""
    fake_pacer.payload = {
        "decision": {
            "action": "held",
            "stalled": True,
            "max_spawns_per_tick": 0,
            "drain_order": [],
            "deferred_order": [],
        },
        "receipt": {
            "class_id": "f",
            "class_name": "dispatcher_stalled_queue_full",
            "dedupe_key": "f:dispatcher_stalled_queue_full:1000.000",
            "should_notify": True,
            "suppressed": False,
        },
        "advisory": None,
    }
    monkeypatch.setenv(kp.PACER_URL_ENV, _url(fake_pacer))
    conn = kbc.connect()
    try:
        kb.create_task(conn, title="t", assignee="alice")
        # Tick 1: the pacer is consulted and says "hold".
        first = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
        assert len(first.spawned) == 1, "the first tick is not held by advice it has not seen"
        # Fresh work, so tick 2 has something it COULD spawn: the hold must be
        # what stops it, not an empty queue.
        kb.create_task(conn, title="t2", assignee="alice")
        # Tick 2: the advice from tick 1 applies.
        second = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
    finally:
        conn.close()
    assert second.spawned == [], "the held decision did not reach the next tick"


def test_the_pacer_narrows_but_never_widens_the_spawn_budget(kanban_home, all_assignees_spawnable, fake_pacer, advice_dir, monkeypatch):
    """A pacer asking for more spawns than allowed is clamped to the limit."""
    fake_pacer.payload = {
        "decision": {
            "action": "paced_drain",
            "stalled": True,
            "max_spawns_per_tick": 99,
            "drain_order": [],
            "deferred_order": [],
        },
        "receipt": None,
        "advisory": None,
    }
    monkeypatch.setenv(kp.PACER_URL_ENV, _url(fake_pacer))
    conn = kbc.connect()
    try:
        for _ in range(5):
            kb.create_task(conn, title="t", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242, max_spawn=2)
        running = kbd.count_running_tasks(conn)
        for _ in range(5):
            kb.create_task(conn, title="t2", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242, max_spawn=2)
        running_after = kbd.count_running_tasks(conn)
    finally:
        conn.close()
    assert running <= 2, (
        f"the pacer widened the dispatcher's own spawn cap: {running} running "
        "against a cap of 2"
    )
    assert running_after <= 2, (
        f"the pacer widened the dispatcher's own spawn cap on the next tick: "
        f"{running_after} running against a cap of 2"
    )


def test_a_healthy_fleet_is_not_paced(kanban_home, all_assignees_spawnable, fake_pacer, advice_dir, monkeypatch):
    """With no intervention required, the dispatcher behaves exactly as before."""
    fake_pacer.payload = {
        "decision": {
            "action": "none",
            "stalled": False,
            "max_spawns_per_tick": 2,
            "drain_order": [],
            "deferred_order": [],
        },
        "receipt": None,
        "advisory": None,
    }
    monkeypatch.setenv(kp.PACER_URL_ENV, _url(fake_pacer))
    conn = kbc.connect()
    try:
        for _ in range(3):
            kb.create_task(conn, title="t", assignee="alice")
        first = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
        # Fresh work for tick 2, so a hold would be visible as a shortfall.
        for _ in range(3):
            kb.create_task(conn, title="t2", assignee="alice")
        second = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
    finally:
        conn.close()
    assert first.spawned, "the first tick spawned nothing at all"
    assert len(second.spawned) == len(first.spawned), (
        "a pacer that asks for no intervention must not slow the dispatcher "
        f"down: tick 1 spawned {len(first.spawned)}, tick 2 spawned {len(second.spawned)}"
    )


def test_stale_advice_does_not_bind_a_later_tick(kanban_home, all_assignees_spawnable, fake_pacer, advice_dir, monkeypatch):
    """An expired advisory is ignored rather than holding the board forever."""
    fake_pacer.payload = {
        "decision": {
            "action": "held",
            "stalled": True,
            "max_spawns_per_tick": 0,
            "drain_order": [],
            "deferred_order": [],
        },
        "receipt": None,
        "advisory": None,
    }
    monkeypatch.setenv(kp.PACER_URL_ENV, _url(fake_pacer))
    conn = kbc.connect()
    try:
        kb.create_task(conn, title="t", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
        # Expire the recorded advice as if it had aged out.
        path = kpa._advice_path(None)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["expires_at"] = 1.0
        path.write_text(json.dumps(payload), encoding="utf-8")
        # Fresh work, so an expired hold is visible as a resumed spawn.
        kb.create_task(conn, title="t2", assignee="alice")
        second = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
    finally:
        conn.close()
    assert len(second.spawned) == 1, "expired advice still held the tick"


class _HangingPacerHandler(BaseHTTPRequestHandler):
    """A pacer that accepts the request and never answers."""

    def log_message(self, *args):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            self.rfile.read(length)
        except OSError:
            pass
        import time as _time

        _time.sleep(10.0)          # never answers in time


def test_a_slow_pacer_does_not_stall_the_tick(kanban_home, all_assignees_spawnable, monkeypatch):
    """A pacer that never answers must not hold the dispatcher open.

    The request carries a timeout, so a hung pacer costs the tick a bounded
    wait and nothing else: the work still spawns and the tick returns.
    """
    import time as _time

    server = HTTPServer(("127.0.0.1", 0), _HangingPacerHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setenv(
            kp.PACER_URL_ENV,
            f"http://127.0.0.1:{server.server_address[1]}/api/operations/micro-decisions/pacing",
        )
        monkeypatch.setenv(kp.PACER_TIMEOUT_ENV, "0.5")

        conn = kbc.connect()
        try:
            kb.create_task(conn, title="t", assignee="alice")
            started = _time.monotonic()
            result = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
            elapsed = _time.monotonic() - started
        finally:
            conn.close()
    finally:
        server.shutdown()
        server.server_close()

    assert result.spawned, "the tick did not spawn while the pacer hung"
    assert elapsed < 5.0, (
        f"the tick waited {elapsed:.1f}s on a hung pacer; the request is not bounded"
    )


def test_the_pacer_is_consulted_outside_the_dispatch_lock(kanban_home, all_assignees_spawnable, monkeypatch):
    """A sibling dispatcher must not be locked out while the pacer is called.

    The pacer is an observer: its round trip happens strictly OUTSIDE the
    single-writer critical section. If it were inside, one slow pacer would
    lock every other dispatcher on that board out for the length of the
    round trip.

    The check is made at the moment the pacer is consulted: if the board's
    writer lock can be taken then, the dispatcher was not holding it.
    """
    db_path = kb.kanban_db_path(board=None)
    lock_free_at_pacer_time: list[bool] = []

    def _observe(conn, result, **kwargs):
        acquired = False
        with kbc._dispatch_tick_lock(db_path) as held:
            acquired = bool(held)
        lock_free_at_pacer_time.append(acquired)

    # Patch the bridge THROUGH the dispatcher's own reference. A test elsewhere
    # in the suite can reload ``hermes_cli.kanban_db_dispatch``, which gives it
    # a fresh ``kanban_pacer_bridge`` module object; patching a separately
    # imported one would then miss the module the dispatcher actually calls.
    monkeypatch.setattr(kbd._kpb, "observe_tick", _observe)

    conn = kbc.connect()
    try:
        kb.create_task(conn, title="t", assignee="alice")
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: 4242)
    finally:
        conn.close()

    assert lock_free_at_pacer_time, (
        "the pacer was never consulted"
        + f"; dispatcher module: {kbd.__file__}"
        + f"; bridge module: {kbd._kpb.__file__}"
        + f"; patched observe_tick is the dispatcher's: "
        f"{kbd._kpb.observe_tick is _observe}"
    )
    assert lock_free_at_pacer_time == [True], (
        "the board's writer lock was held while the pacer was consulted; the "
        "pacer round trip must happen outside the critical section"
    )
