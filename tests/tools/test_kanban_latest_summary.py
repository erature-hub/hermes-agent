"""The board list must show what a completed card produced.

Workers hand off via ``summary`` (which lands on ``task_runs.summary``) and
leave ``tasks.result`` NULL — ``kanban_db.latest_summary`` says so outright:
*"Workers hand off via ``summary`` and leave ``tasks.result`` NULL, so views
need this or a done task looks empty."*

The MCP board list did not read it. A reader asking "what has this board
actually done?" got title/status/assignee and **zero evidence**, and could not
tell a card that produced nothing from a card whose evidence existed but was
unreachable. These tests pin the reader surface:

* ``kanban_list`` carries a per-card ``latest_summary`` (one batched query).
* ``kanban_show`` carries a task-level ``latest_summary`` beside ``result``.
* A card whose evidence is merely unreachable is distinguishable from a card
  that genuinely has no handoff: the field is present and ``None`` in the
  latter case, and absent evidence is never backfilled from ``tasks.result``.

The negative control is the point of the first test: the fixture card has a
NULL ``tasks.result``, so the summary can only have come from
``task_runs.summary``. A test that passed because ``result`` happened to be
populated would prove nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc


@pytest.fixture
def board(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """A board with one done card whose handoff lives only in the run summary."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()

    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="Ship the reader fix", assignee="builder")
        task = kb.claim_task(conn, tid, claimer="builder:1")
        assert task is not None
        # Hand off via `summary` only. `result` stays NULL — this is the
        # negative control: the prose below can only come from the run row.
        assert kb.complete_task(conn, tid, summary="Shipped the reader fix across three surfaces.")
        row = conn.execute(
            "SELECT result FROM tasks WHERE id = ?", (tid,)
        ).fetchone()
        assert row["result"] is None, "fixture must leave tasks.result NULL"

    # A second card with no handoff anywhere: the distinguishability case.
    with kbc.connect() as conn:
        kb.create_task(conn, title="Never ran", assignee="builder")

    return home


def _list_tasks(board_home: Path) -> list[dict]:
    from tools import kanban_tools as tools

    payload = json.loads(tools._handle_list({"limit": 50}))
    return payload["tasks"]


def test_list_surfaces_handoff_summary_when_result_is_null(board: Path) -> None:
    """The card's prose appears even though ``tasks.result`` is NULL."""
    tasks = {t["title"]: t for t in _list_tasks(board)}
    card = tasks["Ship the reader fix"]

    assert card["result" if "result" in card else "status"] == "done"
    assert card["latest_summary"] == "Shipped the reader fix across three surfaces."
    # The prose came from the run row, not from `tasks.result`.
    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT result FROM tasks WHERE id = ?", (card["id"],)
        ).fetchone()
        assert row["result"] is None


def test_list_marks_a_card_with_no_handoff_as_none(board: Path) -> None:
    """A card with no handoff is distinguishable from one whose evidence was unreachable."""
    tasks = {t["title"]: t for t in _list_tasks(board)}

    with_handoff = tasks["Ship the reader fix"]
    without_handoff = tasks["Never ran"]

    # The field is present on both, and its value differs. A reader can tell
    # "this card has no handoff" from "this card's handoff was not surfaced".
    assert with_handoff["latest_summary"] is not None
    assert without_handoff["latest_summary"] is None
    assert "latest_summary" in without_handoff


def test_show_surfaces_handoff_summary_beside_result(board: Path) -> None:
    """``kanban_show`` puts the prose at task level, not only inside runs[]."""
    from tools import kanban_tools as tools

    tasks = {t["title"]: t for t in _list_tasks(board)}
    tid = tasks["Ship the reader fix"]["id"]

    payload = json.loads(tools._handle_show({"task_id": tid}))
    task = payload["task"]

    assert task["latest_summary"] == "Shipped the reader fix across three surfaces."
    assert task["result"] is None
    # The prose was reachable without digging into the runs array.
    assert any(r.get("summary") for r in payload["runs"])


def test_list_never_backfills_summary_from_result(board: Path) -> None:
    """``latest_summary`` is never a copy of ``tasks.result``.

    The two are different columns with different meanings: ``result`` is the
    legacy field most completions leave NULL, ``latest_summary`` is the worker's
    handoff prose. Backfilling one from the other would make a column lie
    about being the primary handoff.
    """
    tasks = {t["title"]: t for t in _list_tasks(board)}
    with_handoff = tasks["Ship the reader fix"]

    with kbc.connect() as conn:
        row = conn.execute(
            "SELECT result FROM tasks WHERE id = ?", (with_handoff["id"],)
        ).fetchone()
    assert row["result"] is None
    assert with_handoff["latest_summary"] != row["result"]
