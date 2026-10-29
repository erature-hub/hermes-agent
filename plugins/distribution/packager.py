#!/usr/bin/env python3
"""Packager for Hermes scoped plugin distributions.

Builds, signs, and verifies distribution bundles of the form
``@scope/name``. A distribution is a curated set of plugins, each pinned to an
exact commit SHA, bundled under one author signature so an operator can prove
the whole stack came from its publisher.

Subcommands
-----------
``build``   Assemble a distribution manifest from component directories.
``sign``    Sign a manifest with the publisher's Ed25519 private key.
``verify``  Verify a manifest's signature and per-component digests.
``show``    Print a manifest's contents without verifying.

Signature scheme
----------------
The signature is Ed25519 over the canonical JSON encoding of the manifest with
the ``signature`` field removed (see
``hermes_cli.plugin_distributions.canonical_manifest_bytes``). Canonicalization
is sorted-key, whitespace-free JSON, so the signed bytes are reproducible: the
same manifest always produces the same signature, and a verifier that
re-serializes the manifest gets the same bytes back.

Private keys are read from a file path or from an environment variable. They
are never accepted as a command-line argument, because a command line is
visible to every other process on the host.

Usage
-----
    python plugins/distribution/packager.py build \\
        --name @erature/vertical-stack \\
        --version 1.0.0 \\
        --publisher "Erature" \\
        --component hermes-memory-hindsight:./plugins/memory/hermes-memory-hindsight:plugins/memory/hermes-memory-hindsight \\
        --output distribution.json

    python plugins/distribution/packager.py sign \\
        --manifest distribution.json \\
        --key-file publisher-ed25519.key \\
        --output distribution.json

    python plugins/distribution/packager.py verify \\
        --manifest distribution.json \\
        --publisher-key <base64-public-key>
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from hermes_cli.plugin_distributions import (
    DistributionError,
    DistributionManifest,
    canonical_manifest_bytes,
    component_digest,
    parse_distribution_manifest,
    parse_scoped_name,
    verify_distribution_signature,
)

#: Private keys are read from files or the environment, never argv.
KEY_ENV_VAR = "HERMES_DISTRIBUTION_SIGNING_KEY"


class PackagerError(Exception):
    """A packaging step failed (the CLI exits non-zero with this message)."""


# ── Key handling ───────────────────────────────────────────────────────────────────────────

def _load_private_key(key_file: Optional[str]) -> bytes:
    """Read the 32-byte Ed25519 private key from a file or the environment.

    The key may be raw 32 bytes, hex, or base64. It is never taken from argv.
    """
    material: Optional[bytes] = None
    if key_file:
        path = Path(key_file).expanduser()
        if not path.is_file():
            raise PackagerError(f"Signing key file not found: {path}")
        material = path.read_bytes()
    else:
        env = os.environ.get(KEY_ENV_VAR)
        if not env:
            raise PackagerError(
                f"No signing key supplied. Pass --key-file, or set {KEY_ENV_VAR}.")
        material = env.encode("utf-8")
    return _decode_key_material(material, "private")


def _decode_key_material(material: bytes, kind: str) -> bytes:
    """Decode raw/hex/base64 key material into its canonical raw bytes.

    Ed25519 public and private keys are both 32 raw bytes, so ``kind`` is only
    used to name the material in the error message.
    """
    expected = 32
    text = material.strip()
    if len(text) == expected:
        return text
    try:
        raw = base64.b64decode(text, validate=True)
    except Exception:
        raw = b""
    if len(raw) == expected:
        return raw
    try:
        raw = bytes.fromhex(text.decode("ascii", errors="ignore"))
    except ValueError:
        raw = b""
    if len(raw) != expected:
        raise PackagerError(
            f"The signing key must be {expected} raw bytes, hex, or base64 "
            f"(got {len(text)} characters).")
    return raw


def _load_public_key(value: str) -> bytes:
    """Read a publisher public key from a file path or a literal key string."""
    candidate = Path(value).expanduser()
    if candidate.is_file():
        return _decode_key_material(candidate.read_bytes(), "public")
    return _decode_key_material(value.encode("utf-8"), "public")


# ── Build ──────────────────────────────────────────────────────────────────────────────────

def _parse_component_spec(spec: str) -> tuple[str, Path, str]:
    """Parse ``name:local_path:subdir`` into its three parts.

    *name* is the plugin's manifest name; *local_path* is where its tree lives
    on this machine; *subdir* is the path the component occupies inside the
    distribution repository.
    """
    parts = spec.split(":")
    if len(parts) != 3:
        raise PackagerError(
            f"Component spec {spec!r} must be 'name:local_path:subdir' "
            "(for example 'hermes-memory-hindsight:./plugins/memory/hermes-memory-hindsight:plugins/memory/hermes-memory-hindsight').")
    name, local, subdir = (p.strip() for p in parts)
    if not name or not local or not subdir:
        raise PackagerError(f"Component spec {spec!r} has an empty name, path, or subdir.")
    return name, Path(local).expanduser(), subdir.strip("/")


def build_manifest(
    *,
    name: str,
    version: str,
    publisher: str,
    components: list[tuple[str, Path, str]],
    description: str = "",
    requires_hermes: str = "",
    refs: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
    """Assemble a distribution manifest from local component trees.

    *refs* maps a component name to its exact commit SHA. A component without a
    ref is emitted with an empty ``ref`` and must be filled in before signing —
    the packager refuses to sign a manifest with an unpinned component.
    """
    if parse_scoped_name(name) is None:
        raise PackagerError(f"Distribution name {name!r} must be '@scope/name'.")
    entries: list[dict[str, Any]] = []
    for component_name, local_path, subdir in components:
        if not local_path.is_dir():
            raise PackagerError(f"Component {component_name!r}: {local_path} is not a directory.")
        manifest_file = next(
            (p for p in (local_path / "plugin.yaml", local_path / "plugin.yml") if p.is_file()), None)
        if manifest_file is None and not (local_path / "plugin.json").is_file():
            raise PackagerError(
                f"Component {component_name!r}: {local_path} has no plugin.yaml or plugin.json — "
                "it is not a plugin directory.")
        entries.append({
            "name": component_name,
            "ref": (refs or {}).get(component_name, ""),
            "subdir": subdir,
            "digest": component_digest(local_path),
        })
    manifest: dict[str, Any] = {
        "distribution_version": 1,
        "name": name,
        "version": version,
        "description": description,
        "publisher": publisher,
        "components": entries,
    }
    if requires_hermes:
        manifest["requires_hermes"] = requires_hermes
    return manifest


# ── Sign ───────────────────────────────────────────────────────────────────────────────────

def sign_manifest(manifest: dict[str, Any], private_key: bytes) -> dict[str, Any]:
    """Return a copy of *manifest* carrying an Ed25519 signature.

    The signature covers the canonical encoding of the manifest *without* the
    ``signature`` field, so signing is idempotent over the same content.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    signed = {k: v for k, v in manifest.items() if k != "signature"}
    signed.setdefault("signature_algorithm", "ed25519")
    payload = canonical_manifest_bytes(signed)
    signature = Ed25519PrivateKey.from_private_bytes(private_key).sign(payload)
    signed["signature"] = base64.b64encode(signature).decode("ascii")
    return signed


