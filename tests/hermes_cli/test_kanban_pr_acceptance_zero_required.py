"""The zero-required-checks acceptance path and the auto-decompose guard.

Two regressions are covered:

* ``collect_acceptance`` used to answer "no required checks configured" with
  ``classification="missing"`` for any repository that declares no required
  status checks. That is a permanent refusal no operator can act on once the
  contract is bound to a PR URL, so approved PR cards deadlocked. The
  zero-required path now verifies the evidence the head actually published.
* the gateway's auto-decomposer fanned a blocked-on-capability card (and a
  card whose PR was already approved) out into duplicate children. Both
  shapes are now refused by a board-local guard.
"""
import json
import os
import subprocess
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pr_acceptance as kpa
from hermes_cli.kanban_db_connect import connect
from gateway import kanban_watchers_dispatcher as kwd


@pytest.fixture
def github(tmp_path, monkeypatch):
    """A local GitHub stand-in for a repository with NO required checks.

    ``state`` controls the published verdicts:
      ``head``      -- {check name: conclusion} published for the PR head
      ``base``      -- {check name: conclusion} published for the base branch
      ``mergeable`` -- the PR's mergeable flag (None while GitHub recomputes)
      ``state``     -- "open" | "closed"
      ``legacy``    -- legacy commit statuses published for the head
    """
    state = {
        "head": {}, "base": {}, "mergeable": True, "state": "open", "legacy": [],
        "head_sha": "a" * 40, "base_sha": "b" * 40, "requests": [], "pending": [],
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state["requests"].append(self.path)
            sha = state["head_sha"]
            if self.path == "/graphql":
                # No requiredStatusChecks: the repository protects its base
                # branch but gates nothing on CI.
                payload = {
                    "headRefOid": sha, "baseRefName": "main", "state": "OPEN",
                    "baseRef": {"branchProtectionRule": {"requiredStatusChecks": []}},
                }
                value = {"data": {"repository": {"pullRequest": payload}}}
            elif "/rules/branches/" in self.path:
                value = [[]]
            elif "/check-runs" in self.path:
                if state["base_sha"] in self.path:
                    runs = [{"id": 900 + i, "name": name, "head_sha": state["base_sha"],
                             "app": {"id": 1}, "status": "completed", "conclusion": conclusion}
                            for i, (name, conclusion) in enumerate(state["base"].items())]
                else:
                    runs = [{"id": 100 + i, "name": name, "head_sha": sha,
                             "app": {"id": 1}, "status": "completed", "conclusion": conclusion}
                            for i, (name, conclusion) in enumerate(state["head"].items())]
                    # A check that CI has wired to the head but has not reported
                    # on yet: status != completed, conclusion is null.
                    for i, name in enumerate(state["pending"]):
                        runs.append({"id": 300 + i, "name": name, "head_sha": sha,
                                     "app": {"id": 1}, "status": "in_progress", "conclusion": None})
                value = [{"total_count": len(runs), "check_runs": runs}]
            elif "/statuses" in self.path:
                value = [[{**s, "sha": sha} for s in state["legacy"]]]
            elif "/pulls/" in self.path:
                value = {"head": {"sha": sha}, "base": {"ref": "main", "sha": state["base_sha"]},
                         "state": state["state"], "merged": False,
                         "mergeable": state["mergeable"], "mergeable_state": "clean"}
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/"

    # Stand in for the ``gh`` process: the real gh would GET the endpoint
    # named after "api" and print the response. Only the spawn is faked —
    # ``_api`` still parses the payload, so the code under test is exercised
    # end to end. (A PATH shim cannot work here: a shebang script is not
    # executable on Windows, which is why the sibling acceptance test is
    # POSIX-only.)
    real_run = subprocess.run

    def fake_run(command, **kwargs):
        if not command or command[0] != "gh":
            return real_run(command, **kwargs)
        endpoint = command[2]
        with urllib.request.urlopen(base_url + endpoint) as response:
            body = response.read().decode()
        return subprocess.CompletedProcess(command, 0, stdout=body, stderr="")

    monkeypatch.setattr(kpa.subprocess, "run", fake_run)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    # This test must never touch the operator's real board, and must not be
    # refused by the delegated-child write fence when it runs from inside an
    # agent session (the same cleanup evals/kanban_pr_acceptance_live.py does).
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.init_db()
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _complete(conn, contract="acme/repo", published="https://github.com/acme/repo/pull/7"):
    tid = kb.create_task(conn, title="Publish", completion_contract=contract)
    ok = kb.complete_task(conn, tid, result="done", metadata={"published_pr": published})
    receipt = json.loads(conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='pr_acceptance' ORDER BY id DESC",
        (tid,),
    ).fetchone()[0])
    return ok, receipt


