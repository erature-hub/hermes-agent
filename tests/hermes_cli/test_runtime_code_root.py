"""The runtime's code root is the deployed checkout, never a card workspace.

A kanban card workspace is a DISPOSABLE hermes-agent worktree: the dispatcher
deletes it when the card ends. Three mechanisms let such a tree become the
runtime's own code root, and each is closed here:

* ``hermes_cli/gateway.py``'s ``PROJECT_ROOT`` is derived from ``__file__``, and
  ``windowless_gateway_restart_spec`` puts that path on the respawned gateway's
  ``PYTHONPATH`` -- so a hijack is re-pinned on every restart.
* ``pin_hermes_tree_on_pythonpath`` pins the same self-derived tree onto every
  kanban worker's ``PYTHONPATH``.
* An editable install of a card workspace writes a ``sys.meta_path`` finder into
  a shared environment, and that finder outranks ``sys.path`` for the mapped
  top-level names.

The regression that matters is the fail-closed one: a synthetic workspace
mapping must be REFUSED, not tolerated.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agent import runtime_self_protection as rsp
from hermes_cli import runtime_code_root as rcr


@pytest.fixture
def deployed_checkout(tmp_path: Path) -> Path:
    """A checkout that stands in for the deployed tree."""
    root = tmp_path / "hermes-agent"
    for package in ("gateway", "hermes_cli", "agent", "tools", "cron"):
        (root / package).mkdir(parents=True)
        (root / package / "__init__.py").write_text("", encoding="utf-8")
    return root


@pytest.fixture
def card_workspace(tmp_path: Path) -> Path:
    """A disposable card workspace that mirrors the real layout."""
    ws = tmp_path / "hermes" / "kanban" / "workspaces" / "t_abc123" / "wt"
    for package in ("gateway", "hermes_cli", "agent", "tools", "cron"):
        (ws / package).mkdir(parents=True)
        (ws / package / "__init__.py").write_text("", encoding="utf-8")
    return ws


# ---------------------------------------------------------------------------
# The predicate
# ---------------------------------------------------------------------------


def test_card_workspace_path_is_recognized(card_workspace: Path):
    assert rcr.is_card_workspace_path(card_workspace)
    assert rcr.is_card_workspace_path(card_workspace / "gateway" / "__init__.py")


def test_deployed_checkout_is_not_a_card_workspace(deployed_checkout: Path):
    assert not rcr.is_card_workspace_path(deployed_checkout)


def test_workspaces_root_pin_is_honored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The dispatcher injects the workspaces root into every worker's env."""
    root = tmp_path / "elsewhere" / "scratch"
    (root / "t_1").mkdir(parents=True)
    monkeypatch.setenv("HERMES_KANBAN_WORKSPACES_ROOT", str(root))
    assert rcr.is_card_workspace_path(root / "t_1")
    assert not rcr.is_card_workspace_path(tmp_path / "elsewhere" / "other")


# ---------------------------------------------------------------------------
# The fail-closed guard: a synthetic workspace mapping must be refused
# ---------------------------------------------------------------------------


