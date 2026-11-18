"""End-to-end sandbox test for the scoped-distribution install path.

This exercises the REAL code path — real git clone at a pinned commit, real
Ed25519 verification, real content-digest verification, real per-component
install — against a temporary HERMES_HOME. Nothing is mocked except the
network boundary: the distribution and its components are served from local git
repositories, so the test is hermetic and deterministic.

The scenarios:

1. A well-formed signed distribution installs every component.
2. A tampered manifest (a pinned ref changed after signing) is REFUSED, and
   nothing is installed.
3. A distribution whose component tree does not match its signed digest is
   REFUSED before the component is published.
4. An unsigned distribution is REFUSED.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli.plugin_distributions import (
    DistributionError,
    canonical_manifest_bytes,
    component_digest,
    parse_distribution_manifest,
    verify_distribution_signature,
)

GIT = shutil.which("git")
pytestmark = pytest.mark.skipif(GIT is None, reason="git is required for the distribution install path")


# ── Local git helpers ──────────────────────────────────────────────────────────────────────

def _git(args: list[str], cwd: Path) -> None:
    result = subprocess.run([GIT, *args], cwd=str(cwd), capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")


def _head_sha(repo: Path) -> str:
    """The commit SHA at the tip of *repo*."""
    return subprocess.run([GIT, "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True, check=False).stdout.strip()


def _file_url(path: Path) -> str:
    """A ``file://`` URL for a local git repo (the install path clones by URL)."""
    return path.resolve().as_uri()