def test_zero_required_checks_accepts_a_green_head(github):
    """A head whose published verdicts all pass is accepted as verified."""
    github["head"] = {"ci": "success", "lint": "success"}
    github["base"] = {"ci": "success", "lint": "success"}
    with connect() as conn:
        ok, receipt = _complete(conn)
    assert ok is True
    assert receipt["classification"] == "success"
    assert receipt["required"] == []
    assert {c["name"] for c in receipt["checks"]} == {"ci", "lint"}


def test_zero_required_checks_does_not_deadlock_on_a_clean_head(github):
    """The regression: this used to be classification="missing" and ok=False."""
    github["head"] = {"ci": "success"}
    with connect() as conn:
        ok, receipt = _complete(conn)
    assert ok is True
    assert receipt["classification"] != "missing"
    assert receipt["detail"]


def test_zero_required_checks_rejects_a_regression_the_base_branch_is_green_on(github):
    """A check red on the head but green on the base branch is this PR's fault."""
    github["head"] = {"ci": "failure", "lint": "success"}
    github["base"] = {"ci": "success", "lint": "success"}
    with connect() as conn:
        ok, receipt = _complete(conn)
    assert ok is False
    assert receipt["classification"] == "failure"
    assert "ci" in receipt["detail"]


def test_a_regression_receipt_names_the_check_and_the_cheapest_disambiguation(github):
    """A "regression" verdict must be actionable, not a bare refusal.

    A red verdict can be runner noise (host, timing, environment) rather than
    a fault of the PR. The receipt must name the check AND offer the cheapest
    way to tell the two apart - re-running the job - instead of asserting a
    fault it cannot prove.
    """
    github["head"] = {"Backend (lint + type + test)": "failure"}
    github["base"] = {"Backend (lint + type + test)": "success"}
    with connect() as conn:
        ok, receipt = _complete(conn)
    assert ok is False
    assert receipt["classification"] == "failure"
    detail = receipt["detail"]
    assert "Backend (lint + type + test)" in detail
    assert "Re-run the job first" in detail


def test_zero_required_checks_accepts_pre_existing_base_branch_breakage(github):
    """Red on the head AND on the base branch is pre-existing, not a regression.

    ``erature-hub/aisuite`` main was itself red on the backend suite, so every
    PR inherited a red ``Backend (lint + type + test)`` check. Blocking on that
    would deadlock every PR card in the repo.
    """
    github["head"] = {"Backend (lint + type + test)": "failure", "lint": "success"}
    github["base"] = {"Backend (lint + type + test)": "failure", "lint": "success"}
    with connect() as conn:
        ok, receipt = _complete(conn)
    assert ok is True
    assert receipt["classification"] == "advisory"
    assert "Backend" in receipt["detail"]


def test_zero_required_checks_treats_skipped_as_not_a_failure(github):
    """A conditionally gated job that declined to run says nothing about the PR."""
    github["head"] = {"Trivy (filesystem scan)": "skipped", "ci": "success"}
    github["base"] = {"Trivy (filesystem scan)": "skipped", "ci": "success"}
    with connect() as conn:
        ok, receipt = _complete(conn)
    assert ok is True
    assert receipt["classification"] == "success"