# ── Verify ─────────────────────────────────────────────────────────────────────────────────

def verify_manifest_file(manifest_path: Path, publisher_key: bytes) -> tuple[DistributionManifest, bool]:
    """Parse and verify a manifest file. Returns the manifest and the verdict."""
    text = manifest_path.read_text(encoding="utf-8-sig")
    manifest = parse_distribution_manifest(text, source=str(manifest_path))
    verify_distribution_signature(manifest, publisher_key)
    return manifest, True


def verify_component_digests(manifest: DistributionManifest, root: Path) -> list[str]:
    """Check each component's declared digest against its tree under *root*.

    Returns the list of components whose digest does NOT match. A component
    with no declared digest is reported as unverifiable rather than assumed
    good — the packager records a digest for every component it builds.
    """
    mismatches: list[str] = []
    for component in manifest.components:
        component_root = root / component.subdir if component.subdir else root
        if not component_root.is_dir():
            mismatches.append(f"{component.name}: missing tree at {component_root}")
            continue
        if not component.digest:
            mismatches.append(f"{component.name}: no digest declared")
            continue
        actual = component_digest(component_root)
        if actual != component.digest:
            mismatches.append(
                f"{component.name}: digest mismatch (expected {component.digest[:12]}…, got {actual[:12]}…)")
    return mismatches


# ── CLI ────────────────────────────────────────────────────────────────────────────────────

