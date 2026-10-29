"""Bank namespacing for the sovereign Hindsight provider.

The sovereign namespace is two-shaped:

* ``hermes``            — the shared personal substrate (the ``hermes`` primary alias).
* ``erature/<owner>``   — a per-owner namespace, e.g. ``erature/paul``.

Hindsight's REST surface addresses a bank by a **path segment**, and the router does not
decode ``%2F`` into a bank id — a slash inside the path segment 404s. But the *alias*
surface is body-driven and accepts a slash. So the slash form is carried as an **alias**
and every call is issued against the canonical bank id the alias resolves to.

This module owns that translation and nothing else: it never issues memory reads/writes,
it only decides which canonical bank id a logical namespace means.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Any

from ._client import HindsightClient, HindsightError

logger = logging.getLogger(__name__)

#: The shared personal namespace.
DEFAULT_NAMESPACE = "hermes"
#: The per-owner namespace prefix. ``erature/<owner>``.
OWNER_NAMESPACE_PREFIX = "erature"

#: A bank id must be non-empty, <=192 bytes of UTF-8, and free of control characters.
_MAX_BANK_ID_BYTES = 192
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class NamespaceError(ValueError):
    """The requested namespace is not a well-formed bank namespace."""


def normalize_namespace(raw: str) -> str:
    """Validate and canonicalize a logical namespace string.

    Accepts ``hermes``, ``erature/<owner>`` and bare ``<owner>`` (promoted to
    ``erature/<owner>``). Rejects anything that could not be a bank id, rather than
    letting it reach the substrate and fail as an opaque 404.
    """
    value = (raw or "").strip()
    if not value:
        raise NamespaceError("memory namespace is empty")
    if _CONTROL_CHARS.search(value):
        raise NamespaceError("memory namespace contains control characters")
    if len(value.encode("utf-8")) > _MAX_BANK_ID_BYTES:
        raise NamespaceError("memory namespace exceeds the 192-byte bank id limit")
    if value == DEFAULT_NAMESPACE:
        return DEFAULT_NAMESPACE
    if "/" in value:
        prefix, _, owner = value.partition("/")
        if prefix != OWNER_NAMESPACE_PREFIX:
            raise NamespaceError(
                f"unknown namespace {value!r}: expected {DEFAULT_NAMESPACE!r} or "
                f"{OWNER_NAMESPACE_PREFIX}/<owner>"
            )
        if not owner.strip():
            raise NamespaceError(f"namespace {value!r} has an empty owner segment")
        if "/" in owner:
            raise NamespaceError(f"namespace {value!r} has more than one owner segment")
        return f"{OWNER_NAMESPACE_PREFIX}/{owner.strip()}"
    # A bare owner name is the ergonomic form of the per-owner namespace.
    return f"{OWNER_NAMESPACE_PREFIX}/{value}"


def owner_of(namespace: str) -> str:
    """The owner segment of a namespace (``""`` for the shared ``hermes`` namespace)."""
    value = (namespace or "").strip()
    if value == DEFAULT_NAMESPACE:
        return ""
    prefix, _, owner = value.partition("/")
    return owner.strip() if prefix == OWNER_NAMESPACE_PREFIX else ""


def owner_bank_id(namespace: str) -> str:
    """The dedicated bank id an owner namespace is provisioned under.

    A bank id is a REST path segment, so it cannot contain a slash — the owner
    namespace's slash form is an ALIAS on this id, and the alias is what the
    namespace resolves to.

    The id is derived from the owner segment so it is stable and human-readable
    (``erature/paul`` -> ``erature-paul``). When sanitizing the owner segment
    would make it lossy (``pa ul`` -> ``pa-ul``, which would collide with the
    distinct owner ``pa-ul``), a short digest of the ORIGINAL segment is appended
    so two owners can never be provisioned onto one bank.
    """
    owner = owner_of(namespace)
    if not owner:
        return ""
    sanitized = re.sub(r"[^A-Za-z0-9_-]+", "-", owner).strip("-_")
    if not sanitized:
        sanitized = "owner"
    if sanitized != owner:
        digest = hashlib.sha256(owner.encode("utf-8")).hexdigest()[:8]
        sanitized = f"{sanitized}-{digest}"
    return f"{OWNER_NAMESPACE_PREFIX}-{sanitized}"


class BankResolver:
    """Resolves a logical namespace to the canonical bank id that addresses it.

    Resolution order for a namespace ``N``:

    1. If a bank whose own id is exactly ``N`` exists, use it.
    2. If some bank carries ``N`` as an alias, use that bank's canonical id.
    3. Otherwise fall back to the shared ``hermes`` namespace's canonical bank.

    The mapping is cached per resolver instance; a resolver is bound to one profile.
    """

    def __init__(self, client: HindsightClient, *, default_namespace: str = DEFAULT_NAMESPACE) -> None:
        self._client = client
        self.default_namespace = default_namespace
        self._canonical_by_namespace: dict[str, str] = {}
        self._canonical_by_bank: dict[str, str] = {}

    # -- internals --------------------------------------------------------------

    def _refresh_index(self) -> None:
        """Rebuild the alias -> canonical-bank index from the live bank list."""
        try:
            banks = self._client.list_banks(limit=1000)
        except HindsightError as exc:
            logger.debug("hindsight bank index refresh failed: %s", exc)
            return
        by_namespace: dict[str, str] = {}
        by_bank: dict[str, str] = {}
        for bank in banks:
            if not isinstance(bank, dict):
                continue
            bank_id = str(bank.get("bank_id") or "").strip()
            if not bank_id:
                continue
            by_bank[bank_id] = bank_id
            # A bank's own id is also a valid namespace for it.
            by_namespace.setdefault(bank_id, bank_id)
            display_alias = bank.get("display_alias")
            if isinstance(display_alias, str) and display_alias.strip():
                by_namespace.setdefault(display_alias.strip(), bank_id)
            for alias in bank.get("matched_aliases") or []:
                if isinstance(alias, str) and alias.strip():
                    by_namespace.setdefault(alias.strip(), bank_id)
        # The aliases endpoint is authoritative for aliases the list view omits.
        for bank_id in list(by_bank):
            try:
                payload = self._client.bank_aliases(bank_id)
            except HindsightError:
                continue
            for entry in payload.get("aliases") or []:
                if not isinstance(entry, dict):
                    continue
                alias = str(entry.get("alias") or "").strip()
                if alias:
                    by_namespace.setdefault(alias, bank_id)
        self._canonical_by_namespace.update(by_namespace)
        self._canonical_by_bank.update(by_bank)

    def _discover_via_query(self, namespace: str) -> str | None:
        """Find the bank carrying ``namespace`` as an alias, via the bank list.

        The ``q`` filter is a substring match over bank id, name and aliases, so a
        SLASH alias is discoverable here even though no per-alias REST route can
        address one (the router will not decode ``%2F`` into a path segment).
        """
        try:
            banks = self._client.list_banks(query=namespace, limit=1000)
        except HindsightError as exc:
            logger.debug("hindsight namespace query failed: %s", exc)
            return None
        for bank in banks:
            if not isinstance(bank, dict):
                continue
            bank_id = str(bank.get("bank_id") or "").strip()
            if not bank_id:
                continue
            matched = {a.strip() for a in (bank.get("matched_aliases") or []) if isinstance(a, str)}
            if namespace in matched:
                return bank_id
            if bank_id == namespace:
                return bank_id
            display_alias = bank.get("display_alias")
            if isinstance(display_alias, str) and display_alias.strip() == namespace:
                return bank_id
        return None

    # -- public API -------------------------------------------------------------

    def canonical_bank(self, namespace: str) -> str:
        """Canonical bank id addressing ``namespace`` (never raises for a missing bank)."""
        wanted = normalize_namespace(namespace)
        cached = self._canonical_by_namespace.get(wanted)
        if cached:
            return cached
        self._refresh_index()
        found = self._canonical_by_namespace.get(wanted)
        if found:
            return found
        # A namespace that is not in the full list may still be an alias the list view
        # only reports when the query matches it — the only way to see a slash alias.
        discovered = self._discover_via_query(wanted)
        if discovered:
            self._canonical_by_namespace[wanted] = discovered
            return discovered
        # The namespace itself may be a real bank id that the list view did not report
        # (e.g. a freshly created bank). Probe it directly before giving up.
        try:
            bank = self._client.get_bank(wanted)
        except HindsightError:
            bank = {}
        if isinstance(bank, dict) and bank.get("bank_id"):
            canonical = str(bank["bank_id"])
            self._canonical_by_namespace[wanted] = canonical
            return canonical
        # Unknown namespace: fall back to the shared namespace so a mis-typed owner
        # degrades to the shared substrate rather than losing memory silently.
        fallback = self._canonical_by_namespace.get(self.default_namespace)
        if fallback:
            logger.warning(
                "hindsight namespace %r is not provisioned; using the shared %r bank",
                wanted,
                self.default_namespace,
            )
            return fallback
        return wanted

    def ensure_namespace(self, namespace: str) -> str:
        """Resolve ``namespace`` and make it addressable.

        The shared ``hermes`` namespace addresses the shared bank as-is.

        An owner namespace gets its OWN bank (``erature/paul`` -> ``erature-paul``),
        because the slash form cannot be a REST path segment and therefore cannot be a
        bank id. The slash form is registered as an ALIAS on that dedicated bank, so
        the namespace stays addressable and the owner's memories stay SEPARATE from
        the shared substrate and from every other owner's.

        Provisioning is idempotent: an existing bank (or an existing alias pointing
        at one) is reused, never recreated.
        """
        wanted = normalize_namespace(namespace)
        owner = owner_of(wanted)
        if not owner:
            return self.canonical_bank(wanted)

        dedicated = owner_bank_id(wanted)
        # An alias for this namespace may already exist on some bank (it was
        # provisioned earlier, or by hand) — that bank is the owner's, and it wins.
        existing = self._canonical_by_namespace.get(wanted)
        if not existing:
            self._refresh_index()
            existing = self._canonical_by_namespace.get(wanted)
        if not existing:
            discovered = self._discover_via_query(wanted)
            if discovered:
                existing = discovered
        if existing:
            self._canonical_by_namespace[wanted] = existing
            return existing

        # No bank carries this namespace yet: provision the dedicated one, then
        # register the slash form as its alias.
        try:
            self._client.ensure_bank(dedicated, name=f"Erature / {owner}")
        except HindsightError as exc:
            logger.warning("hindsight: could not provision bank %r for %r: %s", dedicated, wanted, exc)
            fallback = self.canonical_bank(wanted)
            self._canonical_by_namespace[wanted] = fallback
            return fallback
        try:
            self._client.add_bank_alias(dedicated, wanted)
            logger.info(
                "hindsight: provisioned namespace %r as bank %r", wanted, dedicated
            )
        except HindsightError as exc:
            # The alias could not be registered (e.g. it is already taken by another
            # bank). The dedicated bank still addresses this namespace, so keep it —
            # losing the write would be worse than an undiscoverable alias.
            logger.debug("hindsight: alias write for %r failed: %s", wanted, exc)
        self._canonical_by_namespace[wanted] = dedicated
        self._canonical_by_bank[dedicated] = dedicated
        return dedicated

    def namespaces(self) -> list[str]:
        """Every namespace currently known to the substrate (bank ids + aliases)."""
        self._refresh_index()
        return sorted(self._canonical_by_namespace)

    def bank_snapshot(self, bank_id: str) -> dict[str, Any]:
        try:
            return self._client.get_bank(bank_id)
        except HindsightError:
            return {}
