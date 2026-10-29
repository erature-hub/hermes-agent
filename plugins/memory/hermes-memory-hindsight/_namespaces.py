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
        """Resolve ``namespace`` and, when it is an owner namespace, make it addressable.

        The slash form cannot be a REST path segment, so it is provisioned as an alias
        on the shared bank — which is exactly what makes ``erature/<owner>`` usable.
        """
        wanted = normalize_namespace(namespace)
        canonical = self.canonical_bank(wanted)
        owner = owner_of(wanted)
        if not owner:
            return canonical
        # Ensure the slash namespace is registered as an alias of the canonical bank.
        try:
            aliases = self._client.bank_aliases(canonical)
        except HindsightError as exc:
            logger.debug("hindsight alias read for %s failed: %s", canonical, exc)
            return canonical
        existing = {
            str(e.get("alias"))
            for e in (aliases.get("aliases") or [])
            if isinstance(e, dict) and e.get("alias")
        }
        if wanted not in existing:
            try:
                self._client.add_bank_alias(canonical, wanted)
                logger.info("hindsight: registered namespace alias %r on bank %r", wanted, canonical)
            except HindsightError as exc:
                logger.debug("hindsight alias write for %r failed: %s", wanted, exc)
        self._canonical_by_namespace[wanted] = canonical
        return canonical

    def namespaces(self) -> list[str]:
        """Every namespace currently known to the substrate (bank ids + aliases)."""
        self._refresh_index()
        return sorted(self._canonical_by_namespace)

    def bank_snapshot(self, bank_id: str) -> dict[str, Any]:
        try:
            return self._client.get_bank(bank_id)
        except HindsightError:
            return {}
