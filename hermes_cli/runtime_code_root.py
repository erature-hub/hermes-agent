"""The runtime's code root is the deployed checkout, never a card workspace.

Background
----------
Hermes resolves its own runtime packages (``gateway``, ``hermes_cli``, ``agent``,
``tools``, ``cron``) by import, and every place that resolution is *pinned* --
the gateway's respawn ``PYTHONPATH``, a kanban worker's ``PYTHONPATH``, the
editable install a card's env setup writes -- derives the path it pins from
whichever tree the importing module happened to live in
(``Path(__file__).parent.parent.resolve()``).

That self-reference is what makes a card workspace able to become the
runtime's code root:

* ``hermes_cli/gateway.py`` computes ``PROJECT_ROOT`` from its own
  ``__file__``; ``hermes_cli/gateway_windows.py``'s
  ``windowless_gateway_restart_spec`` puts that path on the respawned
  gateway's ``PYTHONPATH``. A gateway that once loaded from a card workspace
  therefore *re-pins* that workspace on every subsequent respawn -- the
  hijack is self-sustaining, and deleting the card's workspace breaks the
  running runtime.
* ``hermes_cli/kanban_db_dispatch.py`` pins the same self-derived path onto
  every kanban worker's ``PYTHONPATH``.
* A card's env setup that runs ``pip install -e .`` inside its workspace (the
  workspace IS a hermes-agent worktree) writes an ``__editable__*.pth`` plus a
  ``sys.meta_path`` finder into the *shared* environment, so the shared
  environment's mapping for ``hermes_agent`` now names the disposable
  workspace.

The DOMINANT vector, though, is not the editable finder but the WORKING
DIRECTORY. A child spawned as ``python -m <module>`` with ``cwd=<card
workspace>`` gets that workspace at ``sys.path[0]``, and the workspace is a
hermes-agent worktree: it satisfies ``gateway`` / ``hermes_cli`` / ``agent`` /
``tools`` / ``cron`` before ``PYTHONPATH`` is ever consulted. Measured on this
host, with the canonical root ALREADY first on ``PYTHONPATH``::

    gateway -> ...\\kanban\\workspaces\\t_1c09eef2\\wt\\gateway\\__init__.py

``-P`` (do not prepend the cwd to ``sys.path``) moves the resolution back to
the canonical root. The editable finder is consulted only AFTER ``PathFinder``
(it is appended to ``sys.meta_path``, so it answers only for names the path
search did not find) -- it is a fallback, not a precedence winner.

What this module provides
--------------------------
``canonical_code_root()`` -- the ONE tree the runtime may resolve its own
packages from. It is discovered from the deployment's own record (PM's
recorded project root, then the deployment's ``install-stamp.json``), never
from ``__file__`` of a module that might itself live in a workspace.

``assert_runtime_code_root_pinned()`` -- fail closed: raise unless every
runtime package resolves under a permitted code root, and unless none of them
resolves under a card workspace.

``is_card_workspace_path()`` -- the predicate for "this path is a card
workspace", used by the installers and by the guard.

The canonical root is intentionally *not* derived from ``__file__``: a module
imported from a card workspace must not be able to nominate that workspace as
the runtime's code root.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional, Sequence

__all__ = [
    "RUNTIME_PACKAGES",
    "CanonicalCodeRootError",
    "assert_runtime_code_root_pinned",
    "canonical_code_root",
    "is_card_workspace_path",
    "permitted_code_roots",
    "resolved_package_paths",
]

#: The packages that make up the Hermes runtime's own import surface. A card
#: workspace must never be the resolution site for any of them.
RUNTIME_PACKAGES: tuple[str, ...] = (
    "gateway",
    "hermes_cli",
    "agent",
    "tools",
    "cron",
)


class CanonicalCodeRootError(RuntimeError):
    """The runtime's code root is not the deployed checkout (fail closed)."""


def _normalize(path: str | Path) -> str:
    """Case-folded, separator-normalized, resolved path for comparison.

    ``os.path.normcase`` on Windows rewrites ``/`` back to ``\\``, so the case
    fold has to happen FIRST and the separator swap SECOND; doing it the other
    way round leaves a mixed-separator string that never compares equal to
    itself.
    """
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        resolved = Path(path)
    return os.path.normcase(str(resolved)).replace("\\", "/").rstrip("/")


def _is_under(path: str | Path, root: str | Path) -> bool:
    """True when ``path`` is ``root`` itself or lives under it."""
    normalized_path = _normalize(path)
    normalized_root = _normalize(root)
    return normalized_path == normalized_root or normalized_path.startswith(normalized_root + "/")


