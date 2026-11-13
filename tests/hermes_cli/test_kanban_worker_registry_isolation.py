"""The dispatcher's live-worker registry must not leak across tests.

``kanban_db_dispatch`` keeps the handle of every live worker in a module-global
registry (``_live_worker_procs``) and reaps them by polling each handle. A test
that starts a worker through a stand-in ``Popen`` used to leave that stand-in
in the registry for every later test, so the next dispatch tick polled it and
died with ``AttributeError: 'FakeProc' object has no attribute 'poll'``. The
crash landed in whichever test happened to run later rather than in the one
that caused it, which made the suite order-dependent and turned every new
dispatch-level test into a coin flip.

Two invariants are pinned here: the reaper tolerates a registry entry that
cannot report an exit status, and no test inherits another's registry.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from hermes_cli import kanban_db_dispatch as kbd


class _StandIn:
    """A ``Popen``-shaped object with no way to report an exit status.

    This is the shape every worker-spawn test uses: a stand-in that carries a
    ``pid`` (so the dispatcher can track the worker) but no ``poll``.
    """

    def __init__(self, pid: int) -> None:
        self.pid = pid


@pytest.fixture
def windows_reaper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the Windows poll-based reaper on every host.

    Only the Windows branch polls the registry; POSIX reaps via ``waitpid``.
    Forcing the branch makes the invariant meaningful on every host, not just
    on the one where the leak bites.
    """
    monkeypatch.setattr(kbd._kb, "_IS_WINDOWS", True)


def test_the_reaper_survives_a_registry_entry_that_cannot_report_a_status(
    windows_reaper: None,
) -> None:
    """A stand-in left in the registry must not abort the dispatch tick.

    The registry is module-global and outlives any single caller, so an entry
    parked by a stand-in can still be in it when the next tick polls. A handle
    that cannot report an exit status is skipped, not raised on.
    """
    kbd._live_worker_procs[424_242] = _StandIn(424_242)

    assert kbd.reap_worker_zombies() == []


def test_a_reapable_registry_entry_is_still_reaped(windows_reaper: None) -> None:
    """The tolerance above must not cost the normal path its reaped PIDs."""
    proc = subprocess.Popen([sys.executable, "-c", "raise SystemExit(0)"])
    proc.wait()
    kbd._live_worker_procs[proc.pid] = proc

    assert kbd.reap_worker_zombies() == [proc.pid]


def test_a_stand_in_parked_by_one_test_does_not_reach_the_next() -> None:
    """Park a stand-in the way a worker-spawn test does.

    Runs first in this module (pytest runs a module's tests in definition
    order, and the repo's runner keeps one file in one subprocess), so the
    test below can assert that nothing survived it.
    """
    kbd._live_worker_procs[424_243] = _StandIn(424_243)

    assert 424_243 in kbd._live_worker_procs


def test_the_registry_is_empty_when_the_next_test_starts() -> None:
    """The registry a previous test polluted must not be visible here.

    Without per-test isolation the stand-in parked above is still in the
    module-global registry, and this test sees it.
    """
    assert 424_243 not in kbd._live_worker_procs
    assert kbd._live_worker_procs == {}
