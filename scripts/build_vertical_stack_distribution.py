"""Generate the official @erature/vertical-stack distribution manifest.

This is a release-time tool. It:
  1. Computes each component's sha256 content digest from its PINNED tree
     (the tree at the exact commit SHA, not the working copy).
  2. Builds the manifest.
  3. Signs it with the publisher's Ed25519 private key.

The private key NEVER lives in the repository. It is read from a file path or
from the HERMES_DISTRIBUTION_SIGNING_KEY environment variable.

Run:
    python scripts/build_vertical_stack_distribution.py \\
        --key-file C:/secure/erature-publisher-ed25519.key \\
        --output plugins/distribution/manifest.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_cli.plugin_distributions import component_digest
from plugins.distribution.packager import sign_manifest

#: The Erature sovereign vertical stack. Four roles, each filled by a component
#: pinned to an exact commit SHA in a public repository.
#:
#: The four stack roles are:
#:   memory     — episodic memory (Hindsight), the substrate everything else reads.
#:   reader     — the memory-aware ingestion/synthesis surface.
#:   dispatcher — specialized task dispatch.
#:   grader     — output grading / judgment.
#:
#: A role whose component has not shipped yet is declared in ``required_roles``
#: but has no component: the manifest then reports the gap through
#: ``missing_roles()`` rather than silently shipping a half-stack. The stack
#: becomes complete as each component's pinned tree lands.
COMPONENTS = [
    {
        "name": "hermes-memory-hindsight",
        "repo": "erature-hub/hermes-agent",
        "subdir": "plugins/memory/hermes-memory-hindsight",
        "ref": "a8b7a09c9323f2a99a03351af68717c7c04bfab6",
        "role": "memory",
        "requires": [],
        "capabilities": ["memory_provider", "tool:hindsight_recall", "tool:hindsight_retain"],
    },
    {
        "name": "hermes-rss",
        "repo": "Adolanium/hermes-rss",
        "subdir": "catalog",
        "ref": "11f8ea42e353a8fa228ed1e2b40e5bc10a9bdee3",
        "role": "reader",
        "requires": ["hermes-memory-hindsight"],
        "capabilities": ["hook:feed_subscribe"],
    },
    {
        "name": "advanced-delegation",
        "repo": "NousResearch/hermes-official-plugins",
        "subdir": "advanced-delegation",
        "ref": "e404b6816b20237a82f6fbe2749b5a576cf75dba",
        "role": "dispatcher",
        "requires": [],
        "capabilities": ["command:delegation"],
    },
    {
        "name": "jev-judge",
        "repo": "DoGMaTiiC/hermes-jev",
        "subdir": "plugins/jev-judge",
        "ref": "5dddbeaac5be08281e6c3b372eeaba3639971f0a",
        "role": "grader",
        "requires": [],
        "capabilities": ["tool:jev_ask", "hook:pre_tool_call"],
    },
]

#: The roles a complete vertical stack must fill.
REQUIRED_ROLES = ["memory", "reader", "dispatcher", "grader"]

DISTRIBUTION_NAME = "@erature/vertical-stack"
DISTRIBUTION_VERSION = "1.0.0"
PUBLISHER = "Erature"
DESCRIPTION = (
    "The Erature sovereign vertical stack: episodic memory (Hindsight), a memory-aware "
    "reading surface, specialized task dispatch, and output grading."
)


def _tree_at(repo: str, ref: str, subdir: str, workdir: Path, slot: str) -> Path:
    """Materialize ``subdir`` at ``ref`` from ``repo`` under *workdir*.

    *slot* names a unique clone directory: each component comes from a different
    repository, so they cannot share one checkout path.
    """
    url = repo if repo.startswith("http") else f"https://github.com/{repo}.git"
    clone = workdir / f"clone-{slot}"
    subprocess.run(
        ["git", "clone", "--depth", "1", "--filter=blob:none", "--no-checkout", url, str(clone)],
        check=True, capture_output=True)
    subprocess.run(["git", "fetch", "--depth", "1", "origin", ref], cwd=clone, check=True, capture_output=True)
    subprocess.run(["git", "checkout", "--detach", ref], cwd=clone, check=True, capture_output=True)
    tree = clone / subdir
    if not tree.is_dir():
        raise SystemExit(f"Component subdir {subdir!r} does not exist in {repo}@{ref[:12]}")
    return tree


def build(output: Path, key_file: str | None) -> None:
    entries = []
    with tempfile.TemporaryDirectory(prefix="erature-distro-") as tmp:
        workdir = Path(tmp)
        for index, component in enumerate(COMPONENTS):
            tree = _tree_at(component["repo"], component["ref"], component["subdir"], workdir, str(index))
            digest = component_digest(tree)
            print(f"  {component['name']}: sha256:{digest[:16]}… @ {component['ref'][:12]}")
            entries.append({
                "name": component["name"],
                "repo": component["repo"],
                "subdir": component["subdir"],
                "ref": component["ref"],
                "digest": digest,
                "role": component["role"],
                "requires": component["requires"],
                "capabilities": component["capabilities"],
            })

    manifest = {
        "distribution_version": 1,
        "name": DISTRIBUTION_NAME,
        "version": DISTRIBUTION_VERSION,
        "description": DESCRIPTION,
        "publisher": PUBLISHER,
        "requires_hermes": ">=0.21.4",
        "required_roles": REQUIRED_ROLES,
        "components": entries,
    }

    if key_file:
        from plugins.distribution.packager import _load_private_key

        private_key = _load_private_key(key_file)
        manifest = sign_manifest(manifest, private_key)
        print("Signed with the publisher Ed25519 key.")
    else:
        print("WARNING: unsigned manifest (no --key-file). An unsigned distribution is refused at install.")

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the @erature/vertical-stack distribution manifest.")
    parser.add_argument("--key-file", default=None,
                        help="Publisher Ed25519 private key file (or set HERMES_DISTRIBUTION_SIGNING_KEY).")
    parser.add_argument("--output", default=str(_REPO_ROOT / "plugins" / "distribution" / "manifest.json"))
    args = parser.parse_args(argv)
    build(Path(args.output), args.key_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