def test_zero_required_checks_waits_for_an_unreported_verdict(github):
    """CI wired to the head but not yet reporting is pending, not a refusal.

    ``pending`` must NOT be ``advisory`` and must NOT be ``missing``: the PR
    has a check in flight, so the honest answer is "CI has not reported yet,
    retry" — a transient state an operator can act on by re-running, not a
    permanent gate no operator can satisfy.
    """
    github["head"] = {"ci": "success"}
    github["pending"] = ["Backend (lint + type + test)"]
    with connect() as conn:
        ok, receipt = _complete(conn)
    assert receipt["classification"] == "pending"
    assert receipt["classification"] != "missing"
    assert ok is False  # retryable: the verdict has not been published yet
    assert "Backend" in receipt["detail"]
    assert {c["name"] for c in receipt["checks"]} == {"ci", "Backend (lint + type + test)"}
    pending_names = {c["name"] for c in receipt["checks"] if c["classification"] == "pending"}
    assert pending_names == {"Backend (lint + type + test)"}


def test_the_evidence_list_labels_a_red_check_as_a_failure(github):
    """A red check must not be filed under the same label as a declined job.

    The receipt's ``checks`` array is the record an operator audits. A red
    check labelled "infra" reads as "not the PR's fault" - which is exactly
    the wrong reading for the check the receipt is blocking on.
    """
    github["head"] = {"ci": "failure", "gate": "skipped"}
    github["base"] = {"ci": "success", "gate": "skipped"}
    with connect() as conn:
        ok, receipt = _complete(conn)
    assert ok is False
    assert receipt["classification"] == "failure"
    labels = {c["name"]: c["classification"] for c in receipt["checks"]}
    assert labels["ci"] == "failure"
    assert labels["gate"] == "skipped"


def test_zero_required_checks_pending_does_not_mask_a_regression(github):
    """A red verdict outranks an in-flight one: the regression must still block."""
    github["head"] = {"ci": "failure"}
    github["base"] = {"ci": "success"}
    github["pending"] = ["lint"]
    with connect() as conn:
        ok, receipt = _complete(conn)
    assert ok is False
    assert receipt["classification"] == "failure"
    assert "ci" in receipt["detail"]


def test_zero_required_checks_accepts_a_head_with_no_ci_at_all(github):
    """A repository that runs no CI for this PR has nothing to gate on."""
    github["head"] = {}
    github["base"] = {}
    with connect() as conn:
        ok, receipt = _complete(conn)
    assert ok is True
    assert receipt["classification"] == "advisory"


def test_zero_required_checks_rejects_a_conflicting_pr(github):
    """A PR that cannot be merged is not a deliverable."""
    github["head"] = {"ci": "success"}
    github["mergeable"] = False
    with connect() as conn:
        ok, receipt = _complete(conn)
    assert ok is False
    assert receipt["classification"] == "conflict"


def test_zero_required_checks_rejects_a_closed_pr(github):
    github["head"] = {"ci": "success"}
    github["state"] = "closed"
    with connect() as conn:
        ok, receipt = _complete(conn)
    assert ok is False
    assert receipt["classification"] == "conflict"


def test_legacy_status_failure_on_the_head_is_reported(github):
    """A legacy commit status is evidence too, and a red one blocks."""
    github["head"] = {}
    github["legacy"] = [{"id": 7, "context": "build", "state": "failure"}]
    with connect() as conn:
        ok, receipt = _complete(conn)
    assert ok is False
    assert receipt["classification"] == "failure"
    assert "build" in receipt["detail"]


# --- the auto-decompose guard -------------------------------------------------


def _dispatcher():
    settings = kwd._DispatcherSettings(60.0, None, None, 2, 0, True, None, None)
    from types import SimpleNamespace
    return kwd._KanbanDispatcher(SimpleNamespace(DEFAULT_BOARD="default"), settings)