def _install_stamp_root() -> Optional[Path]:
    """The checkout the deployment stamped, if the stamp is readable.

    ``install-stamp.json`` sits in the deployed checkout's root and records the
    commit and branch that were deployed. It is written by the installer, not
    by a card, so it is a trustworthy anchor for "the deployed tree".
    """
    candidates: list[Path] = []

    # 1. The stamp next to the published launcher, which the installer writes
    #    next to the checkout it published.
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        candidates.append(Path(home) / "install-stamp.json")
    try:
        from hermes_constants import get_default_hermes_root

        candidates.append(get_default_hermes_root() / "install-stamp.json")
    except Exception:  # pragma: no cover - hermes_constants is always present in-tree
        pass

    # 2. PM's recorded project root for this install: the checkout the
    #    dependency environment was committed for.
    try:
        from pm.environments import install_state_dir, runtime_facts_path

        state = runtime_facts_path(Path(os.environ.get("HERMES_INSTALL_ROOT") or Path.cwd()))
        recorded = state.parent / "inputs" / ".project-root"
        if recorded.is_file():
            text = recorded.read_text(encoding="utf-8-sig").strip()
            if text:
                candidates.append(Path(text) / "install-stamp.json")
        del install_state_dir
    except Exception:
        pass

    for stamp in candidates:
        try:
            if not stamp.is_file():
                continue
            data = json.loads(stamp.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        root = stamp.parent
        if root.is_dir():
            return root
    return None


def _recorded_project_root() -> Optional[Path]:
    """The checkout PM committed this install's dependencies for.

    PM records it at ``<installs>/<key>/inputs/.project-root``. That record
    lives in the *data* root, outside every checkout, so a card workspace
    cannot forge it.
    """
    try:
        from pm.environments import installs_root
    except Exception:
        return None
    try:
        entries = sorted(installs_root().iterdir())
    except (OSError, ValueError):
        return None
    for entry in entries:
        recorded = entry / "inputs" / ".project-root"
        try:
            if not recorded.is_file():
                continue
            text = recorded.read_text(encoding="utf-8-sig").strip()
        except OSError:
            continue
        if not text:
            continue
        root = Path(text)
        if root.is_dir():
            return root
    return None


def canonical_code_root() -> Path:
    """The one tree the runtime may resolve its own packages from.

    Resolution order, most trustworthy first:

    1. The project root PM committed this install's dependencies for
       (``<installs>/<key>/inputs/.project-root``). It lives in the data root,
       outside every checkout, so no card can nominate itself here.
    2. The directory holding the deployment's ``install-stamp.json``.
    3. The running tree -- but ONLY when it is not a card workspace. A card
       workspace must never be able to nominate itself (it would be naming
       itself from a tree it controls), so a workspace running without a
       readable installation record fails closed instead.

    If none of these can be established the runtime has no trustworthy anchor,
    and the caller must fail closed rather than guess.
    """
    root = _recorded_project_root() or _install_stamp_root()
    if root is None:
        running = Path(__file__).resolve().parent.parent
        if not is_card_workspace_path(running):
            return running
        raise CanonicalCodeRootError(
            "cannot determine the deployed checkout: neither PM's recorded project root "
            "nor the deployment's install stamp is readable, and the running tree "
            f"({running}) is a card workspace, which may not nominate itself"
        )
    return root


def is_card_workspace_path(path: str | Path) -> bool:
    """True when ``path`` lies inside a kanban card workspace.

    Card workspaces live under ``<root>/kanban/workspaces/<task-id>`` (or the
    per-board ``<root>/kanban/boards/<slug>/workspaces`` layout). Both are
    disposable: the dispatcher deletes them when the card ends.
    """
    normalized = _normalize(path)
    if "/kanban/workspaces/" in normalized:
        return True
    if "/kanban/boards/" in normalized and "/workspaces/" in normalized:
        return True
    # An explicit pin wins over the layout: the dispatcher injects it into
    # every worker's environment.
    pinned = os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT", "").strip()
    if pinned:
        pinned_normalized = _normalize(pinned)
        if normalized == pinned_normalized or normalized.startswith(pinned_normalized + "/"):
            return True
    return False


def resolved_package_paths(packages: Sequence[str] = RUNTIME_PACKAGES) -> dict[str, Optional[str]]:
    """Where each runtime package resolves in *this* interpreter.

    ``None`` means the package is not importable here at all.
    """
    import importlib.util as iu

    resolved: dict[str, Optional[str]] = {}
    for name in packages:
        try:
            spec = iu.find_spec(name)
        except (ImportError, ValueError, AttributeError):
            resolved[name] = None
            continue
        if spec is None:
            resolved[name] = None
            continue
        origin = spec.origin
        if not origin:
            locations = list(spec.submodule_search_locations or [])
            origin = locations[0] if locations else None
        resolved[name] = origin
    return resolved


def _committed_generation_roots() -> list[Path]:
    """The sealed payload workspaces PM committed for this install.

    A committed generation's ``workspace`` is a snapshot of the checkout taken
    at commit time. It is a legitimate, durable code root (unlike a card
    workspace), so resolution into one is permitted.
    """
    roots: list[Path] = []
    try:
        from pm.environments import installs_root
    except Exception:
        return roots
    try:
        entries = sorted(installs_root().iterdir())
    except (OSError, ValueError):
        return roots
    for entry in entries:
        try:
            generations = sorted((entry / "environments").iterdir())
        except (OSError, ValueError):
            continue
        for generation in generations:
            workspace = generation / "workspace"
            try:
                if workspace.is_dir():
                    roots.append(workspace)
            except OSError:
                continue
    return roots


def permitted_code_roots() -> list[Path]:
    """The code roots the runtime is allowed to resolve its packages from."""
    roots: list[Path] = []
    try:
        roots.append(canonical_code_root())
    except CanonicalCodeRootError:
        pass
    roots.extend(_committed_generation_roots())
    return roots


def editable_mapping_targets(site_packages: Sequence[str | Path]) -> dict[str, str]:
    """The ``{package: path}`` an editable install maps, per site-packages directory.

    A ``pip install -e <tree>`` writes an ``__editable__*.pth`` plus a finder
    module into the target environment's site-packages, and that finder holds a
    literal ``MAPPING`` of the mapped top-level names. Reading the mapping
    answers "what would this environment resolve the Hermes packages from?"
    without importing anything -- which matters because the environment being
    inspected may itself be the compromised one.
    """
    import re

    mapping_re = re.compile(r"^MAPPING[^=]*=\s*(\{.*?\n\})", re.DOTALL | re.MULTILINE)
    targets: dict[str, str] = {}
    for directory in site_packages:
        base = Path(directory)
        try:
            finders = sorted(base.glob("__editable__*finder*.py"))
        except OSError:
            continue
        for finder in finders:
            try:
                text = finder.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            match = mapping_re.search(text)
            if not match:
                continue
            body = match.group(1)
            for entry in re.finditer(r"'([^']+)'\s*:\s*'([^']*)'", body):
                name, target = entry.group(1), entry.group(2)
                if name in RUNTIME_PACKAGES:
                    targets.setdefault(name, target)
    return targets


def assert_no_workspace_editable_mapping(site_packages: Sequence[str | Path]) -> None:
    """Fail closed when an editable install maps a runtime package into a workspace.

    This is the mapping check the card asks for: after any worker env setup,
    the shared environment's editable mapping for the Hermes packages must not
    name a card workspace -- because that mapping is what a later import
    resolves through, and the workspace is deleted when the card ends.
    """
    offenders = [
        f"{name} -> {target}"
        for name, target in sorted(editable_mapping_targets(site_packages).items())
        if is_card_workspace_path(target)
    ]
    if offenders:
        raise CanonicalCodeRootError(
            "the shared environment's editable install maps the Hermes runtime into a card "
            "workspace: " + "; ".join(offenders)
        )


def assert_runtime_code_root_pinned(packages: Sequence[str] = RUNTIME_PACKAGES) -> Path:
    """Fail closed unless every runtime package resolves under a permitted root.

    Returns the canonical code root on success. Raises
    :class:`CanonicalCodeRootError` when a package resolves under a card
    workspace, when it resolves outside every permitted root, or when it is not
    importable at all.
    """
    root = canonical_code_root()
    permitted = permitted_code_roots()
    resolved = resolved_package_paths(packages)

    offenders: list[str] = []
    for name, origin in resolved.items():
        if origin is None:
            offenders.append(f"{name}: not importable")
            continue
        if is_card_workspace_path(origin):
            offenders.append(f"{name}: resolves under a card workspace ({origin})")
            continue
        if not any(_is_under(origin, permitted_root) for permitted_root in permitted):
            offenders.append(f"{name}: resolves outside every permitted code root ({origin})")

    if offenders:
        raise CanonicalCodeRootError(
            "the Hermes runtime's code root is not the deployed checkout "
            f"({root}); refusing to run: " + "; ".join(offenders)
        )
    return root


def _self_test() -> int:  # pragma: no cover - operator convenience
    """Print the runtime's resolution surface and validate it."""
    try:
        root = canonical_code_root()
    except CanonicalCodeRootError as exc:
        print("FAIL:", exc)
        return 2
    print("canonical code root:", root)
    for name, origin in resolved_package_paths().items():
        marker = ""
        if origin and is_card_workspace_path(origin):
            marker = "  <-- CARD WORKSPACE"
        print(f"  {name:14s} -> {origin}{marker}")
    try:
        assert_runtime_code_root_pinned()
    except CanonicalCodeRootError as exc:
        print("FAIL:", exc)
        return 2
    print("OK: every runtime package resolves under a permitted code root")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_self_test())