def test_guard_refuses_a_workspace_mapping(
    deployed_checkout: Path, card_workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    """A synthetic mapping that resolves the runtime out of a card workspace fails closed."""
    monkeypatch.setattr(rcr, "canonical_code_root", lambda: deployed_checkout)
    monkeypatch.setattr(rcr, "permitted_code_roots", lambda: [deployed_checkout])
    mapping = {
        "gateway": str(card_workspace / "gateway" / "__init__.py"),
        "hermes_cli": str(card_workspace / "hermes_cli" / "__init__.py"),
    }
    monkeypatch.setattr(rcr, "resolved_package_paths", lambda packages=None: mapping)

    with pytest.raises(rcr.CanonicalCodeRootError) as excinfo:
        rcr.assert_runtime_code_root_pinned(("gateway", "hermes_cli"))
    message = str(excinfo.value)
    assert "card workspace" in message
    assert str(card_workspace) in message


def test_guard_accepts_the_deployed_checkout(
    deployed_checkout: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(rcr, "canonical_code_root", lambda: deployed_checkout)
    monkeypatch.setattr(rcr, "permitted_code_roots", lambda: [deployed_checkout])
    mapping = {
        "gateway": str(deployed_checkout / "gateway" / "__init__.py"),
        "hermes_cli": str(deployed_checkout / "hermes_cli" / "__init__.py"),
    }
    monkeypatch.setattr(rcr, "resolved_package_paths", lambda packages=None: mapping)

    assert rcr.assert_runtime_code_root_pinned(("gateway", "hermes_cli")) == deployed_checkout


def test_guard_refuses_a_mapping_outside_every_permitted_root(
    deployed_checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A tree that is not a workspace but is not the deployed checkout is refused too."""
    elsewhere = tmp_path / "somewhere-else"
    elsewhere.mkdir()
    monkeypatch.setattr(rcr, "canonical_code_root", lambda: deployed_checkout)
    monkeypatch.setattr(rcr, "permitted_code_roots", lambda: [deployed_checkout])
    mapping = {"gateway": str(elsewhere / "gateway" / "__init__.py")}
    monkeypatch.setattr(rcr, "resolved_package_paths", lambda packages=None: mapping)

    with pytest.raises(rcr.CanonicalCodeRootError) as excinfo:
        rcr.assert_runtime_code_root_pinned(("gateway",))
    assert "outside every permitted code root" in str(excinfo.value)


def test_guard_refuses_an_unimportable_package(
    deployed_checkout: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(rcr, "canonical_code_root", lambda: deployed_checkout)
    monkeypatch.setattr(rcr, "permitted_code_roots", lambda: [deployed_checkout])
    monkeypatch.setattr(rcr, "resolved_package_paths", lambda packages=None: {"gateway": None})

    with pytest.raises(rcr.CanonicalCodeRootError) as excinfo:
        rcr.assert_runtime_code_root_pinned(("gateway",))
    assert "not importable" in str(excinfo.value)


def test_guard_fails_closed_when_no_anchor_is_readable(
    card_workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    """No trustworthy anchor, and the running tree is a workspace => refuse, never guess.

    A card workspace may not nominate itself: it would be naming itself from a
    tree it controls. So the fallback to the running tree is only allowed when
    that tree is NOT a workspace.
    """
    monkeypatch.setattr(rcr, "_recorded_project_root", lambda: None)
    monkeypatch.setattr(rcr, "_install_stamp_root", lambda: None)
    monkeypatch.setattr(rcr, "__file__", str(card_workspace / "hermes_cli" / "runtime_code_root.py"))
    with pytest.raises(rcr.CanonicalCodeRootError) as excinfo:
        rcr.canonical_code_root()
    assert "cannot determine the deployed checkout" in str(excinfo.value)


def test_guard_falls_back_to_the_running_tree_when_it_is_not_a_workspace(
    deployed_checkout: Path, monkeypatch: pytest.MonkeyPatch
):
    """With no installation record, a NON-workspace running tree is the fallback."""
    monkeypatch.setattr(rcr, "_recorded_project_root", lambda: None)
    monkeypatch.setattr(rcr, "_install_stamp_root", lambda: None)
    monkeypatch.setattr(rcr, "__file__", str(deployed_checkout / "hermes_cli" / "runtime_code_root.py"))
    assert rcr.canonical_code_root() == deployed_checkout.resolve()


# ---------------------------------------------------------------------------
# The worker pin: a card workspace is never pinned
# ---------------------------------------------------------------------------


def test_worker_pin_refuses_a_card_workspace(
    deployed_checkout: Path, card_workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    """The worker's PYTHONPATH pin must never name a disposable tree."""
    import cron.scheduler_worker_env as worker_env_mod

    monkeypatch.setattr(rcr, "canonical_code_root", lambda: deployed_checkout)
    env = worker_env_mod.pin_hermes_tree_on_pythonpath({}, card_workspace)
    entries = env["PYTHONPATH"].split(os.pathsep)
    assert entries[0] == str(deployed_checkout)
    assert str(card_workspace) not in env["PYTHONPATH"]


def test_worker_pin_keeps_a_normal_checkout(
    deployed_checkout: Path, monkeypatch: pytest.MonkeyPatch
):
    """A non-workspace root is still pinned as-is: no behavior change for the normal case."""
    import cron.scheduler_worker_env as worker_env_mod

    monkeypatch.setattr(rcr, "canonical_code_root", lambda: deployed_checkout)
    env = worker_env_mod.pin_hermes_tree_on_pythonpath({}, deployed_checkout)
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(deployed_checkout)


def test_worker_pin_fails_closed_without_an_anchor(
    card_workspace: Path, monkeypatch: pytest.MonkeyPatch
):
    """A workspace that cannot be replaced fails closed rather than being pinned.

    The replacement is the deployed checkout. When even that cannot be
    established -- no installation record, and the running tree is itself a
    workspace that may not nominate itself -- the pin has nothing safe to
    offer, so it raises rather than pinning a disposable tree.
    """
    import cron.scheduler_worker_env as worker_env_mod

    monkeypatch.setattr(rcr, "_recorded_project_root", lambda: None)
    monkeypatch.setattr(rcr, "_install_stamp_root", lambda: None)
    # The running tree is a workspace, so it cannot be the fallback either.
    monkeypatch.setattr(rcr, "__file__", str(card_workspace / "hermes_cli" / "runtime_code_root.py"))
    with pytest.raises(rcr.CanonicalCodeRootError) as excinfo:
        worker_env_mod.pin_hermes_tree_on_pythonpath({}, card_workspace)
    assert "refusing to pin a card workspace" in str(excinfo.value)


# ---------------------------------------------------------------------------
# The editable-install refusal
# ---------------------------------------------------------------------------


def test_editable_install_of_a_card_workspace_is_refused(card_workspace: Path):
    """``pip install -e <workspace>`` would repoint the shared environment's finder."""
    for command in (
        f"pip install -e {card_workspace}",
        f"pip install --editable {card_workspace}",
        f"pip install --editable={card_workspace}",
        f"uv pip install -e {card_workspace}",
        f"python -m pip install -e {card_workspace}",
    ):
        assert rsp.command_repoints_runtime_root(command), command


def test_editable_install_of_the_deployed_checkout_is_allowed(deployed_checkout: Path):
    """Installing the deployed tree editable is the legitimate case and is allowed."""
    assert rsp.command_repoints_runtime_root(f"pip install -e {deployed_checkout}") is None


def test_editable_install_of_an_unrelated_package_is_allowed(tmp_path: Path):
    assert rsp.command_repoints_runtime_root(f"pip install -e {tmp_path / 'other' / 'lib'}") is None


def test_non_editable_install_is_untouched(card_workspace: Path):
    assert rsp.command_repoints_runtime_root(f"pip install {card_workspace}") is None


def test_editable_install_refusal_names_the_guard(card_workspace: Path):
    """The refusal must name the workspace and the reason, so the message is actionable."""
    description = rsp.command_repoints_runtime_root(f"pip install -e {card_workspace}")
    assert description is not None
    assert str(card_workspace) in description
    assert "repoint" in description


# ---------------------------------------------------------------------------
# The editable MAPPING: the card's "runtime editable mapping"
# ---------------------------------------------------------------------------


def test_editable_mapping_reads_the_installed_targets(tmp_path: Path, deployed_checkout: Path):
    """The mapping is read out of the finder file, without importing anything."""
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    finder = site_packages / "__editable___hermes_agent_0_0_0_finder.py"
    finder.write_text(
        "from __future__ import annotations\n"
        "import sys\n"
        "MAPPING: dict[str, str] = {\n"
        f"    'gateway': '{deployed_checkout / 'gateway'}',\n"
        f"    'hermes_cli': '{deployed_checkout / 'hermes_cli'}',\n"
        "    'somepkg': 'C:\\\\elsewhere\\\\somepkg',\n"
        "}\n",
        encoding="utf-8",
    )
    targets = rcr.editable_mapping_targets([site_packages])
    assert targets["gateway"] == str(deployed_checkout / "gateway")
    assert targets["hermes_cli"] == str(deployed_checkout / "hermes_cli")
    # A package that is not part of the runtime's import surface is not reported.
    assert "somepkg" not in targets


def test_editable_mapping_into_a_workspace_fails_closed(
    tmp_path: Path, card_workspace: Path
):
    """A shared environment whose editable mapping names a workspace is refused."""
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    (site_packages / "__editable___hermes_agent_0_0_0_finder.py").write_text(
        "MAPPING: dict[str, str] = {\n"
        f"    'gateway': '{card_workspace / 'gateway'}',\n"
        f"    'hermes_cli': '{card_workspace / 'hermes_cli'}',\n"
        "}\n",
        encoding="utf-8",
    )
    with pytest.raises(rcr.CanonicalCodeRootError) as excinfo:
        rcr.assert_no_workspace_editable_mapping([site_packages])
    assert "card workspace" in str(excinfo.value)
    assert str(card_workspace) in str(excinfo.value)


def test_editable_mapping_into_the_deployed_checkout_is_allowed(
    tmp_path: Path, deployed_checkout: Path
):
    site_packages = tmp_path / "site-packages"
    site_packages.mkdir()
    (site_packages / "__editable___hermes_agent_0_0_0_finder.py").write_text(
        "MAPPING: dict[str, str] = {\n"
        f"    'gateway': '{deployed_checkout / 'gateway'}',\n"
        "}\n",
        encoding="utf-8",
    )
    rcr.assert_no_workspace_editable_mapping([site_packages])


# ---------------------------------------------------------------------------
# The worker must not resolve the runtime from its own cwd
# ---------------------------------------------------------------------------


def test_worker_argv_carries_the_no_cwd_flag():
    """``-P`` keeps the card workspace off ``sys.path[0]``.

    The worker runs with ``cwd=<card workspace>``, and the workspace IS a
    hermes-agent worktree, so without ``-P`` Python puts it at ``sys.path[0]``
    and it satisfies ``hermes_cli`` / ``gateway`` before the pinned
    ``PYTHONPATH`` is consulted. Measured on the live host: with the canonical
    root ALREADY first on ``PYTHONPATH``, the workspace still won without
    ``-P``.
    """
    from hermes_cli import kanban_db_dispatch as kbd

    argv = kbd._worker_argv(_make_task(), "worker", None)
    assert "-P" in argv
    assert argv[-2:] == ["-m", "hermes_cli.kanban_worker_client"]
    # The flag must precede the module, or Python ignores it.
    assert argv.index("-P") < argv.index("-m")


def _make_task(assignee: str = "worker"):
    """A minimal task record for the argv helper."""
    from hermes_cli import kanban_db as kb

    return kb.Task(
        id="t_argv",
        title="x",
        body=None,
        assignee=assignee,
        status="running",
        priority=0,
        created_by="test",
        created_at=1,
        started_at=None,
        completed_at=None,
        workspace_kind="dir",
        workspace_path=None,
        claim_lock="lock",
        claim_expires=None,
        tenant=None,
        current_run_id=7,
    )
