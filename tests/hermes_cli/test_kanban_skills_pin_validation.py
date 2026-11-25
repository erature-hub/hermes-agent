"""Regression tests for pinned-skill validation, failure counting, edit --skills, and diagnostics (#t_f7e50010).

Covers all four parts of the fix:
1. Creation-time validation: ``create_task`` rejects skills not installed or disabled for the assignee,
   naming the pin, the assignee, and resolvable alternatives.
2. Failure accounting: a managed worker whose pinned skills cannot be loaded returns a clean failed
   turn result (never crashing the owner's managed worker), records ``exit_code=1`` + diagnostic
   ``last_output``, increments ``consecutive_failures``, and trips the circuit breaker.
3. ``hermes kanban edit --skills``: replaces or clears pinned skills without touching SQLite directly,
   and validates pins against the task's assignee.
4. One-line diagnostic: both creation refusal and runtime refusal name the pin and assignee
   (``"card pins skill '...', which <assignee> does not have"``), and ``kanban_worker_client._exit_code``
   surfaces recorded ``last_output`` even when the admission receipt settles ``unknown``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from gateway import session_kanban
from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_worker_client as kwc


def _install_skill(profile_home: Path, name: str, *, description: str = "Test skill") -> None:
    skill_dir = profile_home / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\nInstructions.\n",
        encoding="utf-8",
    )


def _setup_profile_home(tmp_path: Path, monkeypatch, profile_name: str = "cypher") -> Path:
    """Create an isolated HERMES_HOME with a named profile directory and skills/."""
    home = tmp_path / ".hermes"
    home.mkdir(parents=True, exist_ok=True)
    profile_home = home / "profiles" / profile_name
    (profile_home / "skills").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.delenv("HERMES_SKILLS_OFF", raising=False)
    monkeypatch.setattr(
        "hermes_cli.profiles.resolve_profile_env",
        lambda name: str(profile_home) if name == profile_name else str(home),
    )
    kb.init_db()
    return profile_home


def _absent_pid(base: int = 3_810_000) -> int:
    pid = base
    while psutil.pid_exists(pid):
        pid += 1
    return pid


# ---------------------------------------------------------------------------
# 1. Creation-time validation
# ---------------------------------------------------------------------------


def test_create_task_accepts_installed_enabled_skill(tmp_path, monkeypatch):
    profile_home = _setup_profile_home(tmp_path, monkeypatch, "cypher")
    _install_skill(profile_home, "anvil-m4-proof")

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="valid pin", assignee="cypher", skills=["anvil-m4-proof"])
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.skills == ["anvil-m4-proof"]


def test_create_task_rejects_missing_skill_with_diagnostic_and_alternatives(tmp_path, monkeypatch):
    profile_home = _setup_profile_home(tmp_path, monkeypatch, "cypher")
    _install_skill(profile_home, "rust-audit")
    _install_skill(profile_home, "anvil-m4-proof")

    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError) as exc_info:
            kb.create_task(conn, title="bad pin", assignee="cypher", skills=["rust-build"])
    msg = str(exc_info.value)
    assert "card pins skill 'rust-build', which cypher does not have" in msg
    assert "rust-audit" in msg


def test_create_task_rejects_disabled_skill(tmp_path, monkeypatch):
    profile_home = _setup_profile_home(tmp_path, monkeypatch, "cypher")
    _install_skill(profile_home, "anvil-m4-proof")
    _install_skill(profile_home, "enabled-skill")
    (profile_home / "config.yaml").write_text(
        "skills:\n  disabled:\n    - anvil-m4-proof\n",
        encoding="utf-8",
    )

    with kbc.connect_closing() as conn:
        with pytest.raises(ValueError) as exc_info:
            kb.create_task(conn, title="disabled pin", assignee="cypher", skills=["anvil-m4-proof"])
    msg = str(exc_info.value)
    assert "card pins skill 'anvil-m4-proof', which cypher does not have" in msg
    assert "enabled-skill" in msg


# ---------------------------------------------------------------------------
# 2 & 4. Runtime diagnostic, clean failed turn, and failure counting
# ---------------------------------------------------------------------------


def test_run_worker_turns_records_diagnostic_and_exit_1_for_unavailable_skills(tmp_path, monkeypatch):
    profile_home = _setup_profile_home(tmp_path, monkeypatch, "cypher")
    _install_skill(profile_home, "other-skill")

    path = kb.kanban_db_path()
    with kbc.connect_closing(path) as conn:
        tid = kb.create_task(conn, title="runtime bad pin", assignee="cypher")
        task = kb.claim_task(conn, tid, claimer="test-host:123")
        assert task is not None
        context = {
            "db": str(path),
            "task_id": tid,
            "run_id": task.current_run_id,
            "claim_lock": task.claim_lock,
            "profile": "cypher",
            "skills": ["rust-build"],
            "context": "",
            "goal_mode": False,
        }
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                tid,
                "worker_bound",
                {"pid": os.getpid(), "claim_lock": task.claim_lock},
                run_id=task.current_run_id,
            )

    monkeypatch.setenv("HERMES_HOME", str(profile_home))
    frame = {"policy": {"kanban_json": json.dumps(context)}, "text": "work task"}
    agent = SimpleNamespace(session_id="sess-test")

    result = session_kanban.run_worker_turns(agent, frame, [])
    assert isinstance(result, dict)
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["failure_reason"] == "kanban_skills_unavailable"
    assert result["final_response"] == "card pins skill 'rust-build', which cypher does not have"

    recorded = session_kanban.worker_result(path, context)
    assert recorded["exit_code"] == 1
    assert recorded["last_output"] == "card pins skill 'rust-build', which cypher does not have"


def test_worker_client_exit_code_surfaces_diagnostic_even_on_unknown_receipt(tmp_path, monkeypatch, capsys):
    _setup_profile_home(tmp_path, monkeypatch, "cypher")
    path = kb.kanban_db_path()
    with kbc.connect_closing(path) as conn:
        tid = kb.create_task(conn, title="unknown receipt diagnostic", assignee="cypher")
        task = kb.claim_task(conn, tid, claimer="test-host:456")
        assert task is not None
        params = {
            "task_id": tid,
            "run_id": task.current_run_id,
            "claim_lock": task.claim_lock,
        }
        with kb.write_txn(conn):
            kb._append_event(
                conn,
                tid,
                "worker_result",
                {
                    "pid": os.getpid(),
                    "claim_lock": task.claim_lock,
                    "exit_code": 1,
                    "last_output": "card pins skill 'rust-build', which cypher does not have",
                },
                run_id=task.current_run_id,
            )

    rc = kwc._exit_code({"status": "unknown"}, params, str(path), waited_s=5.0)
    out = capsys.readouterr().out
    assert rc == 1
    assert "card pins skill 'rust-build', which cypher does not have" in out


def test_skills_unavailable_failure_increments_consecutive_failures_and_trips_breaker(tmp_path, monkeypatch):
    _setup_profile_home(tmp_path, monkeypatch, "cypher")
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    host = kb._claimer_id().split(":", 1)[0]
    path = kb.kanban_db_path()
    diag = "card pins skill 'rust-build', which cypher does not have"

    with kbc.connect_closing(path) as conn:
        tid = kb.create_task(conn, title="count skills failure", assignee="cypher")
        for attempt in range(1, kbd.DEFAULT_FAILURE_LIMIT + 1):
            dead_pid = _absent_pid(3_820_000 + attempt * 10)
            lock = f"{host}:{dead_pid}"
            task = kb.claim_task(conn, tid, claimer=lock)
            assert task is not None
            run_id = task.current_run_id
            with kb.write_txn(conn):
                conn.execute(
                    "UPDATE tasks SET worker_pid = ?, started_at = ? WHERE id = ?",
                    (dead_pid, int(time.time()) - 120, tid),
                )
                conn.execute(
                    "UPDATE task_runs SET worker_pid = ? WHERE id = ?",
                    (dead_pid, run_id),
                )
                kb._append_event(
                    conn,
                    tid,
                    "worker_bound",
                    {"pid": dead_pid + 1, "claim_lock": lock},
                    run_id=run_id,
                )
                kb._append_event(
                    conn,
                    tid,
                    "worker_result",
                    {"pid": dead_pid + 1, "claim_lock": lock, "exit_code": 1, "last_output": diag},
                    run_id=run_id,
                )
            # Simulate the worker log containing the printed diagnostic line.
            log_path = kb.worker_log_path(tid)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log_path.write_text(f"{diag}\n", encoding="utf-8")

            crashed = kbd.detect_crashed_workers(conn)
            assert tid in crashed
            row = conn.execute(
                "SELECT status, consecutive_failures, last_failure_error FROM tasks WHERE id = ?",
                (tid,),
            ).fetchone()
            assert row["consecutive_failures"] == attempt
            assert diag in (row["last_failure_error"] or "")
            if attempt < kbd.DEFAULT_FAILURE_LIMIT:
                assert row["status"] == "ready"
            else:
                assert row["status"] == "blocked"


# ---------------------------------------------------------------------------
# 3. `hermes kanban edit --skills`
# ---------------------------------------------------------------------------


def test_kanban_edit_skills_cli_replaces_clears_and_validates(tmp_path, monkeypatch, capsys):
    profile_home = _setup_profile_home(tmp_path, monkeypatch, "cypher")
    _install_skill(profile_home, "anvil-m4-proof")
    _install_skill(profile_home, "git-forensics")

    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="editable skills", assignee="cypher")

    import argparse
    root = argparse.ArgumentParser()
    parser = kanban_cli.build_parser(root.add_subparsers())

    # Set via comma-separated + repeated flags
    args = parser.parse_args(["edit", tid, "--skills", "anvil-m4-proof,git-forensics"])
    assert kanban_cli.kanban_command(args) == 0
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).skills == ["anvil-m4-proof", "git-forensics"]

    # Reject invalid skill with exit code 2 and diagnostic message
    bad_args = parser.parse_args(["edit", tid, "--skills", "rust-build"])
    assert kanban_cli.kanban_command(bad_args) == 2
    err = capsys.readouterr().err
    assert "card pins skill 'rust-build', which cypher does not have" in err

    # Clear with 'none'
    clear_args = parser.parse_args(["edit", tid, "--skills", "none"])
    assert kanban_cli.kanban_command(clear_args) == 0
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).skills is None
