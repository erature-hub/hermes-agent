"""Installation orchestration for scoped plugin distributions.

The distribution format (see ``hermes_cli/plugin_distributions.py``) is
validated there; this module turns a *verified* manifest into installs.

The install sequence is deliberately layered so that every existing
fail-closed gate still applies to every component:

1. **Resolve** the scoped name against the distribution registry.
2. **Fetch** the manifest from the registry's pinned commit.
3. **Verify** the author signature against the registry's publisher key. An
   unsigned or wrongly-signed bundle is refused before anything is installed.
4. **Verify** each component's content digest against the fetched tree.
5. **Review** — a mandatory screen listing every component, its pin, its
   declared capabilities, and its sandbox requirement. The operator sees the
   whole bundle before the first byte lands.
6. **Install** each component through the ordinary pinned-install path, in
   dependency order, with the same per-plugin capability consent and the same
   security scan a single-plugin install gets. A distribution never bulk-grants
   capability consent.
7. **Activate** — components are enabled only after their dependencies are
   installed, so an activation order violation cannot leave a half-wired stack.

A failure in one component does not abort the others: the fan-out reports
per-component outcomes and the command exits non-zero if any component failed.
That mirrors ``hermes plugins pack`` and keeps one bad component from
half-installing a stack in an unreported way.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from hermes_cli.plugin_distributions import (
    DistributionComponent,
    DistributionError,
    DistributionManifest,
    DistributionRegistryEntry,
    component_digest,
    parse_distribution_manifest,
    resolve_distribution,
    verify_distribution_signature,
)

logger = logging.getLogger(__name__)


@dataclass
class DistributionComponentResult:
    """Outcome of one component install inside a distribution."""

    component: DistributionComponent
    ok: bool
    installed_name: Optional[str] = None
    error: Optional[str] = None
    digest_ok: bool = True


@dataclass
class DistributionInstallResult:
    """Outcome of a whole distribution install."""

    name: str
    results: list[DistributionComponentResult] = field(default_factory=list)
    manifest: Optional[DistributionManifest] = None

    @property
    def ok(self) -> bool:
        """True when every component installed successfully."""
        return bool(self.results) and all(r.ok for r in self.results)

    @property
    def installed(self) -> list[str]:
        """The names of the components that installed successfully."""
        return [r.installed_name for r in self.results if r.ok and r.installed_name]

    @property
    def failed(self) -> list[DistributionComponentResult]:
        """The components that failed to install."""
        return [r for r in self.results if not r.ok]


# ── Manifest fetching ──────────────────────────────────────────────────────────────────────

def _git_clone_source(repo: str) -> str:
    """The argument to pass to ``git clone`` for *repo*.

    A ``file://`` URL is rewritten to the ``file://<native path>`` form that
    git accepts on this host. Two details matter:

    * On Windows, git resolves the RFC form ``file:///C:/repo`` as the POSIX
      path ``/C:/repo``, which does not exist, so the clone fails with "does
      not appear to be a git repository". The native path (``C:\\repo``)
      clones correctly.
    * The ``file://`` SCHEME IS KEPT. The component install path resolves the
      identifier through the core ``_resolve_git_url``, which passes ``file://``
      references through verbatim; a bare local path would instead be parsed
      as ``owner/repo`` shorthand and turned into a GitHub URL.

    A local path is used verbatim; every other reference (https, ssh,
    ``owner/repo``) is left alone.
    """
    text = str(repo).strip()
    if not text.startswith("file://"):
        return text

    body = text[len("file://"):]
    # Strip a query/fragment — neither is meaningful for a local repository.
    body = body.split("#", 1)[0].split("?", 1)[0]

    # ``file://C:\\path`` puts the WHOLE path in the netloc (urlparse cannot
    # tell a drive letter from a host). ``file:///C:/path`` puts it in the
    # path. Handle both shapes explicitly rather than through urlparse.
    if body.startswith("/"):
        path = body
    else:
        path = "/" + body
    if os.name == "nt":
        path = path.replace("/", "\\")
        if len(path) > 2 and path[0] == "\\" and path[2] == ":":
            path = path[1:]
    else:
        path = path.replace("\\", "/")
    if path.startswith("localhost/"):
        path = path[len("localhost"):]
    return f"file://{path}"


def _component_install_identifier(component: DistributionComponent) -> Optional[str]:
    """The install identifier for *component*, with a cloneable repository.

    A ``file://`` repository is normalized to the native-path form git can
    clone on this host; every other repository is passed through unchanged.
    """
    identifier = component.install_identifier
    if identifier is None:
        return None
    repo, sep, subdir = identifier.partition("#")
    source = _git_clone_source(repo)
    if not sep:
        return source
    return f"{source}#{subdir}"


def fetch_distribution_manifest(
    entry: DistributionRegistryEntry,
    *,
    timeout: int = 300,
) -> tuple[DistributionManifest, Path]:
    """Fetch and parse a distribution manifest at the registry's pinned commit.

    Returns the manifest and the temporary clone root (the caller owns it and
    must remove it). The clone is blobless and sparse — only the manifest's
    directory is fetched — so a large repository costs a manifest-sized
    download.
    """
    git = shutil.which("git")
    if not git:
        raise DistributionError("git is not installed or not in PATH.", failure_class="git_missing")
    tmp = Path(tempfile.mkdtemp(prefix="hermes-distro-"))
    try:
        clone = tmp / "distro"
        args = ["clone", "--depth", "1", "--filter=blob:none", "--no-checkout",
                _git_clone_source(entry.repo), str(clone)]
        result = subprocess.run(
            [git, *args], capture_output=True, text=True, timeout=timeout, check=False)
        if result.returncode != 0:
            raise DistributionError(
                f"Could not clone distribution repository {entry.repo}: "
                f"{result.stderr.strip() or result.stdout.strip()}",
                failure_class="clone_failed")
        # Fetch the exact pinned commit and check it out detached.
        for verb, verb_args in (
            ("fetch", ["fetch", "--depth", "1", "origin", entry.ref]),
            ("checkout", ["checkout", "--detach", entry.ref]),
        ):
            verb_result = subprocess.run(
                [git, *verb_args], cwd=clone, capture_output=True, text=True, timeout=timeout, check=False)
            if verb_result.returncode != 0:
                raise DistributionError(
                    f"Git {verb} of distribution commit {entry.ref} failed: "
                    f"{verb_result.stderr.strip() or verb_result.stdout.strip()}",
                    failure_class="clone_failed")
        head = subprocess.run(
            [git, "rev-parse", "HEAD"], cwd=clone, capture_output=True, text=True,
            timeout=timeout, check=False).stdout.strip()
        if head != entry.ref:
            raise DistributionError(
                f"Checked-out distribution revision {head!r} does not match the pinned {entry.ref!r}.",
                failure_class="clone_failed")
        manifest_path = clone / entry.path
        if not manifest_path.is_file():
            raise DistributionError(
                f"Distribution manifest {entry.path!r} not found at the pinned commit {entry.ref[:12]}.",
                failure_class="manifest_invalid")
        text = manifest_path.read_text(encoding="utf-8-sig")
        manifest = parse_distribution_manifest(text, source=f"{entry.name}@{entry.ref[:12]}")
        return manifest, clone
    except subprocess.TimeoutExpired as exc:
        raise DistributionError(f"Fetching distribution {entry.name} timed out after {timeout} seconds.",
                                failure_class="clone_failed") from exc
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


# ── Dependency ordering ───────────────────────────────────────────────────────────────────

def order_components(manifest: DistributionManifest) -> list[DistributionComponent]:
    """Order components so every component follows the ones it requires.

    Ties are broken by declaration order, so the sequence is deterministic.
    A dependency cycle is refused: a cycle means no valid activation order
    exists, and installing one anyway would produce a stack that half-loads.
    """
    by_name = {component.name: component for component in manifest.components}
    ordered: list[DistributionComponent] = []
    visited: set[str] = set()
    in_progress: set[str] = set()

    def visit(name: str, trail: tuple[str, ...]) -> None:
        if name in visited:
            return
        if name in in_progress:
            cycle = " -> ".join((*trail, name))
            raise DistributionError(
                f"Distribution {manifest.name} has a dependency cycle: {cycle}. "
                "A valid activation order does not exist.",
                failure_class="manifest_invalid")
        component = by_name.get(name)
        if component is None:
            raise DistributionError(
                f"Distribution {manifest.name} references unknown component {name!r}.",
                failure_class="manifest_invalid")
        in_progress.add(name)
        for dependency in component.requires:
            visit(dependency, (*trail, name))
        in_progress.discard(name)
        visited.add(name)
        ordered.append(component)

    for component in manifest.components:
        visit(component.name, ())
    return ordered


# ── Review screen ──────────────────────────────────────────────────────────────────────────

def render_distribution_review(console, manifest: DistributionManifest, entry: DistributionRegistryEntry) -> None:
    """Print the mandatory review screen shown before anything is installed."""
    from rich.table import Table

    console.print(f"\n[bold]{manifest.name}[/bold]" + (f" v{manifest.version}" if manifest.version else ""))
    if manifest.description:
        console.print(f"[dim]{manifest.description}[/dim]")
    if manifest.publisher:
        console.print(f"[dim]Publisher: {manifest.publisher}[/dim]")
    console.print(f"[dim]Pinned at {entry.repo} @ {entry.ref[:12]}[/dim]")

    table = Table(show_header=True, header_style="bold")
    table.add_column("Component")
    table.add_column("Role")
    table.add_column("Source")
    table.add_column("Pinned ref")
    table.add_column("Requires")
    table.add_column("Declared capabilities")
    table.add_column("Digest")
    for component in manifest.components:
        table.add_row(
            # The component's own name, so the operator can tell the members of
            # the stack apart. The source column carries the repository it
            # comes from; ``display`` alone is a repo/subdir path that renders
            # as four indistinguishable truncated strings.
            component.name,
            component.role or "—",
            component.repo or "(catalog name)",
            component.ref[:12],
            ", ".join(component.requires) or "—",
            ", ".join(component.capabilities) or "(shown at install)",
            f"sha256:{component.digest[:12]}…" if component.digest else "(none)",
        )
    console.print(table)

    from hermes_cli.plugin_isolation import ISOLATION_HOST, isolation_mode

    console.print(
        f"\n[dim]Sandbox: components run under plugins.isolation="
        f"{isolation_mode()} (host = {ISOLATION_HOST} — third-party code never runs inside "
        "the Hermes process).[dim]")
    console.print(
        f"\n[dim]Installing a distribution runs third-party code × {len(manifest.components)} plugins. "
        "Each plugin's declared capabilities still require individual consent after install — "
        "a distribution never bulk-grants.[dim]")


# ── Install fan-out ────────────────────────────────────────────────────────────────────────

def _refuse_if_component_removed(
    component: DistributionComponent,
    name: Optional[str],
    repo: Optional[str],
) -> None:
    """Refuse a component that the catalog kill list has recalled.

    The kill list is a security control: a plugin removed from the catalog for
    a security or policy reason must not keep being installed. A distribution
    is a bundle of third-party plugins, so it must not become a path around a
    control the single-plugin install applies. The check matches the catalog
    name and the repo URL, exactly as ``raise_if_removed`` does.

    Raises :class:`DistributionError` (recorded as a failed component by the
    fan-out) when the component is on the list.
    """
    from hermes_cli.plugin_catalog import find_removed

    for candidate in (name, repo):
        if not candidate:
            continue
        removed = find_removed(candidate)
        if removed is not None:
            detail = removed.reason or "no reason recorded"
            if removed.date:
                detail += f" (removed {removed.date})"
            raise DistributionError(
                f"Component {component.display!r} was removed from the Hermes plugin catalog "
                f"and is blocked from installation: {detail}. Reinstall with "
                "--allow-removed if you trust it.",
                failure_class="removed_from_catalog")


def _make_digest_verifier(component: DistributionComponent):
    """Build the ``tree_verifier`` callback that enforces a component's signed digest.

    The callback receives the pristine cloned tree and raises to refuse the
    install when its bytes do not hash to the digest the publisher signed. A
    component with no declared digest is NOT verified — the publisher's manifest
    decides which components carry a digest, and an unsigned tree is refused
    rather than waved through.
    """
    if not component.digest:
        def _refuse_unverifiable(tree: Path) -> None:
            raise DistributionError(
                f"Component {component.name!r} has no content digest in the signed manifest; "
                "its bytes cannot be verified. Refusing the install.",
                failure_class="integrity_failed")

        return _refuse_unverifiable

    expected = component.digest

    def _verify(tree: Path) -> None:
        actual = component_digest(tree)
        if actual != expected:
            raise DistributionError(
                f"Component {component.name!r} failed content verification: expected sha256 "
                f"{expected[:12]}…, got {actual[:12]}…. The fetched bytes do not match the "
                "publisher's signed manifest. Refusing the install.",
                failure_class="integrity_failed")

    return _verify


def install_distribution_components(
    manifest: DistributionManifest,
    components: list[DistributionComponent],
    console,
    *,
    force: bool = False,
    allow_removed: bool = False,
    enable: Optional[bool] = None,
    no_deps: bool = False,
    yes_deps: bool = False,
) -> list[DistributionComponentResult]:
    """Install each component through the ordinary pinned-install path.

    Mirrors ``install_pack_plugins``: per-plugin capability consent, the same
    security scan, the same enable path, and no bulk consent. A failure in one
    component is recorded and the fan-out continues, so the operator sees the
    complete picture rather than the first error.

    *allow_removed* bypasses the catalog kill list for every component, exactly
    as ``--allow-removed`` does for a single-plugin install.

    *enable* is the tri-state of ``--enable``/``--no-enable``: ``None`` (neither
    given) enables the components, as a pack install does; ``False`` installs
    them and leaves them disabled.

    *no_deps* skips dependency consent and leaves the components disabled;
    *yes_deps* carries the operator's own consent so a headless install is not
    refused non-interactively. Both mirror the single-plugin install flags.
    """
    from hermes_cli.plugins_cmd import (
        PluginOperationError,
        _declared_capabilities_from_manifest,
        _install_plugin_core,
        _prompt_plugin_env_vars,
        _run_capability_consent,
        _set_plugin_enabled,
    )
    from hermes_cli.plugins_admission import AdmissionRefused
    from hermes_cli.plugins_cmd_install import recorded_install

    results: list[DistributionComponentResult] = []
    should_enable = enable is not False and not no_deps

    for component in components:
        identifier = component.install_identifier
        catalog_match = None
        if identifier is None:
            # A bare name needs the plugin catalog to resolve; the catalog
            # resolves it the same way a pack's bare name does.
            from hermes_cli.plugin_catalog import load_catalog_live

            try:
                match = next((e for e in load_catalog_live() if e.name == component.name), None)
            except Exception as exc:  # a catalog outage must not abort the fan-out
                results.append(DistributionComponentResult(
                    component=component, ok=False,
                    error=f"plugin catalog unavailable: {exc}"))
                continue
            if match is None:
                results.append(DistributionComponentResult(
                    component=component, ok=False, error="not found in the plugin catalog"))
                continue
            identifier = match.install_identifier
            if identifier is None:
                results.append(DistributionComponentResult(
                    component=component, ok=False,
                    error="catalog entry has no installable source"))
                continue
            catalog_match = match

        if not allow_removed:
            # The kill list is a security control: a plugin recalled from the
            # catalog must not keep being installed, and a distribution must not
            # be a way around a control the single-plugin install applies. The
            # match candidates are the catalog entry's own name/repo for a
            # bare-name component, and the component's own name/repo otherwise.
            candidates = (
                (catalog_match.name, catalog_match.repo)
                if catalog_match is not None
                else (component.name, component.repo)
            )
            try:
                _refuse_if_component_removed(component, *candidates)
            except DistributionError as exc:
                # A recalled component fails on its own; the rest of the bundle
                # still installs, and the command exits non-zero.
                results.append(DistributionComponentResult(
                    component=component, ok=False, error=str(exc)))
                console.print(f"[red]✗[/red] {component.display}: {exc}")
                continue

        install_identifier: str = _component_install_identifier(component) or identifier
        console.print(f"[dim]Installing {component.display} @ {component.ref[:12]}...[/dim]")
        try:
            _target, installed_manifest, installed_name = recorded_install(
                lambda: _install_plugin_core(
                    install_identifier, force=force, ref=component.ref,
                    python_deps=not no_deps, assume_deps_consent=yes_deps,
                    allow_removed=allow_removed,
                    tree_verifier=_make_digest_verifier(component)),
                catalog_name=None if component.repo else component.name, identifier=install_identifier)
        except PluginOperationError as exc:
            results.append(DistributionComponentResult(component=component, ok=False, error=str(exc)))
            console.print(f"[red]✗[/red] {component.display}: {exc}")
            continue
        except Exception as exc:
            logger.exception("distribution install failed for %s", component.display)
            results.append(DistributionComponentResult(component=component, ok=False, error=str(exc)))
            console.print(f"[red]✗[/red] {component.display}: {exc}")
            continue

        try:
            _prompt_plugin_env_vars(installed_manifest, console)
        except Exception:
            logger.debug("requires_env prompt failed for %s", installed_name, exc_info=True)

        if should_enable:
            try:
                _set_plugin_enabled(installed_name, enable=True)
            except AdmissionRefused as exc:
                results.append(DistributionComponentResult(
                    component=component, ok=False, installed_name=installed_name, error=str(exc)))
                console.print(f"[red]✗[/red] {component.display}: {exc}")
                continue

        declared = _declared_capabilities_from_manifest(installed_manifest, installed_name)
        if declared:
            _run_capability_consent(console, installed_name, declared, context="install")

        console.print(f"[green]✓[/green] {component.display} installed as [bold]{installed_name}[/bold].")
        results.append(DistributionComponentResult(
            component=component, ok=True, installed_name=installed_name))
    return results


# ── Digest verification ────────────────────────────────────────────────────────────────────

def verify_component_digests(
    manifest: DistributionManifest,
    tree_root: Path,
    *,
    distribution_repo: Optional[str] = None,
) -> list[str]:
    """Check the components hosted IN the distribution repository against the fetched tree.

    A component may live in the distribution's own repository or in its own
    separate repository. Only the former can be checked here, because only its
    tree is present under *tree_root*. Components hosted elsewhere are verified
    at install time by the ``tree_verifier`` seam, which runs on the pristine
    clone of that component's own repository — before the security scan and
    before anything is published.

    *distribution_repo* is the repository the manifest was fetched from; when
    given, a component that names it is checked against *tree_root*.

    Returns the list of components that did not verify. A component with no
    declared digest is reported as unverifiable rather than assumed good.
    """
    mismatches: list[str] = []
    for component in manifest.components:
        if distribution_repo is not None and not _same_repo(component.repo, distribution_repo):
            # Hosted in its own repository: verified at install time instead.
            continue
        component_root = tree_root / component.subdir if component.subdir else tree_root
        if not component_root.is_dir():
            mismatches.append(f"{component.name}: missing tree at {component_root}")
            continue
        if not component.digest:
            mismatches.append(f"{component.name}: no digest declared")
            continue
        actual = component_digest(component_root)
        if actual != component.digest:
            mismatches.append(
                f"{component.name}: digest mismatch (expected {component.digest[:12]}…, "
                f"got {actual[:12]}…)")
    return mismatches


def _same_repo(component_repo: Optional[str], distribution_repo: str) -> bool:
    """True when *component_repo* names the same repository as *distribution_repo*.

    Compares the normalized ``owner/repo`` form so ``erature-hub/hermes-agent``,
    ``https://github.com/erature-hub/hermes-agent.git`` and a local path all
    compare correctly against each other.
    """
    if not component_repo:
        return False
    left = _normalize_repo(component_repo)
    right = _normalize_repo(distribution_repo)
    return bool(left) and left == right


def _normalize_repo(repo: str) -> str:
    """Normalize a repository reference to a comparable ``owner/repo`` string."""
    text = str(repo).strip()
    if text.startswith("http://") or text.startswith("https://"):
        text = text.split("://", 1)[1]
        text = text.removeprefix("www.")
        parts = text.split("/")
        if len(parts) >= 2:
            text = "/".join(parts[:2])
    elif text.startswith("git@") and ":" in text:
        text = text.split(":", 1)[1]
    text = text.removesuffix(".git").strip("/")
    return text.lower()


# ── Top-level entry point ──────────────────────────────────────────────────────────────────

def _refuse_if_hermes_too_old(manifest: DistributionManifest) -> None:
    """Refuse a distribution whose ``requires_hermes`` floor rejects this Hermes.

    The manifest declares the Hermes range it was built for. Installing it on a
    Hermes outside that range produces a half-working stack — components whose
    APIs do not exist yet — so the bundle is refused up front, before any
    component is installed.

    Raises :class:`DistributionError` when the running version is too old.
    """
    if not manifest.requires_hermes:
        return
    from hermes_cli.plugins_manifest import requires_hermes_error

    reason = requires_hermes_error({"requires_hermes": manifest.requires_hermes})
    if reason:
        raise DistributionError(
            f"Distribution {manifest.name} {reason}. Update Hermes and install again.",
            failure_class="incompatible")


def install_distribution(
    name: str,
    console,
    *,
    force: bool = False,
    publisher_key: Optional[str] = None,
    registry_dir: Optional[Path] = None,
    skip_digest_check: bool = False,
    allow_removed: bool = False,
    enable: Optional[bool] = None,
    no_deps: bool = False,
    yes_deps: bool = False,
) -> DistributionInstallResult:
    """Install a whole distribution by its scoped name.

    *publisher_key* overrides the registry's publisher key (base64 or hex). It
    exists so an operator can verify against a key they obtained out of band
    rather than the one the registry ships.

    *allow_removed* bypasses the catalog kill list for every component, the
    same way ``--allow-removed`` does for a single-plugin install.

    *enable* is the tri-state of ``--enable``/``--no-enable``; *no_deps* and
    *yes_deps* mirror the single-plugin install flags of the same name.

    The whole sequence is fail-closed: a missing distribution, an unsigned
    manifest, a bad signature, a digest mismatch, or a Hermes version the
    distribution does not support all abort before any component is installed.
    """
    entry = resolve_distribution(name, registry_dir)
    key = publisher_key or entry.publisher_public_key
    if not key:
        raise DistributionError(
            f"Distribution {entry.name} has no publisher key in the registry. Supply one with "
            "--publisher-key, or add 'publisher_public_key' to the registry entry.",
            failure_class="signature_invalid")

    manifest, clone_root = fetch_distribution_manifest(entry)
    try:
        verify_distribution_signature(manifest, key)
        console.print(f"[green]✓[/green] Signature verified for [bold]{manifest.name}[/bold].")

        # The manifest's own version floor is a fail-closed gate: a bundle that
        # needs a newer Hermes must not half-install on an older one.
        _refuse_if_hermes_too_old(manifest)

        if not skip_digest_check:
            mismatches = verify_component_digests(
                manifest, clone_root, distribution_repo=entry.repo)
            if mismatches:
                for line in mismatches:
                    console.print(f"[red]✗[/red] {line}")
                raise DistributionError(
                    f"Distribution {manifest.name} failed content verification; nothing was installed.",
                    failure_class="integrity_failed")

        components = order_components(manifest)
        render_distribution_review(console, manifest, entry)

        results = install_distribution_components(
            manifest, components, console, force=force, allow_removed=allow_removed,
            enable=enable, no_deps=no_deps, yes_deps=yes_deps)
        return DistributionInstallResult(name=manifest.name, results=results, manifest=manifest)
    finally:
        shutil.rmtree(clone_root.parent, ignore_errors=True)


def distribution_summary(manifest: DistributionManifest) -> str:
    """A one-line human summary of a distribution, used by ``plugins list``."""
    parts = [f"{component.name}@{component.ref[:8]}" for component in manifest.components]
    return f"{manifest.name} v{manifest.version or '?'} — {len(manifest.components)} component(s): {', '.join(parts)}"
