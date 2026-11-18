"""Tests for scoped plugin distributions (the ``@scope/name`` install target).

Covers the format (parsing, validation, canonicalization), the trust model
(signature verification, digest enforcement), the dependency ordering, and the
CLI routing that sends a scoped identifier down the distribution path instead
of the single-plugin path.

These are hermetic: no network, no real user home, no real plugin installs.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from hermes_cli.plugin_distributions import (
    DistributionError,
    canonical_manifest_bytes,
    component_digest,
    is_scoped_name,
    load_distribution_registry,
    parse_distribution_manifest,
    parse_scoped_name,
    resolve_distribution,
    verify_distribution_signature,
)
from hermes_cli.plugin_distributions_install import order_components


# ── Fixtures ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def publisher_keypair():
    """An Ed25519 keypair: ``(private_bytes, public_bytes)``."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    raw_private = private.private_bytes_raw()
    raw_public = private.public_key().public_bytes_raw()
    return raw_private, raw_public


def _sign(manifest: dict, private_key: bytes) -> dict:
    """Sign a manifest dict the way the packager does."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    payload = dict(manifest)
    payload.pop("signature", None)
    signed = canonical_manifest_bytes(payload)
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(signed)
    return {**payload, "signature": base64.b64encode(signature).decode("ascii")}


def _manifest(**overrides) -> dict:
    """A minimal valid manifest body."""
    manifest = {
        "distribution_version": 1,
        "name": "@erature/vertical-stack",
        "version": "1.0.0",
        "description": "test stack",
        "publisher": "Erature",
        "required_roles": ["memory", "grader"],
        "components": [
            {
                "name": "alpha",
                "repo": "erature-hub/hermes-agent",
                "subdir": "plugins/alpha",
                "ref": "a" * 40,
                "digest": "1" * 64,
                "role": "memory",
                "requires": [],
                "capabilities": ["tool:alpha"],
            },
            {
                "name": "beta",
                "repo": "erature-hub/hermes-agent",
                "subdir": "plugins/beta",
                "ref": "b" * 40,
                "digest": "2" * 64,
                "role": "grader",
                "requires": ["alpha"],
                "capabilities": [],
            },
        ],
    }
    manifest.update(overrides)
    return manifest


# ── Scoped names ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("identifier", [
    "@erature/vertical-stack",
    "@erature/reader-aware",
    "@a/b",
])
def test_scoped_names_parse(identifier):
    parsed = parse_scoped_name(identifier)
    assert parsed is not None
    assert parsed.full == identifier
    assert is_scoped_name(identifier)


@pytest.mark.parametrize("identifier", [
    "erature/vertical-stack",      # no scope marker
    "@erature",                     # no name
    "@/vertical-stack",             # empty scope
    "@Erature/vertical-stack",      # uppercase scope
    "@erature/vertical stack",      # space
    "hermes-memory-hindsight",      # a bare plugin name
    "https://github.com/erature/x",
    "",
])
def test_non_scoped_names_are_rejected(identifier):
    assert parse_scoped_name(identifier) is None
    assert not is_scoped_name(identifier)


def test_scoped_name_does_not_collide_with_owner_repo_shorthand():
    """The scoped form must not be misread as owner/repo shorthand.

    ``_resolve_git_url`` turns ``owner/repo`` into a GitHub URL. Without the
    distribution branch in cmd_install, '@erature/vertical-stack' would become
    'https://github.com/@erature/vertical-stack.git' — a clone that can never
    succeed. The scoped check happens BEFORE that path is reached.
    """
    from hermes_cli.plugins_cmd import _resolve_git_url

    assert is_scoped_name("@erature/vertical-stack")
    # The legacy resolver would misparse it — which is exactly why cmd_install
    # routes scoped identifiers away before calling it.
    assert _resolve_git_url("@erature/vertical-stack")[0].endswith("@erature/vertical-stack.git")


# ── Manifest parsing ──────────────────────────────────────────────────────────────────────

def test_parse_valid_manifest():
    manifest = parse_distribution_manifest(json.dumps(_manifest()))
    assert manifest.name == "@erature/vertical-stack"
    assert manifest.version == "1.0.0"
    assert len(manifest.components) == 2
    assert manifest.component("alpha").ref == "a" * 40
    assert manifest.component("alpha").role == "memory"
    assert manifest.required_roles == ["memory", "grader"]
    assert manifest.missing_roles() == []


def test_parse_rejects_non_scoped_name():
    with pytest.raises(DistributionError, match="@scope/name"):
        parse_distribution_manifest(json.dumps(_manifest(name="vertical-stack")))


def test_parse_rejects_branch_ref():
    """A branch ref is refused: only exact commits are installable."""
    bad = _manifest()
    bad["components"][0]["ref"] = "main"
    with pytest.raises(DistributionError, match="40-character commit SHAs"):
        parse_distribution_manifest(json.dumps(bad))


def test_parse_rejects_short_sha():
    bad = _manifest()
    bad["components"][0]["ref"] = "abc123"
    with pytest.raises(DistributionError, match="40-character commit SHAs"):
        parse_distribution_manifest(json.dumps(bad))


def test_parse_rejects_bad_digest():
    bad = _manifest()
    bad["components"][0]["digest"] = "not-a-digest"
    with pytest.raises(DistributionError, match="sha256"):
        parse_distribution_manifest(json.dumps(bad))


def test_parse_rejects_empty_components():
    with pytest.raises(DistributionError, match="non-empty 'components'"):
        parse_distribution_manifest(json.dumps(_manifest(components=[])))


def test_parse_rejects_duplicate_component_names():
    bad = _manifest()
    bad["components"][1]["name"] = "alpha"
    with pytest.raises(DistributionError, match="more than once"):
        parse_distribution_manifest(json.dumps(bad))


def test_parse_rejects_unknown_dependency():
    bad = _manifest()
    bad["components"][0]["requires"] = ["nonexistent"]
    with pytest.raises(DistributionError, match="not a component of this distribution"):
        parse_distribution_manifest(json.dumps(bad))


def test_parse_rejects_future_version():
    with pytest.raises(DistributionError, match="distribution_version"):
        parse_distribution_manifest(json.dumps(_manifest(distribution_version=99)))


def test_parse_rejects_invalid_json():
    with pytest.raises(DistributionError, match="not valid JSON"):
        parse_distribution_manifest("{not json")


def test_parse_rejects_oversized_manifest():
    with pytest.raises(DistributionError, match="1 MiB"):
        parse_distribution_manifest("x" * (1024 * 1024 + 10))


def test_missing_roles_reports_the_gap():
    """A role with no component is reported, not silently dropped."""
    manifest = parse_distribution_manifest(json.dumps(_manifest()))
    assert manifest.missing_roles() == []
    gap = parse_distribution_manifest(json.dumps(_manifest(required_roles=["memory", "reader", "grader"])))
    assert gap.missing_roles() == ["reader"]


# ── Signature verification ────────────────────────────────────────────────────────────────

def test_signature_verifies(publisher_keypair):
    private_key, public_key = publisher_keypair
    signed = _sign(_manifest(), private_key)
    manifest = parse_distribution_manifest(json.dumps(signed))
    assert verify_distribution_signature(manifest, base64.b64encode(public_key).decode()) is True


def test_signature_verifies_with_hex_key(publisher_keypair):
    private_key, public_key = publisher_keypair
    signed = _sign(_manifest(), private_key)
    manifest = parse_distribution_manifest(json.dumps(signed))
    assert verify_distribution_signature(manifest, public_key.hex()) is True


def test_unsigned_manifest_is_refused(publisher_keypair):
    _, public_key = publisher_keypair
    manifest = parse_distribution_manifest(json.dumps(_manifest()))
    with pytest.raises(DistributionError, match="unsigned"):
        verify_distribution_signature(manifest, base64.b64encode(public_key).decode())


def test_tampered_manifest_fails_signature(publisher_keypair):
    """Changing a pinned ref after signing must break verification."""
    private_key, public_key = publisher_keypair
    signed = _sign(_manifest(), private_key)
    signed["components"][0]["ref"] = "c" * 40
    manifest = parse_distribution_manifest(json.dumps(signed))
    with pytest.raises(DistributionError, match="does not verify"):
        verify_distribution_signature(manifest, base64.b64encode(public_key).decode())


def test_tampered_digest_fails_signature(publisher_keypair):
    private_key, public_key = publisher_keypair
    signed = _sign(_manifest(), private_key)
    signed["components"][0]["digest"] = "9" * 64
    manifest = parse_distribution_manifest(json.dumps(signed))
    with pytest.raises(DistributionError, match="does not verify"):
        verify_distribution_signature(manifest, base64.b64encode(public_key).decode())


def test_wrong_key_fails_signature(publisher_keypair):
    """A different publisher's key must not verify the manifest."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private_key, _ = publisher_keypair
    other_public = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    signed = _sign(_manifest(), private_key)
    manifest = parse_distribution_manifest(json.dumps(signed))
    with pytest.raises(DistributionError, match="does not verify"):
        verify_distribution_signature(manifest, base64.b64encode(other_public).decode())


