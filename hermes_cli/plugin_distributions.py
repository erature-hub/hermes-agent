"""Scoped plugin distributions — the ``@scope/name`` install target.

A *distribution* is a curated, versioned bundle of plugins addressed by a scoped
identifier (``@erature/vertical-stack``). It is the public-facing counterpart to
the curated plugin catalog: the catalog answers "install this plugin", a
distribution answers "install this whole stack, pinned, verified, and in a
declared order".

Why this is not the plugin catalog
-----------------------------------
The catalog is a flat, human-merged list of *single* plugins, each pinned to one
exact commit SHA. A distribution is a *multi-plugin* artifact that additionally
carries an author signature over the whole bundle and per-component integrity
digests. Those are different trust objects with different review surfaces, so
they live in their own registry (``plugin-distributions/``) rather than being
folded into the catalog's flat name space.

Relationship to plugin packs
----------------------------
``hermes plugins pack`` (hermes_cli/plugin_packs.py) already implements the
mechanical half of this: a declarative YAML set of plugins, each pinned to an
exact 40-character commit SHA, installed with per-plugin capability consent and
a mandatory review screen. A distribution is that same artifact plus the three
things a public bundle needs and a local pack does not:

1. A **scoped, globally-addressable name** (``@scope/name``) so the bundle has
   an identity independent of any single repository.
2. An **author signature** over the canonical bundle manifest, so an operator
   can verify that the bundle they fetched is the one Erature published — even
   if it was fetched from a mirror.
3. **Per-component integrity digests**, so a component's bytes are checked
   against the signed manifest and not merely against a commit reference.

This module owns the *format* (parsing, validation, canonicalization,
verification). Installation orchestration lives in ``plugins_cmd_install.py``;
the CLI surface lives in ``plugins_cmd.py``.

Trust model
-----------
The manifest is the trust anchor. Its canonical form is signed with Ed25519
(``cryptography``'s implementation, already a hard dependency of Hermes) by the
distribution's publisher key. The signature covers a canonical JSON encoding of
the manifest with the ``signature`` field removed, so it is stable across
re-serialization. Verification therefore proves:

* the bundle contents are the ones the publisher signed (integrity), and
* the signer holds the publisher key (authenticity).

The public key is distributed *out of band* (the registry entry, or an explicit
``--publisher-key``), never read from the manifest itself. A manifest that
carries its own key would be self-certifying and worthless.

Every install is still fail-closed on the existing per-plugin gates: the
security scan, the capability-consent prompt, and the exact-SHA pin all still
apply to each component. A distribution never bulk-grants capability consent —
that decision is per plugin, exactly as it is for a pack.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = [
    "DistributionComponent",
    "DistributionError",
    "DistributionManifest",
    "ScopedName",
    "canonical_manifest_bytes",
    "component_digest",
    "compute_tree_digest",
    "get_distribution_registry_dir",
    "is_scoped_name",
    "load_distribution_registry",
    "parse_distribution_manifest",
    "parse_scoped_name",
    "resolve_distribution",
    "verify_distribution_signature",
]


# ── Constants ────────────────────────────────────────────────────────────────────────────────

#: A scoped distribution identifier: ``@scope/name``. Mirrors npm's shape so the
#: mental model transfers, and deliberately narrower than the catalog's flat name.
SCOPED_NAME_RE = re.compile(r"^@([a-z0-9][a-z0-9._-]{0,63})/([a-z0-9][a-z0-9._-]{0,127})$")

#: The scope this Hermes fork publishes under.
ERATURE_SCOPE = "erature"

#: The canonical distribution published by Erature.
VERTICAL_STACK_NAME = f"@{ERATURE_SCOPE}/vertical-stack"

#: Exact commit SHA, lowercase hex. The same rule the catalog and packs enforce.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

#: sha256 hex digest, lowercase. 64 chars.
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

#: Manifest format version this module understands.
SUPPORTED_DISTRIBUTION_VERSION = 1

#: Signing algorithm. Ed25519 is deterministic, small, and has no parameter
#: negotiation surface to get wrong.
SIGNATURE_ALGORITHM = "ed25519"

#: Upper bound on a manifest's size, mirroring the pack reader's 1 MiB limit.
_MAX_MANIFEST_BYTES = 1 * 1024 * 1024


class DistributionError(Exception):
    """A distribution manifest is malformed, unverified, or unresolvable.

    Carries a ``failure_class`` so the CLI can attribute the failure to a
    metric bucket the same way plugin installs do (``other`` when untagged).
    """

    failure_class = "other"

    def __init__(self, message: str, *, failure_class: Optional[str] = None):
        super().__init__(message)
        if failure_class is not None:
            self.failure_class = failure_class


# ── Scoped names ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScopedName:
    """A parsed ``@scope/name`` identifier."""

    scope: str
    name: str

    @property
    def full(self) -> str:
        """The canonical ``@scope/name`` string."""
        return f"@{self.scope}/{self.name}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.full


def parse_scoped_name(identifier: str) -> Optional[ScopedName]:
    """Parse ``@scope/name``; ``None`` when *identifier* is not a scoped name.

    Never raises: callers use this to decide whether to take the distribution
    path or fall through to the ordinary plugin-install path.
    """
    if not isinstance(identifier, str):
        return None
    match = SCOPED_NAME_RE.match(identifier.strip())
    if not match:
        return None
    return ScopedName(scope=match.group(1), name=match.group(2))


def is_scoped_name(identifier: str) -> bool:
    """True when *identifier* is a ``@scope/name`` distribution identifier."""
    return parse_scoped_name(identifier) is not None


# ── Manifest model ──────────────────────────────────────────────────────────────────────────

@dataclass
class DistributionComponent:
    """One plugin inside a distribution, pinned to an exact commit."""

    name: str                                  # the plugin's id (manifest name)
    ref: str                                   # exact 40-char commit SHA
    repo: Optional[str] = None                 # owner/repo or git URL
    subdir: Optional[str] = None               # path within the repo
    digest: Optional[str] = None               # sha256 of the component tree (optional, see below)
    role: Optional[str] = None                 # the stack role this component fills (e.g. "memory")
    requires: list[str] = field(default_factory=list)   # component ids that must load first
    capabilities: list[str] = field(default_factory=list)  # declared capabilities (informational)

    @property
    def display(self) -> str:
        base = self.repo or self.name
        return f"{base}/{self.subdir}" if self.subdir else base

    @property
    def install_identifier(self) -> Optional[str]:
        """Identifier the plugin-install path understands; ``None`` for bare names.

        A bare ``name`` is resolved through the plugin catalog at install time
        (the same resolution a pack performs).

        A repository is joined to its subdirectory with ``#``, matching the
        catalog's own ``install_identifier``. That form is unambiguous for every
        repository kind: ``owner/repo#subdir`` (GitHub shorthand), a full git URL
        with a fragment, and a ``file://`` URL all resolve correctly, whereas
        ``owner/repo/subdir`` would be misread as part of the repository name
        for a non-GitHub host.
        """
        if self.repo:
            return f"{self.repo}#{self.subdir}" if self.subdir else self.repo
        return None


@dataclass
class DistributionManifest:
    """A parsed, validated distribution manifest."""

    name: str                                  # "@scope/name"
    version: str = ""
    description: str = ""
    publisher: str = ""
    publisher_key_id: str = ""
    requires_hermes: str = ""
    required_roles: list[str] = field(default_factory=list)  # roles every stack must fill
    components: list[DistributionComponent] = field(default_factory=list)
    signature: Optional[str] = None            # base64 Ed25519 signature
    signature_algorithm: str = SIGNATURE_ALGORITHM

    @property
    def scoped(self) -> Optional[ScopedName]:
        """The parsed scoped name, or ``None`` when *name* is malformed."""
        return parse_scoped_name(self.name)

    def component(self, name: str) -> Optional[DistributionComponent]:
        """The component with this id, or ``None``."""
        return next((c for c in self.components if c.name == name), None)

    def missing_roles(self) -> list[str]:
        """The required roles no component fills.

        A distribution that promises a role but ships no component for it is
        incomplete: the operator would get a stack that silently lacks a
        capability, so the gap is reported rather than papered over.
        """
        filled = {c.role for c in self.components if c.role}
        return [role for role in self.required_roles if role not in filled]


# ── Parsing + validation ───────────────────────────────────────────────────────────────────

def _require_str(value: Any, label: str, *, max_len: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DistributionError(f"{label} must be a non-empty string.", failure_class="manifest_invalid")
    text = value.strip()
    if len(text) > max_len:
        raise DistributionError(f"{label} exceeds {max_len} characters.", failure_class="manifest_invalid")
    return text


def _parse_component(item: Any, label: str) -> DistributionComponent:
    """Validate one ``components:`` entry."""
    if not isinstance(item, dict):
        raise DistributionError(f"{label} must be a mapping.", failure_class="manifest_invalid")
    name = _require_str(item.get("name"), f"{label} 'name'", max_len=128)
    ref = item.get("ref") or item.get("version")
    if not isinstance(ref, str) or not _SHA_RE.fullmatch(ref.strip().lower()):
        raise DistributionError(
            f"{label} has ref {ref!r}: refs must be exact 40-character commit SHAs "
            "(tags and branch names are rejected — pin the commit for reproducible installs).",
            failure_class="manifest_invalid")
    repo = item.get("repo") or item.get("source")
    if isinstance(repo, str):
        repo = repo.removeprefix("github:").strip() or None
    subdir = item.get("subdir")
    if subdir is not None and (not isinstance(subdir, str) or not subdir.strip("/")):
        raise DistributionError(f"{label} has an invalid 'subdir'.", failure_class="manifest_invalid")
    digest = item.get("digest")
    if digest is not None:
        digest = _require_str(digest, f"{label} 'digest'", max_len=128).lower()
        if not _SHA256_RE.fullmatch(digest):
            raise DistributionError(
                f"{label} has an invalid 'digest': expected a 64-character sha256 hex digest.",
                failure_class="manifest_invalid")
    requires = item.get("requires") or []
    if not isinstance(requires, list) or any(not isinstance(r, str) for r in requires):
        raise DistributionError(f"{label} 'requires' must be a list of component names.",
                                failure_class="manifest_invalid")
    capabilities = item.get("capabilities") or []
    if not isinstance(capabilities, list) or any(not isinstance(c, str) for c in capabilities):
        raise DistributionError(f"{label} 'capabilities' must be a list of strings.",
                                failure_class="manifest_invalid")
    role = item.get("role")
    if role is not None:
        role = _require_str(role, f"{label} 'role'", max_len=64)
    return DistributionComponent(
        name=name, ref=ref.strip().lower(), repo=repo,
        subdir=subdir.strip("/") if isinstance(subdir, str) else None,
        digest=digest, role=role,
        requires=[r.strip() for r in requires if r.strip()],
        capabilities=[c.strip() for c in capabilities if c.strip()])


def parse_distribution_manifest(text: str, *, source: str = "<distribution>") -> DistributionManifest:
    """Parse and validate a distribution manifest (JSON).

    Validation is strict and fails closed: every structural error raises
    :class:`DistributionError` rather than being silently dropped, because a
    partially-understood manifest would mean installing an unverified bundle.
    """
    if not isinstance(text, str):
        raise DistributionError(f"Distribution {source} must be text.", failure_class="manifest_invalid")
    if len(text.encode("utf-8", errors="ignore")) > _MAX_MANIFEST_BYTES:
        raise DistributionError("Distribution manifest exceeds the 1 MiB size limit.",
                                failure_class="manifest_invalid")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DistributionError(f"Distribution {source} is not valid JSON: {exc}",
                                failure_class="manifest_invalid") from exc
    if not isinstance(raw, dict):
        raise DistributionError(f"Distribution {source} must be a JSON object.",
                                failure_class="manifest_invalid")

    version = raw.get("distribution_version", 1)
    if not isinstance(version, int) or version < 1:
        raise DistributionError(f"Distribution {source} has an invalid 'distribution_version'.",
                                failure_class="manifest_invalid")
    if version > SUPPORTED_DISTRIBUTION_VERSION:
        raise DistributionError(
            f"Distribution {source} requires distribution_version {version}, but this Hermes "
            f"supports up to {SUPPORTED_DISTRIBUTION_VERSION}. Update Hermes to install it.",
            failure_class="incompatible")

    name = _require_str(raw.get("name"), f"Distribution {source} 'name'", max_len=256)
    scoped = parse_scoped_name(name)
    if scoped is None:
        raise DistributionError(
            f"Distribution {source} has name {name!r}: a distribution name must be '@scope/name' "
            "(for example '@erature/vertical-stack').",
            failure_class="manifest_invalid")

    components_raw = raw.get("components")
    if not isinstance(components_raw, list) or not components_raw:
        raise DistributionError(f"Distribution {source} must declare a non-empty 'components' list.",
                                failure_class="manifest_invalid")
    components = [_parse_component(item, f"Component #{i + 1}") for i, item in enumerate(components_raw)]

    seen: set[str] = set()
    for component in components:
        if component.name in seen:
            raise DistributionError(
                f"Distribution {source} declares component {component.name!r} more than once.",
                failure_class="manifest_invalid")
        seen.add(component.name)
    for component in components:
        for dependency in component.requires:
            if dependency not in seen:
                raise DistributionError(
                    f"Distribution {source} component {component.name!r} requires {dependency!r}, "
                    "which is not a component of this distribution.",
                    failure_class="manifest_invalid")

    required_roles = raw.get("required_roles") or []
    if not isinstance(required_roles, list) or any(not isinstance(r, str) for r in required_roles):
        raise DistributionError(f"Distribution {source} 'required_roles' must be a list of strings.",
                                failure_class="manifest_invalid")
    required_roles = [r.strip() for r in required_roles if r.strip()]

    signature = raw.get("signature")
    if signature is not None:
        signature = _require_str(signature, f"Distribution {source} 'signature'", max_len=1024)
    algorithm = raw.get("signature_algorithm") or SIGNATURE_ALGORITHM
    if algorithm != SIGNATURE_ALGORITHM:
        raise DistributionError(
            f"Distribution {source} uses signature algorithm {algorithm!r}; only "
            f"{SIGNATURE_ALGORITHM!r} is supported.",
            failure_class="manifest_invalid")

    return DistributionManifest(
        name=name,
        version=str(raw.get("version") or "").strip(),
        description=str(raw.get("description") or "").strip(),
        publisher=str(raw.get("publisher") or "").strip(),
        publisher_key_id=str(raw.get("publisher_key_id") or "").strip(),
        requires_hermes=str(raw.get("requires_hermes") or "").strip(),
        required_roles=required_roles,
        components=components,
        signature=signature,
        signature_algorithm=algorithm,
    )


# ── Canonicalization + signature verification ──────────────────────────────────────────────

def _canonical_payload(source: DistributionManifest | dict[str, Any]) -> dict[str, Any]:
    """The canonical dict both signing and verification operate on.

    Both a parsed manifest and a raw manifest dict normalize to the SAME shape
    here — identical key set, identical defaults, identical component fields —
    so the bytes that were signed are exactly the bytes that get verified. A
    field present in one form and absent in the other would otherwise make
    signing and verification disagree (and every signature would fail).
    """
    if isinstance(source, DistributionManifest):
        name = source.name
        version = source.version
        description = source.description
        publisher = source.publisher
        publisher_key_id = source.publisher_key_id
        requires_hermes = source.requires_hermes
        required_roles = list(source.required_roles)
        algorithm = source.signature_algorithm
        components = source.components
    else:
        name = str(source.get("name") or "")
        version = str(source.get("version") or "")
        description = str(source.get("description") or "")
        publisher = str(source.get("publisher") or "")
        publisher_key_id = str(source.get("publisher_key_id") or "")
        requires_hermes = str(source.get("requires_hermes") or "")
        required_roles = [str(r) for r in (source.get("required_roles") or [])]
        algorithm = str(source.get("signature_algorithm") or SIGNATURE_ALGORITHM)
        components = []
        for item in source.get("components") or []:
            if not isinstance(item, dict):
                continue
            components.append(DistributionComponent(
                name=str(item.get("name") or ""),
                ref=str(item.get("ref") or item.get("version") or "").strip().lower(),
                repo=item.get("repo") or item.get("source"),
                subdir=item.get("subdir"),
                digest=item.get("digest"),
                role=item.get("role"),
                requires=[str(r) for r in (item.get("requires") or [])],
                capabilities=[str(c) for c in (item.get("capabilities") or [])],
            ))

    return {
        "name": name,
        "version": version,
        "description": description,
        "publisher": publisher,
        "publisher_key_id": publisher_key_id,
        "requires_hermes": requires_hermes,
        "required_roles": required_roles,
        "signature_algorithm": algorithm,
        "components": [
            {
                "name": component.name,
                "ref": component.ref,
                "repo": component.repo,
                "subdir": component.subdir,
                "digest": component.digest,
                "role": component.role,
                "requires": list(component.requires),
                "capabilities": list(component.capabilities),
            }
            for component in components
        ],
    }


def canonical_manifest_bytes(manifest: DistributionManifest | dict[str, Any]) -> bytes:
    """The canonical byte string the signature covers.

    The ``signature`` field is excluded (it cannot cover itself) and the rest
    is serialized with sorted keys and no insignificant whitespace, so the
    signed bytes are reproducible across re-serialization and across platforms.
    """
    payload = _canonical_payload(manifest)
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_distribution_signature(
    manifest: DistributionManifest,
    publisher_public_key: str | bytes,
) -> bool:
    """Verify the manifest's Ed25519 signature against *publisher_public_key*.

    *publisher_public_key* is the publisher's base64 raw 32-byte Ed25519
    public key, or those raw bytes. The key is supplied by the caller (the
    registry entry or an explicit ``--publisher-key``), never read out of the
    manifest — a manifest that carries its own key certifies nothing.

    Returns ``True`` when the signature is valid. Raises
    :class:`DistributionError` (fail closed) when it is absent, malformed, or
    does not verify.
    """
    if not manifest.signature:
        raise DistributionError(
            f"Distribution {manifest.name} is unsigned. A distribution must carry an author "
            "signature over its canonical manifest.",
            failure_class="signature_invalid")
    key = _decode_public_key(publisher_public_key)
    signature = _decode_signature(manifest.signature)
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError as exc:  # pragma: no cover - cryptography is a hard dependency
        raise DistributionError(
            "Verifying a distribution signature requires the 'cryptography' package.",
            failure_class="signature_invalid") from exc
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(signature, canonical_manifest_bytes(manifest))
    except InvalidSignature as exc:
        raise DistributionError(
            f"Distribution {manifest.name} signature does not verify against the supplied "
            "publisher key. The bundle was modified, or the key does not match the publisher.",
            failure_class="signature_invalid") from exc
    except ValueError as exc:
        raise DistributionError(f"Distribution {manifest.name} has an invalid signature: {exc}",
                                failure_class="signature_invalid") from exc
    return True


def _decode_public_key(key: str | bytes) -> bytes:
    """Decode a base64 (or hex) Ed25519 public key into its 32 raw bytes."""
    if isinstance(key, bytes):
        if len(key) == 32:
            return key
        key = key.decode("ascii", errors="ignore")
    text = str(key).strip()
    if not text:
        raise DistributionError("Publisher public key is empty.", failure_class="signature_invalid")
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raw = None
    if raw is None or len(raw) != 32:
        try:
            raw = bytes.fromhex(text)
        except ValueError:
            raw = b""
    if len(raw) != 32:
        raise DistributionError(
            "Publisher public key must be a base64 or hex Ed25519 public key (32 bytes).",
            failure_class="signature_invalid")
    return raw


def _decode_signature(signature: str) -> bytes:
    """Decode a base64 (or hex) Ed25519 signature into its 64 raw bytes."""
    text = str(signature).strip()
    try:
        raw = base64.b64decode(text, validate=True)
    except (binascii.Error, ValueError):
        raw = b""
    if len(raw) != 64:
        try:
            raw = bytes.fromhex(text)
        except ValueError:
            raw = b""
    if len(raw) != 64:
        raise DistributionError(
            "Distribution signature must be a base64 or hex Ed25519 signature (64 bytes).",
            failure_class="signature_invalid")
    return raw


# ── Component integrity digests ────────────────────────────────────────────────────────────

def component_digest(root: Path) -> str:
    """The sha256 digest of a component's file tree at *root*.

    The digest covers every regular file's path and contents, sorted by path,
    so it is stable across filesystems and independent of mtimes. Directories
    that contain no files contribute nothing, and symlinks are hashed by their
    target path so a link cannot smuggle a different tree past the digest.
    """
    digest = hashlib.sha256()
    for path in sorted(_walk_files(root)):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def compute_tree_digest(root: Path) -> str:
    """Alias of :func:`component_digest` named for the packaging direction."""
    return component_digest(root)


def _walk_files(root: Path) -> Iterable[Path]:
    """Every regular file under *root*, sorted, skipping caches and VCS dirs."""
    skip = {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", "node_modules"}
    if not root.is_dir():
        return []
    found: list[Path] = []
    for path in root.rglob("*"):
        if any(part in skip for part in path.relative_to(root).parts):
            continue
        if path.is_symlink():
            # Hash the link's own target text: a symlink pointing outside the
            # tree must not silently change what the tree means.
            found.append(path)
        elif path.is_file():
            found.append(path)
    return sorted(found)


# ── Registry (the curated, human-merged list of distributions) ─────────────────────────────

def get_distribution_registry_dir() -> Path:
    """The ``plugin-distributions/`` directory shipped with this checkout."""
    return Path(__file__).resolve().parent.parent / "plugin-distributions"


@dataclass
class DistributionRegistryEntry:
    """One registry entry: where a distribution's signed manifest lives."""

    name: str                                  # "@scope/name"
    repo: str                                  # git URL holding the manifest
    ref: str                                   # exact 40-char commit SHA of the manifest
    path: str = "distribution.json"            # manifest path within the repo
    publisher_public_key: str = ""             # base64 Ed25519 public key (out of band)
    version: str = ""
    description: str = ""
    maintainer: str = ""

    @property
    def scoped(self) -> Optional[ScopedName]:
        return parse_scoped_name(self.name)