def _cmd_build(args: argparse.Namespace) -> int:
    components: list[tuple[str, Path, str]] = []
    for spec in args.component:
        name, local, subdir = _parse_component_spec(spec)
        components.append((name, local, subdir))
    refs: dict[str, str] = {}
    for spec in args.ref or []:
        if "=" not in spec:
            raise PackagerError(f"--ref must be 'name=sha', got {spec!r}.")
        key, value = spec.split("=", 1)
        refs[key.strip()] = value.strip().lower()
    manifest = build_manifest(
        name=args.name, version=args.version, publisher=args.publisher,
        components=components, description=args.description,
        requires_hermes=args.requires_hermes, refs=refs)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {output} ({len(manifest['components'])} component(s)).")
    if args.sign:
        private_key = _load_private_key(args.key_file)
        signed = sign_manifest(manifest, private_key)
        output.write_text(json.dumps(signed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Signed {output} with an Ed25519 publisher key.")
    return 0


def _cmd_sign(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict):
        raise PackagerError(f"{path} must contain a JSON object.")
    private_key = _load_private_key(args.key_file)
    signed = sign_manifest(raw, private_key)
    output = Path(args.output) if args.output else path
    output.write_text(json.dumps(signed, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Signed {output} with an Ed25519 publisher key.")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    publisher_key = _load_public_key(args.publisher_key)
    manifest, _ = verify_manifest_file(path, publisher_key)
    print(f"OK  {manifest.name} v{manifest.version or '(unversioned)'} — signature verified.")
    for component in manifest.components:
        digest = f" sha256:{component.digest[:12]}…" if component.digest else " (no digest)"
        print(f"    - {component.name} @ {component.ref[:12]} {component.subdir or ''}{digest}")
    if args.tree:
        mismatches = verify_component_digests(manifest, Path(args.tree))
        if mismatches:
            for line in mismatches:
                print(f"FAIL  {line}", file=sys.stderr)
            return 1
        print(f"OK  {len(manifest.components)} component digest(s) verified against {args.tree}.")
    return 0


def _cmd_show(args: argparse.Namespace) -> int:
    path = Path(args.manifest)
    manifest = parse_distribution_manifest(path.read_text(encoding="utf-8-sig"), source=str(path))
    print(f"{manifest.name} v{manifest.version or '(unversioned)'}")
    if manifest.description:
        print(f"  {manifest.description}")
    if manifest.publisher:
        print(f"  publisher: {manifest.publisher}")
    print(f"  signature: {'present' if manifest.signature else 'ABSENT'}")
    for component in manifest.components:
        digest = f" sha256:{component.digest[:12]}…" if component.digest else ""
        print(f"    - {component.name} @ {component.ref[:12]} {component.subdir or ''}{digest}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="packager.py", description="Build, sign and verify Hermes plugin distributions.")
    sub = parser.add_subparsers(dest="command", required=True)

    build = sub.add_parser("build", help="Assemble a distribution manifest from component trees.")
    build.add_argument("--name", required=True, help="Distribution name, '@scope/name'.")
    build.add_argument("--version", default="", help="Distribution version.")
    build.add_argument("--publisher", default="", help="Publisher name.")
    build.add_argument("--description", default="", help="Short description.")
    build.add_argument("--requires-hermes", default="", help="Hermes version requirement.")
    build.add_argument("--component", action="append", default=[],
                       help="Component as 'name:local_path:subdir' (repeatable).")
    build.add_argument("--ref", action="append", default=[],
                       help="Component pin as 'name=<40-hex sha>' (repeatable).")
    build.add_argument("--output", required=True, help="Where to write the manifest.")
    build.add_argument("--sign", action="store_true", help="Sign the manifest after writing it.")
    build.add_argument("--key-file", default=None, help=f"Ed25519 private key file (or ${KEY_ENV_VAR}).")
    build.set_defaults(func=_cmd_build)

    sign = sub.add_parser("sign", help="Sign a manifest with the publisher key.")
    sign.add_argument("--manifest", required=True, help="Manifest to sign.")
    sign.add_argument("--key-file", default=None, help=f"Ed25519 private key file (or ${KEY_ENV_VAR}).")
    sign.add_argument("--output", default=None, help="Output path (defaults to --manifest).")
    sign.set_defaults(func=_cmd_sign)

    verify = sub.add_parser("verify", help="Verify a manifest's signature and digests.")
    verify.add_argument("--manifest", required=True, help="Manifest to verify.")
    verify.add_argument("--publisher-key", required=True,
                        help="Publisher public key (base64/hex) or a path to a file holding it.")
    verify.add_argument("--tree", default=None, help="Directory holding the component trees to digest-check.")
    verify.set_defaults(func=_cmd_verify)

    show = sub.add_parser("show", help="Print a manifest without verifying it.")
    show.add_argument("--manifest", required=True, help="Manifest to show.")
    show.set_defaults(func=_cmd_show)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (PackagerError, DistributionError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
