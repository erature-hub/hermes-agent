"""Worker PROGRESS signals: is the worker alive, or is it producing output?

The dispatcher used to judge a running worker by its HEARTBEAT alone. A worker
parked mid-turn — waiting on a provider response that never arrives — keeps
touching activity, so ``agent.activity_tracking._touch_activity`` keeps
bridging a fresh heartbeat into ``tasks.last_heartbeat_at`` every 60 s. The
heartbeat measures LIVENESS, not PROGRESS: a parked worker is
indistinguishable from a working one, and it holds its dispatch slot until a
human reclaims it.

Three signals are measured here, and each is used only where it is
trustworthy:

``worker_transcript_progress``
    When the worker session last wrote a MESSAGE. This is the real progress
    signal: every tool call and every reply appends to the transcript, so a
    working worker advances it continuously while a parked worker leaves it
    frozen. It is read from the profile's own ``state.db`` (read-only), so it
    works regardless of how the host accounts for CPU.

``worker_log_idle_seconds``
    How long the worker's own log file has gone unwritten. This is the
    card-log signal — and it is a WEAK one: the worker's stdout is a file
    handle, not a tty, so Python block-buffers it. A healthy worker mid-task
    was measured on a live fleet with a log 51 minutes stale while its
    transcript grew every few seconds. It is therefore used as a SECOND gate
    (both must be quiet), never as the decision.

``process_cpu_seconds`` / ``sample_cpu_deltas``
    The CPU time a worker burned over a sample window. Used ONLY as a VETO —
    a worker that is visibly burning cycles is never reclaimed, whatever the
    other signals say. It is never required: cross-process CPU accounting is
    not available on every host (measured on this Windows host: a child
    spinning at 100% reads back 0.00 CPU seconds through both ``psutil`` and
    ``GetProcessTimes``, while the child's own ``time.process_time()`` reports
    the real 1.98 s), and a gate that cannot be measured must not be treated
    as a gate that was passed.

Every "cannot measure" case returns ``None`` / is ABSENT from the result.
Unknown is never idle: the dispatcher must not reclaim a worker it could not
measure.
"""

from __future__ import annotations

import contextlib
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable, Optional

__all__ = [
    "TranscriptProgress",
    "newest_worker_session_id",
    "process_cpu_seconds",
    "sample_cpu_deltas",
    "worker_log_idle_seconds",
    "worker_transcript_progress",
]

# The worker prints its session id as the first line of its own log:
#     Session: local-<hash>
# The NEWEST such line belongs to the CURRENT attempt (the log is appended to
# across attempts and only rotated once it grows past the rotation size).
_SESSION_LINE = re.compile(r"^Session:\s+(\S+)", re.MULTILINE)

# How much of the worker log to read when looking for the session id. The
# current attempt's line sits at the start of that attempt's output, which can
# be far back in a long log: the tail is tried first and the whole file is read
# only when the tail has no match.
_LOG_TAIL_BYTES = 512 * 1024
_LOG_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True)
class TranscriptProgress:
    """How far a worker session has actually got."""

    session_id: str
    last_message_at: float
    messages: int
    tool_calls: int

    def idle_seconds(self, now: Optional[float] = None) -> float:
        return max(0.0, (now if now is not None else time.time()) - self.last_message_at)


def worker_log_idle_seconds(task_id: str, *, board: Optional[str] = None) -> Optional[float]:
    """Seconds since the worker's own log file was last written.

    ``None`` when the log is missing or unreadable — the dispatcher cannot
    measure "no output" without it, and a worker it cannot measure is never
    reclaimed on this signal.
    """
    from hermes_cli import kanban_db as _kb

    try:
        path = _kb.worker_log_path(task_id, board=board)
        mtime = path.stat().st_mtime
    except Exception:
        return None
    return max(0.0, time.time() - mtime)


def newest_worker_session_id(task_id: str, *, board: Optional[str] = None) -> Optional[str]:
    """The session id of the worker's CURRENT attempt, read from its own log.

    ``None`` when the log holds no session line (the worker never got that far)
    or cannot be read — the dispatcher then cannot map the card to a transcript
    and must not reclaim it.
    """
    from hermes_cli import kanban_db as _kb

    try:
        path = _kb.worker_log_path(task_id, board=board)
    except Exception:
        return None
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _LOG_TAIL_BYTES:
                handle.seek(size - _LOG_TAIL_BYTES)
            found = _SESSION_LINE.findall(
                handle.read(_LOG_TAIL_BYTES).decode("utf-8", errors="replace")
            )
            if not found and size > _LOG_TAIL_BYTES:
                handle.seek(0)
                found = _SESSION_LINE.findall(
                    handle.read(_LOG_MAX_BYTES).decode("utf-8", errors="replace")
                )
    except Exception:
        return None
    return found[-1] if found else None