def test_malformed_publisher_key_is_refused(publisher_keypair):
    private_key, _ = publisher_keypair
    signed = _sign(_manifest(), private_key)
    manifest = parse_distribution_manifest(json.dumps(signed))
    with pytest.raises(DistributionError, match="public key"):
        verify_distribution_signature(manifest, "not-a-key")


def test_canonicalization_is_stable_across_key_order(publisher_keypair):
    """Re-serializing a manifest must not change the bytes that were signed."""
    private_key, public_key = publisher_keypair
    manifest = _manifest()
    signed = _sign(manifest, private_key)
    parsed = parse_distribution_manifest(json.dumps(signed))
    # Round-trip through a differently-ordered JSON document.
    reordered = json.dumps(json.loads(json.dumps(signed)), sort_keys=False, indent=4)
    reparsed = parse_distribution_manifest(reordered)
    assert canonical_manifest_bytes(parsed) == canonical_manifest_bytes(reparsed)
    assert verify_distribution_signature(reparsed, base64.b64encode(public_key).decode()) is True


# ── Content digests ───────────────────────────────────────────────────────────────────────

def test_component_digest_is_stable_and_content_sensitive(tmp_path):
    tree = tmp_path / "plugin"
    tree.mkdir()
    (tree / "plugin.yaml").write_text("name: x\n", encoding="utf-8")
    (tree / "__init__.py").write_text("", encoding="utf-8")

    first = component_digest(tree)
    assert first == component_digest(tree)

    (tree / "plugin.yaml").write_text("name: y\n", encoding="utf-8")
    assert component_digest(tree) != first