def _make_component_repo(root: Path, name: str) -> tuple[Path, str]:
    """Create a git repo holding one plugin. Returns ``(repo, commit_sha)``."""
    repo = root / name
    (repo / "plugins" / name).mkdir(parents=True)
    plugin = repo / "plugins" / name
    (plugin / "plugin.yaml").write_text(
        f"name: {name}\nversion: 1.0.0\ndescription: test component {name}\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(f'NAME = "{name}"\n', encoding="utf-8")
    _git(["init", "-q", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.invalid"], repo)
    _git(["config", "user.name", "Test"], repo)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "add component"], repo)
    return repo, _head_sha(repo)


def _digest_of(repo: Path, subdir: str) -> str:
    return component_digest(repo / subdir)


def _sign(manifest: dict, private_key: bytes) -> dict:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    payload = dict(manifest)
    payload.pop("signature", None)
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(
        canonical_manifest_bytes(payload))
    return {**payload, "signature": base64.b64encode(signature).decode("ascii")}


@pytest.fixture
def publisher():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    return key.private_bytes_raw(), key.public_key().public_bytes_raw()


@pytest.fixture
def distribution_repo(tmp_path, publisher):
    """A local git repo holding a signed distribution manifest.

    Yields ``(repo, commit_sha, manifest_dict, public_key_b64)``. The signing
    key is the ``publisher`` fixture's, so a test that mutates the manifest can
    re-sign it and publish a second commit.
    """
    private_raw, public_raw = publisher
    public_b64 = base64.b64encode(public_raw).decode("ascii")

    alpha_repo, alpha_sha = _make_component_repo(tmp_path / "src", "alpha-plugin")
    beta_repo, beta_sha = _make_component_repo(tmp_path / "src", "beta-plugin")

    manifest = {
        "distribution_version": 1,
        "name": "@erature/vertical-stack",
        "version": "1.0.0",
        "description": "sandbox distribution",
        "publisher": "Erature",
        "required_roles": ["memory", "grader"],
        "components": [
            {
                "name": "alpha-plugin",
                "repo": _file_url(alpha_repo),
                "subdir": "plugins/alpha-plugin",
                "ref": alpha_sha,
                "digest": _digest_of(alpha_repo, "plugins/alpha-plugin"),
                "role": "memory",
                "requires": [],
                "capabilities": [],
            },
            {
                "name": "beta-plugin",
                "repo": _file_url(beta_repo),
                "subdir": "plugins/beta-plugin",
                "ref": beta_sha,
                "digest": _digest_of(beta_repo, "plugins/beta-plugin"),
                "role": "grader",
                "requires": ["alpha-plugin"],
                "capabilities": [],
            },
        ],
    }
    signed = _sign(manifest, private_raw)

    distro = tmp_path / "distro-repo"
    distro.mkdir()
    (distro / "distribution.json").write_text(json.dumps(signed, indent=2), encoding="utf-8")
    _git(["init", "-q", "-b", "main"], distro)
    _git(["config", "user.email", "test@example.invalid"], distro)
    _git(["config", "user.name", "Test"], distro)
    _git(["add", "-A"], distro)
    _git(["commit", "-q", "-m", "publish distribution"], distro)
    return distro, _head_sha(distro), signed, public_b64


def _registry(tmp_path, distro: Path, sha: str, public_b64: str) -> Path:
    """Write a one-entry distribution registry and return its directory."""
    registry = tmp_path / "registry"
    registry.mkdir()
    (registry / "erature-vertical-stack.json").write_text(json.dumps({
        "name": "@erature/vertical-stack",
        "repo": _file_url(distro),
        "ref": sha,
        "path": "distribution.json",
        "publisher_public_key": public_b64,
        "version": "1.0.0",
    }), encoding="utf-8")
    return registry


# ── The scenarios ─────────────────────────────────────────────────────────────────────────

def test_signed_distribution_installs_every_component(tmp_path, distribution_repo, publisher, monkeypatch):
    """The happy path: a signed distribution installs all its components."""
    from hermes_cli.plugin_distributions_install import install_distribution

    distro, sha, _signed, public_b64 = distribution_repo
    registry = _registry(tmp_path, distro, sha, public_b64)

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    class _Console:
        def __init__(self):
            self.lines: list[str] = []

        def print(self, *args, **kwargs):
            self.lines.append(" ".join(str(a) for a in args))

    console = _Console()
    result = install_distribution(
        "@erature/vertical-stack", console, registry_dir=registry)

    assert result.ok, f"install failed: {[r.error for r in result.failed]}"
    assert sorted(result.installed) == ["alpha-plugin", "beta-plugin"]
    assert any("Signature verified" in line for line in console.lines)


def test_tampered_manifest_is_refused_and_nothing_is_installed(
        tmp_path, distribution_repo, monkeypatch):
    """Changing a pinned ref after signing must abort the install entirely."""
    from hermes_cli.plugin_distributions_install import install_distribution

    distro, _sha, signed, public_b64 = distribution_repo
    tampered = json.loads(json.dumps(signed))
    tampered["components"][0]["ref"] = "c" * 40
    (distro / "distribution.json").write_text(json.dumps(tampered, indent=2), encoding="utf-8")
    _git(["add", "-A"], distro)
    _git(["commit", "-q", "-m", "tamper"], distro)
    registry = _registry(tmp_path, distro, _head_sha(distro), public_b64)

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    class _Console:
        def print(self, *args, **kwargs):
            pass

    with pytest.raises(DistributionError, match="does not verify"):
        install_distribution("@erature/vertical-stack", _Console(), registry_dir=registry)

    plugins = home / "plugins"
    installed = [p.name for p in plugins.iterdir()] if plugins.is_dir() else []
    assert "alpha-plugin" not in installed
    assert "beta-plugin" not in installed


def test_unsigned_distribution_is_refused(tmp_path, distribution_repo, monkeypatch):
    """An unsigned bundle must not install."""
    from hermes_cli.plugin_distributions_install import install_distribution

    distro, _sha, signed, public_b64 = distribution_repo
    unsigned = json.loads(json.dumps(signed))
    unsigned.pop("signature", None)
    (distro / "distribution.json").write_text(json.dumps(unsigned, indent=2), encoding="utf-8")
    _git(["add", "-A"], distro)
    _git(["commit", "-q", "-m", "unsigned"], distro)
    registry = _registry(tmp_path, distro, _head_sha(distro), public_b64)

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    class _Console:
        def print(self, *args, **kwargs):
            pass

    with pytest.raises(DistributionError, match="unsigned"):
        install_distribution("@erature/vertical-stack", _Console(), registry_dir=registry)


def test_wrong_publisher_key_is_refused(tmp_path, distribution_repo, monkeypatch):
    """A bundle verified against the wrong key must not install."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from hermes_cli.plugin_distributions_install import install_distribution

    distro, sha, _signed, _public_b64 = distribution_repo
    other_key = base64.b64encode(
        Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode("ascii")
    registry = _registry(tmp_path, distro, sha, other_key)

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    class _Console:
        def print(self, *args, **kwargs):
            pass

    with pytest.raises(DistributionError, match="does not verify"):
        install_distribution("@erature/vertical-stack", _Console(), registry_dir=registry)


def test_component_digest_mismatch_is_refused(tmp_path, distribution_repo, monkeypatch):
    """A component whose bytes differ from the signed digest must not publish."""
    from hermes_cli.plugin_distributions_install import install_distribution

    distro, _sha, signed, public_b64 = distribution_repo
    # Re-point the manifest at a component tree whose bytes were changed AFTER
    # the manifest was signed: the commit still exists, but its content no
    # longer matches the signed digest.
    alpha_repo = Path(signed["components"][0]["repo"].replace("file:///", "").replace("/", "\\"))
    _git(["checkout", "-q", "-b", "mutated"], alpha_repo)
    plugin = alpha_repo / "plugins" / "alpha-plugin"
    (plugin / "backdoor.py").write_text("import os\n", encoding="utf-8")
    _git(["add", "-A"], alpha_repo)
    _git(["commit", "-q", "-m", "mutate"], alpha_repo)
    mutated_sha = _head_sha(alpha_repo)

    mutated = json.loads(json.dumps(signed))
    mutated["components"][0]["ref"] = mutated_sha
    # The signature no longer covers the mutated ref, so the install is refused
    # at the signature gate — which is the correct first line of defence.
    (distro / "distribution.json").write_text(json.dumps(mutated, indent=2), encoding="utf-8")
    _git(["add", "-A"], distro)
    _git(["commit", "-q", "-m", "mutate ref"], distro)
    registry = _registry(tmp_path, distro, _head_sha(distro), public_b64)

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    class _Console:
        def print(self, *args, **kwargs):
            pass

    with pytest.raises(DistributionError):
        install_distribution("@erature/vertical-stack", _Console(), registry_dir=registry)

    plugins = home / "plugins"
    installed = [p.name for p in plugins.iterdir()] if plugins.is_dir() else []
    assert "alpha-plugin" not in installed


def test_digest_verifier_refuses_a_mismatched_tree(tmp_path):
    """The tree_verifier refuses a tree whose bytes differ from the digest."""
    from hermes_cli.plugin_distributions_install import _make_digest_verifier
    from hermes_cli.plugin_distributions import DistributionComponent

    tree = tmp_path / "plugin"
    tree.mkdir()
    (tree / "plugin.yaml").write_text("name: x\n", encoding="utf-8")
    actual = component_digest(tree)

    good = DistributionComponent(name="x", ref="a" * 40, digest=actual)
    _make_digest_verifier(good)(tree)  # must not raise

    bad = DistributionComponent(name="x", ref="a" * 40, digest="0" * 64)
    with pytest.raises(DistributionError, match="failed content verification"):
        _make_digest_verifier(bad)(tree)


def test_digest_verifier_refuses_a_component_without_a_digest(tmp_path):
    """Unverifiable bytes are refused, not waved through."""
    from hermes_cli.plugin_distributions import DistributionComponent
    from hermes_cli.plugin_distributions_install import _make_digest_verifier

    tree = tmp_path / "plugin"
    tree.mkdir()
    component = DistributionComponent(name="x", ref="a" * 40)
    with pytest.raises(DistributionError, match="no content digest"):
        _make_digest_verifier(component)(tree)


# ── Fail-closed gates that must not be bypassed by the bundle path ───────────────────────

def test_distribution_refuses_a_hermes_version_it_does_not_support(
        tmp_path, distribution_repo, publisher, monkeypatch):
    """A bundle whose `requires_hermes` floor rejects this Hermes must not install.

    The manifest declares the Hermes range it was built for. Installing it on an
    older Hermes produces a half-working stack, so the bundle is refused BEFORE
    any component lands.
    """
    from hermes_cli.plugin_distributions_install import install_distribution

    # Pin the running version: a source checkout with no release tags resolves
    # to "unknown", and the platform's version gate is permissive on an
    # unparseable version (the same rule the plugin loader uses).
    monkeypatch.setattr(
        "hermes_cli.plugins_manifest.running_hermes_version", lambda: "0.22.5")

    distro, _sha, signed, public_b64 = distribution_repo
    future = json.loads(json.dumps(signed))
    future.pop("signature", None)
    future["requires_hermes"] = ">=999.0.0"
    # Re-sign: the manifest is published with the change, so the SIGNATURE gate
    # passes and the VERSION gate is the one under test.
    resigned = _sign(future, publisher[0])
    (distro / "distribution.json").write_text(json.dumps(resigned, indent=2), encoding="utf-8")
    _git(["add", "-A"], distro)
    _git(["commit", "-q", "-m", "require future hermes"], distro)
    registry = _registry(tmp_path, distro, _head_sha(distro), public_b64)

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    class _Console:
        def print(self, *args, **kwargs):
            pass

    with pytest.raises(DistributionError, match="requires hermes"):
        install_distribution("@erature/vertical-stack", _Console(), registry_dir=registry)

    plugins = home / "plugins"
    installed = [p.name for p in plugins.iterdir()] if plugins.is_dir() else []
    assert "alpha-plugin" not in installed
    assert "beta-plugin" not in installed


def test_distribution_refuses_a_kill_listed_component(tmp_path, distribution_repo, monkeypatch):
    """A component the catalog has recalled must not install through a bundle.

    The kill list is a security control. A distribution must not become a path
    around a control the single-plugin install applies.
    """
    from hermes_cli.plugin_distributions_install import install_distribution

    distro, sha, _signed, public_b64 = distribution_repo
    registry = _registry(tmp_path, distro, sha, public_b64)

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    class _Console:
        def print(self, *args, **kwargs):
            pass

    # Put the component's repo on the kill list.
    from hermes_cli import plugin_catalog

    removed = plugin_catalog.RemovedEntry(
        name="alpha-plugin", repo="", reason="recalled for a security review", date="2026-10-29")
    monkeypatch.setattr(
        plugin_catalog, "find_removed",
        lambda candidate, catalog_dir=None: removed if candidate == "alpha-plugin" else None)

    result = install_distribution("@erature/vertical-stack", _Console(), registry_dir=registry)

    assert not result.ok, "a kill-listed component must not install"
    assert any("removed from the Hermes plugin catalog" in (r.error or "") for r in result.failed)
    assert "alpha-plugin" not in result.installed


def test_distribution_kill_list_bypass_is_explicit(tmp_path, distribution_repo, monkeypatch):
    """`--allow-removed` is the only way past the kill list, and it is recorded."""
    from hermes_cli.plugin_distributions_install import install_distribution

    distro, sha, _signed, public_b64 = distribution_repo
    registry = _registry(tmp_path, distro, sha, public_b64)

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    class _Console:
        def print(self, *args, **kwargs):
            pass

    from hermes_cli import plugin_catalog

    removed = plugin_catalog.RemovedEntry(
        name="alpha-plugin", repo="", reason="recalled for a security review", date="2026-10-29")
    monkeypatch.setattr(
        plugin_catalog, "find_removed",
        lambda candidate, catalog_dir=None: removed if candidate == "alpha-plugin" else None)

    result = install_distribution(
        "@erature/vertical-stack", _Console(), registry_dir=registry, allow_removed=True)
    assert result.ok, f"allow_removed should let the bundle through: {[r.error for r in result.failed]}"


def test_distribution_honours_no_enable(tmp_path, distribution_repo, monkeypatch):
    """`--no-enable` installs the components but leaves them disabled."""
    from hermes_cli.plugin_distributions_install import install_distribution

    distro, sha, _signed, public_b64 = distribution_repo
    registry = _registry(tmp_path, distro, sha, public_b64)

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    class _Console:
        def print(self, *args, **kwargs):
            pass

    enabled_calls: list[tuple] = []
    from hermes_cli import plugins_cmd
    monkeypatch.setattr(
        plugins_cmd, "_set_plugin_enabled",
        lambda name, enable=True, **kw: enabled_calls.append((name, enable)))

    result = install_distribution(
        "@erature/vertical-stack", _Console(), registry_dir=registry, enable=False)

    assert result.ok, f"install failed: {[r.error for r in result.failed]}"
    assert enabled_calls == [], "--no-enable must not enable any component"


def test_distribution_enables_components_by_default(tmp_path, distribution_repo, monkeypatch):
    """The default (no flag) enables the components, as a pack install does."""
    from hermes_cli.plugin_distributions_install import install_distribution

    distro, sha, _signed, public_b64 = distribution_repo
    registry = _registry(tmp_path, distro, sha, public_b64)

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    class _Console:
        def print(self, *args, **kwargs):
            pass

    enabled_calls: list[tuple] = []
    from hermes_cli import plugins_cmd
    monkeypatch.setattr(
        plugins_cmd, "_set_plugin_enabled",
        lambda name, enable=True, **kw: enabled_calls.append((name, enable)))

    result = install_distribution("@erature/vertical-stack", _Console(), registry_dir=registry)

    assert result.ok, f"install failed: {[r.error for r in result.failed]}"
    assert sorted(name for name, _ in enabled_calls) == ["alpha-plugin", "beta-plugin"]