def worker_transcript_progress(
    assignee: Optional[str], session_id: Optional[str]
) -> Optional[TranscriptProgress]:
    """How far the worker session has got, read from the profile's ``state.db``.

    Read-only: the worker owns that database and keeps writing to it, so the
    dispatcher only ever looks. ``None`` when the profile, the session or the
    message history cannot be read — unknown, never idle.
    """
    if not assignee or not session_id:
        return None
    path = _profile_state_db(assignee)
    if path is None:
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
    except Exception:
        return None
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT message_count, tool_call_count FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        last = conn.execute(
            "SELECT MAX(timestamp) FROM messages WHERE session_id = ?",
            (session_id,),
        ).fetchone()[0]
        if last is None:
            return None
        return TranscriptProgress(
            session_id=session_id,
            last_message_at=float(last),
            messages=int(row["message_count"] or 0),
            tool_calls=int(row["tool_call_count"] or 0),
        )
    except Exception:
        return None
    finally:
        with contextlib.suppress(Exception):
            conn.close()


def _profile_state_db(assignee: str):
    """``<profile home>/state.db`` for a profile name, or ``None``."""
    try:
        from hermes_cli.profiles import get_profile_dir

        return get_profile_dir(assignee) / "state.db"
    except Exception:
        return None


def process_cpu_seconds(pid: Optional[int]) -> Optional[float]:
    """Total CPU seconds (user + system) consumed by ``pid`` so far.

    ``None`` when the process is gone or its counters cannot be read. Callers
    must read ``None`` as "unknown", never as "idle" — and must never make a
    reclaim depend on it (see the module docstring).
    """
    if not pid:
        return None
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None

    try:
        import psutil
    except Exception:
        psutil = None  # type: ignore[assignment]
    if psutil is not None:
        try:
            times = psutil.Process(pid).cpu_times()
            return float(times.user) + float(times.system)
        except Exception:
            return None

    seconds = _proc_cpu_seconds(pid)
    if seconds is not None:
        return seconds
    return _windows_cpu_seconds(pid)


def sample_cpu_deltas(pids: Iterable[int], window_seconds: float) -> dict[int, float]:
    """CPU seconds each pid consumed over ``window_seconds``.

    ONE window is shared by every pid: the dispatcher samples the whole fleet
    once per tick, never once per card. A pid that cannot be read at either end
    of the window is ABSENT from the result — "unreadable" is not "idle", and
    the caller must not read a missing measurement as a quiet worker.
    """
    wanted: list[int] = []
    for raw in pids:
        try:
            pid = int(raw)
        except (TypeError, ValueError):
            continue
        if pid > 0 and pid not in wanted:
            wanted.append(pid)
    if not wanted:
        return {}

    before = {pid: process_cpu_seconds(pid) for pid in wanted}
    time.sleep(max(0.0, float(window_seconds)))
    deltas: dict[int, float] = {}
    for pid in wanted:
        start = before.get(pid)
        if start is None:
            continue
        end = process_cpu_seconds(pid)
        if end is None:
            continue
        deltas[pid] = max(0.0, end - start)
    return deltas


def _proc_cpu_seconds(pid: int) -> Optional[float]:
    """Linux ``/proc/<pid>/stat`` CPU seconds (utime + stime), or ``None``."""
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as handle:
            data = handle.read()
    except OSError:
        return None
    # ``comm`` may contain spaces and parens: everything after the LAST ')'
    # is the whitespace-separated tail, where utime is field 12 and stime 13.
    tail = data[data.rfind(")") + 1:].split()
    sysconf = getattr(os, "sysconf", None)
    if sysconf is None:
        return None
    try:
        ticks = float(tail[11]) + float(tail[12])
        clock = sysconf("SC_CLK_TCK")
    except (IndexError, ValueError, OSError):
        return None
    if not clock:
        return None
    return ticks / float(clock)


def _windows_cpu_seconds(pid: int) -> Optional[float]:
    """Windows ``GetProcessTimes`` CPU seconds (kernel + user), or ``None``."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(0x1000, False, int(pid))  # QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            creation = wintypes.FILETIME()
            exit_time = wintypes.FILETIME()
            kernel = wintypes.FILETIME()
            user = wintypes.FILETIME()
            ok = kernel32.GetProcessTimes(
                handle,
                ctypes.byref(creation),
                ctypes.byref(exit_time),
                ctypes.byref(kernel),
                ctypes.byref(user),
            )
            if not ok:
                return None

            def _ticks(value: wintypes.FILETIME) -> int:
                return (value.dwHighDateTime << 32) | value.dwLowDateTime

            # FILETIME counts 100-nanosecond units.
            return (_ticks(kernel) + _ticks(user)) / 1e7
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None