def test_component_digest_ignores_caches(tmp_path):
    """__pycache__ is build output, not content — it must not change the digest."""
    tree = tmp_path / "plugin"
    tree.mkdir()
    (tree / "plugin.yaml").write_text("name: x\n", encoding="utf-8")
    before = component_digest(tree)
    cache = tree / "__pycache__"
    cache.mkdir()
    (cache / "x.cpython-314.pyc").write_bytes(b"\x00compiled")
    assert component_digest(tree) == before


def test_component_digest_covers_relative_paths(tmp_path):
    """Two files with identical bytes at different paths hash differently."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (a / "one.txt").write_text("same", encoding="utf-8")
    (b / "two.txt").write_text("same", encoding="utf-8")
    assert component_digest(a) != component_digest(b)


# ── Dependency ordering ───────────────────────────────────────────────────────────────────

def test_order_components_respects_dependencies():
    manifest = parse_distribution_manifest(json.dumps(_manifest()))
    ordered = [c.name for c in order_components(manifest)]
    assert ordered.index("alpha") < ordered.index("beta")


def test_order_components_is_deterministic():
    manifest = parse_distribution_manifest(json.dumps(_manifest()))
    first = [c.name for c in order_components(manifest)]
    second = [c.name for c in order_components(manifest)]
    assert first == second


def test_order_components_refuses_a_cycle():
    """A dependency cycle has no valid activation order and must be refused."""
    manifest = _manifest()
    manifest["components"][0]["requires"] = ["beta"]
    manifest["components"][1]["requires"] = ["alpha"]
    parsed = parse_distribution_manifest(json.dumps(manifest))
    with pytest.raises(DistributionError, match="dependency cycle"):
        order_components(parsed)


# ── Registry ──────────────────────────────────────────────────────────────────────────────

def test_registry_entry_is_loaded(tmp_path):
    entry = {
        "name": "@erature/vertical-stack",
        "repo": "https://github.com/erature-hub/hermes-agent.git",
        "ref": "a" * 40,
        "path": "plugins/distribution/manifest.json",
        "publisher_public_key": "x" * 43,
        "version": "1.0.0",
    }
    (tmp_path / "erature-vertical-stack.json").write_text(json.dumps(entry), encoding="utf-8")

    entries = load_distribution_registry(tmp_path)
    assert len(entries) == 1
    assert entries[0].name == "@erature/vertical-stack"
    assert entries[0].ref == "a" * 40

    found = resolve_distribution("@erature/vertical-stack", tmp_path)
    assert found.repo.endswith("hermes-agent.git")


def test_registry_skips_malformed_entries(tmp_path):
    """One bad file must not make the whole registry unusable."""
    (tmp_path / "bad.json").write_text(json.dumps({"name": "not-scoped", "repo": "x", "ref": "a" * 40}), encoding="utf-8")
    (tmp_path / "worse.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "good.json").write_text(json.dumps({
        "name": "@erature/vertical-stack", "repo": "https://github.com/erature-hub/x.git", "ref": "b" * 40,
    }), encoding="utf-8")
    entries = load_distribution_registry(tmp_path)
    assert [e.name for e in entries] == ["@erature/vertical-stack"]


def test_resolve_unknown_distribution_fails_closed(tmp_path):
    with pytest.raises(DistributionError, match="not published"):
        resolve_distribution("@erature/nonexistent", tmp_path)


def test_resolve_rejects_non_scoped_identifier(tmp_path):
    with pytest.raises(DistributionError, match="not a distribution identifier"):
        resolve_distribution("hermes-memory-hindsight", tmp_path)


def test_shipped_registry_entry_is_valid():
    """The registry shipped in this repo must itself be well-formed."""
    from hermes_cli.plugin_distributions import get_distribution_registry_dir

    registry = get_distribution_registry_dir()
    if not registry.is_dir():
        pytest.skip("no distribution registry in this checkout")
    entries = load_distribution_registry(registry)
    assert entries, "the shipped registry must contain at least one entry"
    for entry in entries:
        assert parse_scoped_name(entry.name) is not None
        assert len(entry.ref) == 40
        assert entry.publisher_public_key, "a distribution must publish its publisher key"


# ── The shipped manifest ──────────────────────────────────────────────────────────────────

def test_shipped_manifest_parses_and_has_all_four_roles():
    """The published @erature/vertical-stack manifest must cover all four roles."""
    manifest_path = Path(__file__).resolve().parents[2] / "plugins" / "distribution" / "manifest.json"
    if not manifest_path.is_file():
        pytest.skip("no shipped distribution manifest in this checkout")
    manifest = parse_distribution_manifest(manifest_path.read_text(encoding="utf-8"))
    assert manifest.name == "@erature/vertical-stack"
    assert manifest.missing_roles() == [], "every declared role must have a component"
    assert set(manifest.required_roles) == {"memory", "reader", "dispatcher", "grader"}
    for component in manifest.components:
        assert len(component.ref) == 40
        assert component.digest and len(component.digest) == 64


def test_shipped_manifest_signature_verifies_against_the_registry_key():
    """The shipped manifest must verify against the key the registry publishes."""
    from hermes_cli.plugin_distributions import get_distribution_registry_dir

    manifest_path = Path(__file__).resolve().parents[2] / "plugins" / "distribution" / "manifest.json"
    registry = get_distribution_registry_dir()
    if not manifest_path.is_file() or not registry.is_dir():
        pytest.skip("no shipped manifest/registry in this checkout")
    manifest = parse_distribution_manifest(manifest_path.read_text(encoding="utf-8"))
    entries = {e.name: e for e in load_distribution_registry(registry)}
    entry = entries.get(manifest.name)
    if entry is None:
        pytest.skip("manifest is not registered in this checkout")
    assert verify_distribution_signature(manifest, entry.publisher_public_key) is True


# ── The CLI contract ──────────────────────────────────────────────────────────────────────

def test_partial_install_exits_non_zero(monkeypatch):
    """A distribution whose components partly failed must exit non-zero.

    A partial install is a failed install. If the command returned 0, a CI job or
    an operator's script would read "exit 0" as "the stack is installed" while
    the stack is missing a component — a silent-failure shape.
    """
    from hermes_cli import plugins_cmd_install
    from hermes_cli.plugin_distributions import DistributionComponent
    from hermes_cli.plugin_distributions_install import (
        DistributionComponentResult,
        DistributionInstallResult,
    )

    component = DistributionComponent(name="alpha", ref="a" * 40)
    result = DistributionInstallResult(
        name="@erature/vertical-stack",
        results=[
            DistributionComponentResult(component=component, ok=True, installed_name="alpha"),
            DistributionComponentResult(component=component, ok=False, error="boom"),
        ])

    class _Console:
        def __init__(self):
            self.lines: list[str] = []

        def print(self, *args, **kwargs):
            self.lines.append(" ".join(str(a) for a in args))

    console = _Console()
    # install_distribution is imported lazily inside the function under test, so patch
    # the source module attribute.
    monkeypatch.setattr(
        "hermes_cli.plugin_distributions_install.install_distribution",
        lambda *a, **k: result)

    with pytest.raises(SystemExit) as excinfo:
        plugins_cmd_install._install_scoped_distribution("@erature/vertical-stack", console)
    assert excinfo.value.code == 1
    assert any("failed component" in line for line in console.lines)


def test_scoped_identifier_is_routed_before_the_single_plugin_path():
    """`hermes plugins install @scope/name` must take the distribution path.

    The ordinary single-plugin path would misparse '@erature/vertical-stack' as
    owner/repo shorthand, so the routing check has to come first.
    """
    from hermes_cli.plugin_distributions import is_scoped_name

    assert is_scoped_name("@erature/vertical-stack")
    assert not is_scoped_name("erature/vertical-stack")
    assert not is_scoped_name("hermes-memory-hindsight")


def test_review_screen_names_every_component_and_its_role():
    """The review screen must let the operator tell the stack's members apart.

    A table of four truncated repository paths tells the operator nothing about
    which plugin is which or what each one contributes, so the screen names each
    component, its role, and its source.
    """
    from hermes_cli.plugin_distributions import (
        DistributionComponent,
        DistributionManifest,
        DistributionRegistryEntry,
    )
    from hermes_cli.plugin_distributions_install import render_distribution_review

    manifest = DistributionManifest(
        name="@erature/vertical-stack",
        version="1.0.0",
        description="stack under review",
        publisher="Erature",
        required_roles=["memory", "grader"],
        components=[
            DistributionComponent(name="memory-plugin", ref="a" * 40, repo="owner/memory-repo",
                                  subdir="plugins/memory", digest="b" * 64, role="memory"),
            DistributionComponent(name="grader-plugin", ref="c" * 40, repo="owner/grader-repo",
                                  subdir="plugins/grader", digest="d" * 64, role="grader"),
        ])
    entry = DistributionRegistryEntry(
        name="@erature/vertical-stack", repo="https://example.invalid/distro.git",
        ref="e" * 40, publisher_public_key="k")

    # A REAL Rich console: the table is a renderable, so a fake console that
    # only stringifies its argument would never see the rows.
    import io
    from rich.console import Console

    buffer = io.StringIO()
    console = Console(file=buffer, width=200, no_color=True, legacy_windows=False)
    render_distribution_review(console, manifest, entry)
    text = buffer.getvalue()

    assert "memory-plugin" in text, "the screen must name each component"
    assert "grader-plugin" in text
    assert "memory" in text, "the screen must show the role each component fills"
    assert "grader" in text
    assert "owner/memory-repo" in text, "the screen must show where a component comes from"
    # The isolation mode and the per-plugin consent rule must both be stated.
    assert "Sandbox" in text
    assert "never bulk-grants" in text