def test_auto_decompose_refuses_a_capability_blocked_card(tmp_path, monkeypatch):
    """The incident shape: a card blocked on a capability wall must not fan out."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="blocked", triage=True)
        conn.execute("UPDATE tasks SET block_kind='capability' WHERE id=?", (tid,))
        conn.commit()
        reason = kwd._KanbanDispatcher._auto_decompose_blocked_reason(conn, tid)
    assert reason is not None
    assert "capability" in reason


def test_auto_decompose_refuses_an_approved_pr_awaiting_merge(tmp_path, monkeypatch):
    """Approved + open PR: finished work, one completion away — not a fan-out."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="approved", triage=True,
                             completion_contract="https://github.com/acme/repo/pull/7")
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'review_requested', '{}', 1)",
            (tid,),
        )
        conn.commit()
        reason = kwd._KanbanDispatcher._auto_decompose_blocked_reason(conn, tid)
    assert reason is not None
    assert "approved" in reason


def test_auto_decompose_allows_a_card_with_changes_requested(tmp_path, monkeypatch):
    """Changes were requested: the work is genuinely open again."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="rework", triage=True,
                             completion_contract="https://github.com/acme/repo/pull/7")
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'review_requested', '{}', 1)",
            (tid,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'changes_requested', '{}', 2)",
            (tid,),
        )
        conn.commit()
        reason = kwd._KanbanDispatcher._auto_decompose_blocked_reason(conn, tid)
    assert reason is None


def test_auto_decompose_allows_a_pr_bound_card_that_was_never_reviewed(tmp_path, monkeypatch):
    """A PR-bound card with no review at all is genuinely open work.

    Only an APPROVED PR is finished work; a card whose PR has not been
    reviewed yet still needs decomposing, so the guard must not over-fire.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="unreviewed", triage=True,
                             completion_contract="https://github.com/acme/repo/pull/7")
        conn.commit()
        reason = kwd._KanbanDispatcher._auto_decompose_blocked_reason(conn, tid)
    assert reason is None


def test_auto_decompose_allows_a_card_whose_contract_is_not_a_pr_url(tmp_path, monkeypatch):
    """Only a PULL-REQUEST contract is the approved-PR shape."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="local", triage=True,
                             completion_contract="OWNER/REPO")
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'review_requested', '{}', 1)",
            (tid,),
        )
        conn.commit()
        reason = kwd._KanbanDispatcher._auto_decompose_blocked_reason(conn, tid)
    assert reason is None


def test_auto_decompose_allows_an_ordinary_triage_card(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="ordinary", triage=True)
        conn.commit()
        reason = kwd._KanbanDispatcher._auto_decompose_blocked_reason(conn, tid)
    assert reason is None


def test_auto_decompose_tick_skips_a_blocked_card(tmp_path, monkeypatch):
    """End to end through the tick: a capability-blocked card is never decomposed."""
    import hermes_cli
    from types import SimpleNamespace

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    with connect() as conn:
        tid = kb.create_task(conn, title="blocked", triage=True)
        conn.execute("UPDATE tasks SET block_kind='capability' WHERE id=?", (tid,))
        conn.commit()

    decomposed = []
    fake = SimpleNamespace(
        list_triage_ids=lambda: [tid],
        decompose_task=lambda task_id, author=None: (
            decomposed.append(task_id) or SimpleNamespace(ok=True, fanout=True, child_ids=["x"], reason=None)),
    )
    monkeypatch.setitem(sys.modules, "hermes_cli.kanban_decompose", fake)
    monkeypatch.setattr(hermes_cli, "kanban_decompose", fake, raising=False)
    monkeypatch.setattr(kwd, "_board_slugs", lambda kb: ["default"])

    assert _dispatcher().auto_decompose_tick(5) == 0
    assert decomposed == []