def load_distribution_registry(registry_dir: Optional[Path] = None) -> list[DistributionRegistryEntry]:
    """Every valid registry entry, sorted by name.

    Malformed entries are skipped with a warning rather than raising: one bad
    file must not make the whole registry unusable.
    """
    root = registry_dir or get_distribution_registry_dir()
    if not root.is_dir():
        return []
    entries: list[DistributionRegistryEntry] = []
    for path in sorted(root.glob("*.json")):
        entry = _parse_registry_entry(path)
        if entry is not None:
            entries.append(entry)
    return sorted(entries, key=lambda e: e.name)


def _parse_registry_entry(path: Path) -> Optional[DistributionRegistryEntry]:
    """Parse one registry JSON file; ``None`` (with a warning) when malformed."""
    import logging

    logger = logging.getLogger(__name__)
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Distribution registry: %s: could not read entry: %s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning("Distribution registry: %s: entry must be a mapping", path)
        return None
    name = str(raw.get("name") or "")
    if parse_scoped_name(name) is None:
        logger.warning("Distribution registry: %s: name must be '@scope/name'", path)
        return None
    repo = str(raw.get("repo") or "")
    if not repo:
        logger.warning("Distribution registry: %s: missing 'repo'", path)
        return None
    ref = str(raw.get("ref") or "").strip().lower()
    if not _SHA_RE.fullmatch(ref):
        logger.warning("Distribution registry: %s: 'ref' must be a full 40-character hex commit SHA", path)
        return None
    return DistributionRegistryEntry(
        name=name, repo=repo, ref=ref,
        path=str(raw.get("path") or "distribution.json").strip("/"),
        publisher_public_key=str(raw.get("publisher_public_key") or "").strip(),
        version=str(raw.get("version") or "").strip(),
        description=str(raw.get("description") or "").strip(),
        maintainer=str(raw.get("maintainer") or "").strip())


def resolve_distribution(name: str, registry_dir: Optional[Path] = None) -> DistributionRegistryEntry:
    """Look up a distribution by its scoped name.

    Raises :class:`DistributionError` when the distribution is not published.
    """
    scoped = parse_scoped_name(name)
    if scoped is None:
        raise DistributionError(
            f"'{name}' is not a distribution identifier. A distribution is addressed as "
            "'@scope/name' (for example '@erature/vertical-stack').",
            failure_class="not_found")
    for entry in load_distribution_registry(registry_dir):
        if entry.name == scoped.full:
            return entry
    known = ", ".join(e.name for e in load_distribution_registry(registry_dir)) or "none"
    raise DistributionError(
        f"Distribution '{scoped.full}' is not published. Known distributions: {known}.",
        failure_class="not_found")
