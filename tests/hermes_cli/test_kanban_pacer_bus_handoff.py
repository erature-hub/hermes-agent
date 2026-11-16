"""The Hermes dispatcher hands the pacer's receipt to the alarm bus.

This is a CROSS-REPO test: it drives the real Hermes dispatcher code (this
repo) and then feeds the resulting hook payload into the real alarm-bus
plugin (the ``hermes-alarm-bus`` checkout) and asserts the single class (f)
alarm carries the pacer's evidence.

The two repos cannot import each other, so the plugin is loaded from its
checkout by path. If the plugin is not present, the test is SKIPPED rather
than silently passing -- a skipped cross-repo proof is not a proof.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


PLUGIN_CANDIDATES = [
    Path(r"C:/Users/paulf/src/hermes-alarm-bus"),
    Path.home() / "AppData/Local/hermes/plugins/hermes-alarm-bus",
]


def _load_plugin() -> tuple:
    """Import the alarm-bus plugin from its checkout, or skip."""
    for root in PLUGIN_CANDIDATES:
        if not (root / "bus.py").is_file():
            continue
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        for name in ("classify", "detectors", "state", "telegram_transport", "bus"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("bus", root / "bus.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["bus"] = module
        spec.loader.exec_module(module)
        return module, root
    pytest.skip("hermes-alarm-bus checkout not found; the cross-repo proof needs it")


class _FakePacerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
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


@pytest.fixture
def fake_pacer(monkeypatch):
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
            "evidence": "ready=3 (peak 3) todo=1 · barren 3",
            "operator_verify": ["hermes kanban status"],
            "window_s": 1800.0,
            "ready_count": 3,
            "barren_ticks": 3,
            "quota_state": "unknown",
        },
        "advisory": None,
    }
    thread = __import__("threading").Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(
        "HERMES_JEV_PACER_URL",
        f"http://127.0.0.1:{server.server_address[1]}/api/operations/micro-decisions/pacing",
    )
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def _isolated_worker_proc_registry(monkeypatch):
    monkeypatch.setattr(kbd, "_live_worker_procs", {})
    monkeypatch.setattr(kbd, "_recent_worker_exits", {})


def test_the_dispatcher_hands_the_receipt_to_the_alarm_bus(
    tmp_path, monkeypatch, fake_pacer
):
    """One tick, one receipt, one class (f) alarm -- carrying the receipt.

    This is the whole loop the JEV epic exists to close: the dispatcher
    observes, the pacer advises, the dispatcher acts, and the bus's single
    class (f) alarm is enriched by what the pacer saw. If the receipt never
    arrived, or if it arrived and produced a SECOND alarm, this fails.
    """
    plugin, _plugin_root = _load_plugin()

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()

    # Capture what the dispatcher hands to the hook.
    captured: list[dict] = []
    monkeypatch.setattr(
        kb,
        "_fire_dispatch_tick_hook",
        lambda result, **kw: captured.append(dict(kw, result=result)),
    )

    conn = kbc.connect()
    try:
        kb.create_task(conn, title="stalled work", assignee="alice")
        kbd.dispatch_once(
            conn,
            spawn_fn=lambda *a, **kw: 0,
            dry_run=True,
            board="b1",
        )
    finally:
        conn.close()

    # The pacer was asked, and its answer came back.
    assert fake_pacer.requests, "the dispatcher never consulted the pacer"
    assert len(captured) == 1, f"expected one tick hook, saw {len(captured)}"

    receipt = captured[0].get("pacing_receipt")
    assert receipt is not None, "the pacer's receipt did not ride out with the hook"
    assert receipt["class_name"] == "dispatcher_stalled_queue_full"

    # Now the bus side: the same payload shape, one alarm, enriched.
    payload = {
        "board": "b1",
        "outcome": "ok",
        "result": captured[0]["result"],
        "pacing_receipt": receipt,
    }
    hint = plugin.dispatch_hint(payload)
    assert hint is not None
    assert hint["pacer_receipt"]["barren_ticks"] == 3

    event = plugin.classify.dispatcher_stalled_queue_full(
        ready=3,
        stallable=2,
        minutes=7,
        ticks=3,
        quota_state=None,
        quota_deferred=0,
        pacer_receipt=hint["pacer_receipt"],
    )
    assert event.cls == "f"
    assert "pacer:" in event.evidence, "the pacer's evidence did not reach the alarm"
    assert "barren 3" in event.evidence
    assert event.reverify.endswith("&& hermes kanban status")
    # The receipt did not invent a second class or rewrite the dedupe key.
    assert event.dedupe_key == "stall"
