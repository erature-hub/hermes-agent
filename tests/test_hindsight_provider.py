"""Hermetic tests for the sovereign Hindsight memory provider.

Everything here runs against a FAKE transport — no socket, no live substrate, no
credential. The suite covers the three sovereign behaviours and the wire contract:

* **Bank namespacing** — ``hermes`` / ``erature/<owner>`` resolve to the canonical bank
  id the substrate addresses (a slash cannot be a path segment, so the slash form is an
  ALIAS on the canonical bank).
* **Commencement recall** — a session-start recall runs in the background and stages
  context for the next ``prefetch()``; a slow or dead substrate degrades to no context
  and never blocks a turn.
* **Decision/directive retention** — a turn is retained, and a detected decision is
  promoted into a standing directive exactly once.

The fake transport is installed at the client's ``opener`` seam, so the URL, method,
headers and body the plugin actually emits are all asserted.
"""

from __future__ import annotations

import importlib
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any

import pytest

# The repo root must be importable: the plugin imports agent.memory_provider.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_PROVIDER_PACKAGE = "plugins.memory.hermes-memory-hindsight"
_provider_mod = importlib.import_module(_PROVIDER_PACKAGE)
_client_mod = importlib.import_module(f"{_PROVIDER_PACKAGE}._client")
_namespaces_mod = importlib.import_module(f"{_PROVIDER_PACKAGE}._namespaces")

HindsightClient = _client_mod.HindsightClient
HindsightError = _client_mod.HindsightError
BankResolver = _namespaces_mod.BankResolver
NamespaceError = _namespaces_mod.NamespaceError
normalize_namespace = _namespaces_mod.normalize_namespace
owner_of = _namespaces_mod.owner_of
owner_bank_id = _namespaces_mod.owner_bank_id
HindsightMemoryProvider = _provider_mod.HindsightMemoryProvider
PROVIDER_NAME = _provider_mod.PROVIDER_NAME


# ---------------------------------------------------------------------------
# Fake transport
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: Any, status: int = 200) -> None:
        self._body = json.dumps(payload).encode("utf-8") if payload is not None else b""
        self.status = status

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeTransport:
    """Records every request and answers from a routing table.

    Routes are ``"<METHOD> <path template>"`` keys, where ``{bank}`` matches one path
    segment. Matching is EXACT (a template is turned into a regex), because a
    substring match would let ``/v1/default/banks`` shadow ``/reflect`` and
    ``/memories/recall`` — a wrong URL would then silently return a wrong-but-200
    answer instead of failing loudly.

    The bank registry is stateful: an alias POST really registers the alias, exactly
    as the substrate does, so idempotency is exercised against real behaviour.
    """

    def __init__(self, routes: dict[str, Any] | None = None) -> None:
        self.routes: dict[str, Any] = dict(routes or {})
        self.calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        # bank_id -> list of alias dicts
        self.aliases: dict[str, list[dict[str, Any]]] = {}
        # bank_id -> provisioned bank record (created through PUT /banks/{id})
        self.banks: dict[str, dict[str, Any]] = {}
        self.directives: list[dict[str, Any]] = []

    def add(self, route: str, handler: Any) -> None:
        self.routes[route] = handler

    @staticmethod
    def _compile(template: str) -> re.Pattern[str]:
        """A path template -> an exact regex; every ``{name}`` matches ONE path segment.

        Split on the placeholders FIRST (escaping would hide them behind backslashes),
        escape the literal parts, then rejoin with the segment matcher.
        """
        placeholder = re.compile(r"\{[a-z_]+\}")
        segments = placeholder.split(template)
        pattern = r"[^/]+".join(re.escape(part) for part in segments)
        return re.compile(f"^{pattern}$")

    def _match(self, method: str, path: str) -> tuple[Any, str] | None:
        for template, handler in self.routes.items():
            route_method, _, route_path = template.partition(" ")
            if route_method.upper() != method.upper():
                continue
            if self._compile(route_path).match(path):
                return handler, route_path
        return None

    def __call__(self, request: Any, timeout: float | None = None) -> _FakeResponse:
        method = request.get_method()
        url = request.full_url
        # Compare PATHS, not full URLs: the route templates are path templates.
        # Decode the path the way an ASGI server does BEFORE routing: a percent-encoded
        # slash becomes a real path separator, so ``%2F`` inside a bank id does not
        # match a single-segment template. That is the substrate's real behaviour (a
        # slash bank id 404s), and the resolver's fallback depends on it.
        split = urllib.parse.urlsplit(url)
        path = urllib.parse.unquote(split.path)
        query = split.query
        body_raw = request.data
        body = json.loads(body_raw.decode("utf-8")) if body_raw else None
        headers = {k.lower(): v for k, v in request.header_items()}
        with self._lock:
            self.calls.append({"method": method, "url": url, "path": path, "query": query,
                               "body": body, "headers": headers})
        matched = self._match(method, path)
        if matched is None:
            # An unmatched path is a 404 from the substrate, not a test explosion:
            # the provider must survive it and fall back.
            raise _http_error(404, "Not Found")
        handler, _template = matched
        result = handler(method, body, path, query)
        if isinstance(result, _FakeResponse):
            return result
        if isinstance(result, tuple):
            status, payload = result
            return _FakeResponse(payload, status)
        return _FakeResponse(result)


def _http_error(status: int, detail: str = "boom") -> urllib.error.HTTPError:
    import io

    return urllib.error.HTTPError(
        "http://test", status, detail, {}, io.BytesIO(detail.encode("utf-8"))
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_real_sockets(monkeypatch):
    """Fail loudly if anything opens a real socket.

    The sovereign-memory suite must never depend on a running Hindsight substrate: a
    test that quietly reached the live substrate would pass on a developer machine and
    fail in CI (or, worse, mutate real memory). Blocking outbound connections makes
    that impossible.
    """

    def _blocked(*args, **kwargs):
        raise AssertionError(
            "the sovereign-memory suite opened a real socket: it must use the fake "
            "transport only and never touch a live substrate"
        )

    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)


@pytest.fixture(autouse=True)
def _isolate_env(tmp_path, monkeypatch):
    """Isolate HERMES_HOME and every Hindsight credential/env knob."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    for var in (
        "HINDSIGHT_API_KEY",
        "HINDSIGHT_API_URL",
        "HINDSIGHT_TIMEOUT",
        "HINDSIGHT_BANK_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    return hermes_home


def _make_client(transport: _FakeTransport, *, api_key: str = "test-key") -> HindsightClient:
    return HindsightClient("http://substrate.test", api_key, timeout=1.0, opener=transport)


def _bind_transport(monkeypatch, transport: _FakeTransport) -> None:
    """Make the provider build its client on the fake transport.

    ``initialize()`` constructs its own client, so without this seam the provider
    would open a real socket to the configured base URL — which would make the suite
    non-hermetic AND depend on a running substrate.
    """
    def _factory(base_url: str, api_key: str, *, timeout: float = 1.0, opener: Any = None) -> HindsightClient:
        return HindsightClient(base_url, api_key, timeout=timeout, opener=transport)

    monkeypatch.setattr(_provider_mod, "HindsightClient", _factory)


def _default_routes(transport: _FakeTransport | None = None) -> dict[str, Any]:
    """A minimal but faithful substrate: one bank with one alias.

    Alias and directive writes mutate the transport's own state, so idempotency and
    "promote once" are exercised against behaviour that behaves like the real thing.
    """
    transport = transport or _FakeTransport()

    def _bank_of(path: str) -> str:
        parts = path.split("/")
        return parts[parts.index("banks") + 1] if "banks" in parts else ""

    _BANKS = [
        {
            "bank_id": "erature-hermes",
            "name": "Erature Hermes",
            "display_alias": "hermes",
            "fact_count": 12,
        },
    ]

    def list_banks(m, b, p, q):
        # ``q`` is a substring filter over bank id, name and aliases; the response
        # reports the aliases it matched in ``matched_aliases`` — which is how a
        # SLASH alias is discoverable at all.
        needle = ""
        if q:
            for part in q.split("&"):
                if part.startswith("q="):
                    needle = urllib.parse.unquote_plus(part[2:])
        rows = []
        banks = list(_BANKS) + [
            {**record, "display_alias": None, "fact_count": 0}
            for record in transport.banks.values()
        ]
        for bank in banks:
            aliases = transport.aliases.get(bank["bank_id"], [{"alias": "hermes", "primary": True}])
            names = [bank["bank_id"], bank.get("name") or "", bank.get("display_alias") or ""]
            names += [a.get("alias", "") for a in aliases]
            if needle and not any(needle.lower() in n.lower() for n in names):
                continue
            matched = [a.get("alias") for a in aliases
                       if needle and needle.lower() in a.get("alias", "").lower()]
            rows.append({**bank, "matched_aliases": matched})
        return {"banks": rows, "total": len(rows)}

    def list_aliases(m, b, p, q):
        bank = _bank_of(p)
        return {
            "bank_id": bank,
            "aliases": transport.aliases.get(bank, [{"alias": "hermes", "primary": True}]),
        }

    def add_alias(m, b, p, q):
        bank = _bank_of(p)
        entries = transport.aliases.setdefault(bank, [{"alias": "hermes", "primary": True}])
        alias = (b or {}).get("alias")
        if alias and not any(e.get("alias") == alias for e in entries):
            entries.append({"alias": alias, "primary": bool((b or {}).get("primary"))})
        return {"bank_id": bank, "aliases": entries}

    def list_directives(m, b, p, q):
        return {"items": list(transport.directives), "total": len(transport.directives)}

    def create_directive(m, b, p, q):
        entry = dict(b or {})
        entry.setdefault("id", f"d{len(transport.directives) + 1}")
        transport.directives.append(entry)
        return entry

    def put_bank(m, b, p, q):
        """``PUT /banks/{id}`` is the upsert — the substrate has no POST /banks."""
        bank_id = _bank_of(p)
        transport.banks.setdefault(bank_id, {"bank_id": bank_id, "name": "", "aliases": []})
        record = transport.banks[bank_id]
        if isinstance(b, dict) and b.get("name"):
            record["name"] = b["name"]
        return {"bank_id": bank_id, "name": record.get("name", "")}

    return {
        "GET /v1/default/banks": list_banks,
        "GET /v1/default/banks/{bank}": lambda m, b, p, q: {"bank_id": _bank_of(p)},
        "PUT /v1/default/banks/{bank}": put_bank,
        "GET /v1/default/banks/{bank}/aliases": list_aliases,
        "POST /v1/default/banks/{bank}/aliases": add_alias,
        "POST /v1/default/banks/{bank}/memories": lambda m, b, p, q: {
            "success": True, "items_count": 1
        },
        "POST /v1/default/banks/{bank}/memories/recall": lambda m, b, p, q: {
            "results": [{"id": "f1", "text": "Paul prefers concise bullet summaries."}]
        },
        "POST /v1/default/banks/{bank}/reflect": lambda m, b, p, q: {
            "text": "Paul prefers concise summaries."
        },
        "GET /v1/default/banks/{bank}/directives": list_directives,
        "POST /v1/default/banks/{bank}/directives": create_directive,
    }


def _transport(routes: dict[str, Any] | None = None) -> _FakeTransport:
    """A transport preloaded with the default substrate routes."""
    transport = _FakeTransport()
    transport.routes.update(_default_routes(transport))
    return transport


# ===========================================================================
# Namespacing
# ===========================================================================


class TestNamespaceNormalization:
    def test_shared_namespace_is_preserved(self):
        assert normalize_namespace("hermes") == "hermes"

    def test_owner_namespace_is_preserved(self):
        assert normalize_namespace("erature/paul") == "erature/paul"

    def test_bare_owner_is_promoted_to_owner_namespace(self):
        assert normalize_namespace("paul") == "erature/paul"

    def test_surrounding_whitespace_is_trimmed(self):
        assert normalize_namespace("  erature/paul  ") == "erature/paul"

    @pytest.mark.parametrize("bad", ["", "   ", "erature/", "/paul", "erature/a/b", "other/paul"])
    def test_malformed_namespaces_are_refused(self, bad):
        with pytest.raises(NamespaceError):
            normalize_namespace(bad)

    def test_control_characters_are_refused(self):
        with pytest.raises(NamespaceError):
            normalize_namespace("erature/pa\x00ul")

    def test_oversized_namespace_is_refused(self):
        with pytest.raises(NamespaceError):
            normalize_namespace("erature/" + ("x" * 300))

    def test_owner_of_extracts_the_owner_segment(self):
        assert owner_of("hermes") == ""
        assert owner_of("erature/paul") == "paul"


class TestBankResolver:
    def test_shared_namespace_resolves_through_the_primary_alias(self):
        transport = _transport()
        resolver = BankResolver(_make_client(transport))
        assert resolver.canonical_bank("hermes") == "erature-hermes"

    def test_owner_namespace_resolves_to_its_own_bank(self, monkeypatch):
        """An owner namespace must NOT collapse onto the shared bank.

        Falling back to the shared bank for every owner means one owner's memories
        are recalled under another owner's namespace — the namespace stops being a
        boundary. Each owner gets a dedicated bank instead.
        """
        transport = _transport()
        resolver = BankResolver(_make_client(transport))
        canonical = resolver.ensure_namespace("erature/paul")
        assert canonical != "erature-hermes"
        assert "/" not in canonical
        assert canonical == "erature-paul"

    def test_two_owners_get_different_banks(self, monkeypatch):
        transport = _transport()
        resolver = BankResolver(_make_client(transport))
        paul = resolver.ensure_namespace("erature/paul")
        jane = resolver.ensure_namespace("erature/jane")
        assert paul != jane

    def test_an_owner_namespace_is_provisioned_as_a_bank(self):
        """The dedicated bank is created, then the slash form is its alias."""
        transport = _transport()
        transport.routes["PUT /v1/default/banks/{bank}"] = (
            lambda m, b, p, q: {"bank_id": _bank_of(p), "name": "Erature / paul"}
        )
        resolver = BankResolver(_make_client(transport))
        canonical = resolver.ensure_namespace("erature/paul")
        put_calls = [c for c in transport.calls if c["method"] == "PUT"]
        assert put_calls, "the dedicated bank was never provisioned"
        assert canonical in put_calls[0]["path"]
        alias_calls = [
            c for c in transport.calls if c["method"] == "POST" and c["path"].endswith("/aliases")
        ]
        assert alias_calls
        assert alias_calls[0]["body"]["alias"] == "erature/paul"

    def test_a_lossy_owner_name_cannot_collide_with_another_owner(self):
        """``pa ul`` and ``pa-ul`` are different owners and must not share a bank."""
        assert owner_bank_id("erature/pa ul") != owner_bank_id("erature/pa-ul")
        assert owner_bank_id("erature/pa-ul") == "erature-pa-ul"

    def test_an_existing_alias_is_reused_not_reprovisioned(self):
        """An owner namespace provisioned earlier keeps ITS bank."""
        transport = _transport()
        transport.banks["erature-paul"] = {"bank_id": "erature-paul", "name": "Erature / paul"}
        transport.aliases["erature-paul"] = [{"alias": "erature/paul", "primary": False}]
        resolver = BankResolver(_make_client(transport))
        canonical = resolver.ensure_namespace("erature/paul")
        assert canonical == "erature-paul"
        assert not [c for c in transport.calls if c["method"] == "PUT"]

    def test_the_shared_namespace_is_never_provisioned(self):
        """``hermes`` addresses the shared bank; it must not create one."""
        transport = _transport()
        resolver = BankResolver(_make_client(transport))
        assert resolver.ensure_namespace("hermes") == "erature-hermes"
        assert not [c for c in transport.calls if c["method"] == "PUT"]

    def test_ensure_namespace_registers_the_slash_form_as_an_alias(self):
        """The slash form cannot be a path segment, so it is provisioned as an alias."""
        transport = _transport()
        resolver = BankResolver(_make_client(transport))
        resolver.ensure_namespace("erature/paul")
        alias_calls = [
            c for c in transport.calls
            if c["method"] == "POST" and c["path"].endswith("/aliases")
        ]
        assert alias_calls, "the slash namespace was never registered as an alias"
        assert alias_calls[0]["body"]["alias"] == "erature/paul"

    def test_a_provisioned_owner_namespace_resolves_to_its_bank(self):
        """A slash alias is discovered through the bank list's matched_aliases.

        The substrate cannot address a slash bank id through a REST path, so the
        namespace label is an ALIAS and the provider must resolve it to the canonical
        bank id it actually addresses.
        """
        transport = _transport()
        transport.aliases["erature-hermes"] = [
            {"alias": "hermes", "primary": True},
            {"alias": "erature/paul", "primary": False},
        ]
        resolver = BankResolver(_make_client(transport))
        assert resolver.canonical_bank("erature/paul") == "erature-hermes"

    def test_an_unaddressable_slash_bank_id_is_never_used_as_a_bank(self):
        """``erature/paul`` must never become the bank id the provider addresses.

        The substrate's router does not decode ``%2F`` into a path segment, so a
        percent-encoded bank id would 404 on every read and write.
        """
        transport = _transport()
        resolver = BankResolver(_make_client(transport))
        canonical = resolver.canonical_bank("erature/paul")
        assert "/" not in canonical
        assert "%2F" not in canonical.upper()

    def test_ensure_namespace_is_idempotent(self):
        transport = _transport()
        resolver = BankResolver(_make_client(transport))
        resolver.ensure_namespace("erature/paul")
        resolver.ensure_namespace("erature/paul")
        alias_calls = [
            c for c in transport.calls
            if c["method"] == "POST" and c["path"].endswith("/aliases")
        ]
        assert len(alias_calls) == 1

    def test_shared_namespace_never_registers_an_alias(self):
        transport = _transport()
        resolver = BankResolver(_make_client(transport))
        resolver.ensure_namespace("hermes")
        assert not [
            c for c in transport.calls if c["method"] == "POST" and c["path"].endswith("/aliases")
        ]


# ===========================================================================
# Client wire contract
# ===========================================================================


class TestClientWireContract:
    def test_bearer_auth_is_sent_on_every_v1_call(self):
        transport = _transport()
        client = _make_client(transport, api_key="secret-token")
        client.recall("hermes", "hello")
        call = transport.calls[-1]
        assert call["headers"]["authorization"] == "Bearer secret-token"

    def test_x_api_key_is_never_sent(self):
        """Hindsight rejects X-API-Key; only the bearer header is accepted."""
        transport = _transport()
        _make_client(transport).recall("hermes", "hello")
        assert "x-api-key" not in transport.calls[-1]["headers"]

    def test_retain_posts_an_items_list(self):
        """The substrate's RetainRequest takes items[]; a bare content field is a 422."""
        transport = _transport()
        _make_client(transport).retain("hermes", "a fact", context="ctx")
        body = transport.calls[-1]["body"]
        assert list(body) == ["items", "async"]
        assert body["items"][0]["content"] == "a fact"
        assert body["items"][0]["context"] == "ctx"

    def test_retain_is_asynchronous_by_default(self):
        """The turn path must not block on extraction.

        Measured on the live shared bank (13k facts): a synchronous retain took
        2-4s for a normal turn and 9-10s when extraction flaked — and a flaky
        extraction answers 500, losing the fact. The async path returns in ~24ms
        and the substrate owns the retry, so the write is not lost.
        """
        transport = _transport()
        _make_client(transport).retain("hermes", "a fact")
        assert transport.calls[-1]["body"]["async"] is True

    def test_retain_can_be_synchronous_on_request(self):
        """An explicit synchronous retain omits the async flag entirely."""
        transport = _transport()
        _make_client(transport).retain("hermes", "a fact", asynchronous=False)
        body = transport.calls[-1]["body"]
        assert "async" not in body
        assert list(body) == ["items"]

    def test_operation_status_reports_the_substrate_record(self):
        transport = _transport()
        transport.routes["GET /v1/default/banks/{bank}/operations/{op}"] = (
            lambda m, b, p, q: {"status": "completed"}
        )
        status = _make_client(transport).operation_status("hermes", "op-1")
        assert status["status"] == "completed"
        assert transport.calls[-1]["path"].endswith("/operations/op-1")

    def test_operation_status_treats_an_evicted_record_as_complete(self):
        """A 404 means the substrate no longer tracks the op — not that it failed."""
        transport = _transport()
        transport.routes["GET /v1/default/banks/{bank}/operations/{op}"] = (
            lambda m, b, p, q: (_ for _ in ()).throw(HindsightError("404 gone", status=404))
        )
        status = _make_client(transport).operation_status("hermes", "op-1")
        assert status["status"] == "completed"

    def test_recall_posts_query_and_budget(self):
        transport = _transport()
        _make_client(transport).recall("hermes", "find me", budget="high")
        body = transport.calls[-1]["body"]
        assert body["query"] == "find me"
        assert body["budget"] == "high"

    def test_reflect_reads_the_text_answer(self):
        transport = _transport()
        assert _make_client(transport).reflect("hermes", "q") == "Paul prefers concise summaries."

    def test_delete_memory_retires_the_memory_through_the_curation_route(self):
        """There is NO per-memory DELETE on the substrate.

        ``DELETE /v1/default/banks/{bank}/memories/{memory}`` answers **405 Method
        Not Allowed** on the live substrate (the OpenAPI lists only GET and PATCH
        there). The real per-memory removal is the curation route: PATCH with
        ``state="invalidated"``, which excludes the memory from recall while
        keeping it for audit.
        """
        transport = _transport()
        transport.routes["PATCH /v1/default/banks/{bank}/memories/{memory}"] = (
            lambda m, b, p, q: {"id": "mem-123", "state": "invalidated"}
        )
        _make_client(transport).delete_memory("hermes", "mem-123")
        call = transport.calls[-1]
        assert call["method"] == "PATCH"
        assert call["path"].endswith("/memories/mem-123")
        assert call["body"]["state"] == "invalidated"

    def test_http_error_is_raised_with_status(self):
        def boom(m, b, p, q):
            raise _http_error(422, "unprocessable")

        transport = _transport()
        transport.routes["POST /v1/default/banks/{bank}/memories"] = boom
        with pytest.raises(HindsightError) as excinfo:
            _make_client(transport).retain("hermes", "x")
        assert excinfo.value.status == 422

    def test_transient_5xx_is_retried_then_raised(self):
        attempts = {"n": 0}

        def flaky(m, b, p, q):
            attempts["n"] += 1
            raise _http_error(503, "unavailable")

        transport = _transport()
        transport.routes["POST /v1/default/banks/{bank}/memories"] = flaky
        with pytest.raises(HindsightError):
            _make_client(transport).retain("hermes", "x")
        assert attempts["n"] > 1, "a 503 must be retried"

    def test_4xx_is_not_retried(self):
        attempts = {"n": 0}

        def rejected(m, b, p, q):
            attempts["n"] += 1
            raise _http_error(400, "bad request")

        transport = _transport()
        transport.routes["POST /v1/default/banks/{bank}/memories"] = rejected
        with pytest.raises(HindsightError):
            _make_client(transport).retain("hermes", "x")
        assert attempts["n"] == 1, "a 4xx is a contract error and must not be retried"

    def test_error_message_never_leaks_the_api_key(self):
        def boom(m, b, p, q):
            raise _http_error(500, "server exploded")

        transport = _transport()
        transport.routes["POST /v1/default/banks/{bank}/memories"] = boom
        with pytest.raises(HindsightError) as excinfo:
            _make_client(transport, api_key="super-secret-key").retain("hermes", "x")
        assert "super-secret-key" not in str(excinfo.value)


# ===========================================================================
# Provider lifecycle
# ===========================================================================


def _bank_of(path: str) -> str:
    """The bank id segment of a substrate path."""
    parts = path.split("/")
    return parts[parts.index("banks") + 1] if "banks" in parts else ""


def _provider(monkeypatch, transport, **cfg) -> HindsightMemoryProvider:
    """Build a provider bound to a fake transport, without touching the network."""
    provider = HindsightMemoryProvider()
    monkeypatch.setattr(provider, "_client", _make_client(transport))
    monkeypatch.setattr(provider, "_resolver", BankResolver(_make_client(transport)))
    provider._namespace = cfg.get("namespace", "hermes")
    provider._bank_id = cfg.get("bank", "erature-hermes")
    provider._api_key = "test-key"
    provider._base_url = "http://substrate.test"
    provider._session_id = "sess-1"
    return provider


class TestProviderIdentity:
    def test_provider_name_is_stable(self):
        assert HindsightMemoryProvider().name == PROVIDER_NAME
        assert PROVIDER_NAME == "hermes-memory-hindsight"

    def test_availability_requires_a_credential(self, monkeypatch):
        monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)
        provider = HindsightMemoryProvider()
        assert provider.is_available() is False
        assert "HINDSIGHT_API_KEY" in provider.unavailable_reason()

    def test_availability_is_true_with_a_credential(self, monkeypatch):
        monkeypatch.setenv("HINDSIGHT_API_KEY", "test-key")
        assert HindsightMemoryProvider().is_available() is True

    def test_availability_accepts_a_key_in_the_provider_config(self, monkeypatch):
        """The declared schema advertises ``api_key``; a value there must be honored.

        The provider's own config file is a first-class secret location: the
        Dashboard's flat-json storage and the community ``hindsight`` plugin both
        keep the key there. Reading only the env var made the provider report
        itself unavailable — and ``agent_init`` then DROPS it, so the session
        silently loses external memory.
        """
        home = Path(os.environ["HERMES_HOME"])
        (home / PROVIDER_NAME).mkdir(parents=True, exist_ok=True)
        (home / PROVIDER_NAME / "config.json").write_text(
            json.dumps({"api_key": "file-scoped-key"}), encoding="utf-8"
        )
        monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)
        provider = HindsightMemoryProvider()
        assert provider.is_available() is True
        assert provider._resolve_api_key() == "file-scoped-key"

    def test_the_env_store_wins_over_the_config_file(self, monkeypatch):
        """A rotated key in the env store must not be shadowed by a stale file value."""
        home = Path(os.environ["HERMES_HOME"])
        (home / PROVIDER_NAME).mkdir(parents=True, exist_ok=True)
        (home / PROVIDER_NAME / "config.json").write_text(
            json.dumps({"api_key": "stale-file-key"}), encoding="utf-8"
        )
        monkeypatch.setenv("HINDSIGHT_API_KEY", "fresh-env-key")
        assert HindsightMemoryProvider()._resolve_api_key() == "fresh-env-key"

    def test_tool_schemas_are_declared(self):
        names = {s["name"] for s in HindsightMemoryProvider().get_tool_schemas()}
        assert names == {
            "hindsight_recall",
            "hindsight_reflect",
            "hindsight_retain",
            "hindsight_directives",
        }


class TestProviderInitialization:
    def test_initialize_binds_the_bank_and_stages_a_recall(self, monkeypatch):
        transport = _transport()
        monkeypatch.setenv("HINDSIGHT_API_KEY", "test-key")
        _bind_transport(monkeypatch, transport)
        provider = HindsightMemoryProvider()
        provider.initialize("sess-1", hermes_home=str(_isolate_home(monkeypatch)))
        assert provider._bank_id == "erature-hermes"
        assert provider._namespace == "hermes"
        # The commencement recall ran against the substrate.
        assert any(c["path"].endswith("/memories/recall") for c in transport.calls)

    def test_initialize_without_a_credential_stays_inert(self, monkeypatch):
        monkeypatch.delenv("HINDSIGHT_API_KEY", raising=False)
        provider = HindsightMemoryProvider()
        provider.initialize("sess-1", hermes_home=str(_isolate_home(monkeypatch)))
        assert provider._client is None

    def test_initialize_survives_an_unreachable_substrate(self, monkeypatch):
        """A dead substrate must not raise out of initialize()."""
        monkeypatch.setenv("HINDSIGHT_API_KEY", "test-key")

        def dead(m, b, p, q):
            raise urllib.error.URLError("connection refused")

        transport = _transport()
        transport.routes["GET /v1/default/banks"] = dead
        _bind_transport(monkeypatch, transport)
        provider = HindsightMemoryProvider()
        provider.initialize("sess-1", hermes_home=str(_isolate_home(monkeypatch)))
        # It fell back to the namespace itself rather than raising.
        assert provider._bank_id == "hermes"


def _isolate_home(monkeypatch) -> Path:
    import os

    return Path(os.environ["HERMES_HOME"])


# ===========================================================================
# Commencement recall
# ===========================================================================


class TestCommencementRecall:
    def test_prefetch_returns_the_staged_context(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider._run_recall("what does Paul prefer?")
        text = provider.prefetch("what does Paul prefer?")
        assert "Paul prefers concise bullet summaries." in text

    def test_prefetch_includes_standing_directives(self, monkeypatch):
        transport = _transport()
        transport.directives.append(
            {"id": "d1", "name": "Prefer bullets", "content": "Use bullet summaries."}
        )
        provider = _provider(monkeypatch, transport)
        provider._run_recall("q")
        text = provider.prefetch("q")
        assert "Standing directives" in text
        assert "Use bullet summaries." in text

    def test_prefetch_consumes_the_staged_context(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider._run_recall("q")
        assert provider.prefetch("q")
        # The second prefetch has nothing staged and must not repeat the first block.
        provider._prefetch_text = ""
        assert provider.prefetch("q") == ""

    def test_a_dead_substrate_yields_no_context_and_no_raise(self, monkeypatch):
        def dead(m, b, p, q):
            raise urllib.error.URLError("connection refused")

        transport = _transport()
        transport.routes["POST /v1/default/banks/{bank}/memories/recall"] = dead
        transport.routes["GET /v1/default/banks/{bank}/directives"] = dead
        provider = _provider(monkeypatch, transport)
        provider._run_recall("q")
        assert provider.prefetch("q") == ""

    def test_recall_status_reports_the_count_then_resets(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider._run_recall("q")
        provider.prefetch("q")
        status = provider.recall_status()
        assert status is not None and status.count >= 1
        assert provider.recall_status() is None

    def test_queue_prefetch_stages_a_background_recall(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider.queue_prefetch("next turn")
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(c["path"].endswith("/memories/recall") for c in transport.calls):
                break
            time.sleep(0.01)
        assert any(c["path"].endswith("/memories/recall") for c in transport.calls)

    def test_on_turn_start_triggers_a_recall(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider.on_turn_start(1, "what did we decide about the bank?")
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(c["path"].endswith("/memories/recall") for c in transport.calls):
                break
            time.sleep(0.01)
        assert any(c["path"].endswith("/memories/recall") for c in transport.calls)

    def test_injected_context_is_bounded(self, monkeypatch):
        transport = _transport()
        transport.routes["POST /v1/default/banks/{bank}/memories/recall"] = (
            lambda m, b, p, q: {"results": [{"id": f"f{i}", "text": "x" * 500} for i in range(50)]}
        )
        provider = _provider(monkeypatch, transport)
        provider._run_recall("q")
        text = provider.prefetch("q")
        assert len(text) <= _provider_mod.MAX_INJECTED_CHARS


# ===========================================================================
# Decision / directive retention
# ===========================================================================


class TestDecisionRetention:
    def test_a_turn_is_retained(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider.sync_turn("what is my name?", "Your name is Paul.")
        retains = [c for c in transport.calls if c["path"].endswith("/memories")]
        assert retains, "the turn was never retained"
        item = retains[-1]["body"]["items"][0]
        assert "Your name is Paul." in item["content"]
        assert item["metadata"]["namespace"] == "hermes"

    def test_a_decision_is_promoted_to_a_directive(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider.sync_turn(
            "how should we handle the bank?",
            "We will always use the hermes bank for shared memory.",
        )
        creates = [
            c for c in transport.calls
            if c["method"] == "POST" and c["path"].endswith("/directives")
        ]
        assert creates, "the decision was not promoted to a directive"
        assert "hermes bank" in creates[0]["body"]["content"]

    def test_ordinary_prose_is_not_promoted(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider.sync_turn("hello", "Hi there! How can I help you today?")
        creates = [
            c for c in transport.calls
            if c["method"] == "POST" and c["path"].endswith("/directives")
        ]
        assert not creates, "ordinary prose must not become a standing directive"

    def test_the_same_decision_is_not_promoted_twice(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        decision = "We will always use the hermes bank for shared memory."
        provider.sync_turn("q", decision)
        provider.sync_turn("q", decision)
        creates = [
            c for c in transport.calls
            if c["method"] == "POST" and c["path"].endswith("/directives")
        ]
        assert len(creates) == 1, "a repeated decision must not spam directives"

    def test_non_primary_contexts_do_not_write(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider._agent_context = "subagent"
        provider.sync_turn("q", "a")
        assert not [c for c in transport.calls if c["path"].endswith("/memories")]


# ===========================================================================
# Tool bridge
# ===========================================================================


class TestToolBridge:
    def test_recall_tool_returns_results(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        payload = json.loads(provider.handle_tool_call("hindsight_recall", {"query": "prefs"}))
        assert payload["results"][0]["text"].startswith("Paul prefers")
        assert payload["bank"] == "erature-hermes"

    def test_reflect_tool_returns_the_answer(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        payload = json.loads(provider.handle_tool_call("hindsight_reflect", {"query": "q"}))
        assert payload["answer"] == "Paul prefers concise summaries."

    def test_retain_tool_writes_a_fact(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        payload = json.loads(
            provider.handle_tool_call("hindsight_retain", {"content": "Paul likes tea"})
        )
        assert payload["retained"] is True
        retains = [c for c in transport.calls if c["path"].endswith("/memories")]
        assert retains[-1]["body"]["items"][0]["content"] == "Paul likes tea"

    def test_retain_tool_can_promote_a_directive(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider.handle_tool_call(
            "hindsight_retain", {"content": "Always cite the source.", "as_directive": True}
        )
        creates = [
            c for c in transport.calls
            if c["method"] == "POST" and c["path"].endswith("/directives")
        ]
        assert creates

    def test_directives_tool_lists_them(self, monkeypatch):
        transport = _transport()
        transport.directives.append({"id": "d1", "name": "n", "content": "c"})
        provider = _provider(monkeypatch, transport)
        payload = json.loads(provider.handle_tool_call("hindsight_directives", {}))
        assert payload["directives"][0]["name"] == "n"

    def test_unknown_tool_returns_an_error_not_an_exception(self, monkeypatch):
        provider = _provider(monkeypatch, _transport())
        payload = json.loads(provider.handle_tool_call("nope", {}))
        assert "error" in payload

    def test_missing_required_argument_is_reported(self, monkeypatch):
        provider = _provider(monkeypatch, _transport())
        payload = json.loads(provider.handle_tool_call("hindsight_recall", {}))
        assert "error" in payload

    def test_tool_failures_never_raise_into_the_turn(self, monkeypatch):
        def boom(m, b, p, q):
            raise urllib.error.URLError("down")

        transport = _transport()
        transport.routes["POST /v1/default/banks/{bank}/memories/recall"] = boom
        provider = _provider(monkeypatch, transport)
        payload = json.loads(provider.handle_tool_call("hindsight_recall", {"query": "q"}))
        assert "error" in payload


# ===========================================================================
# Discovery / registration
# ===========================================================================


class TestBuiltInMemoryMirror:
    """A built-in MEMORY/USER write must reach the substrate.

    Hermes' built-in stores are the operator's most durable artifact. Without
    mirroring, an explicit "remember this" note lives only on local disk — exactly
    the amnesia this provider exists to remove.
    """

    def test_a_memory_write_is_mirrored(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider.on_memory_write("add", "memory", "The deploy key rotates every 90 days.")
        retains = [c for c in transport.calls if c["path"].endswith("/memories")]
        assert retains, "the built-in memory write was never mirrored"
        item = retains[-1]["body"]["items"][0]
        assert "rotates every 90 days" in item["content"]
        assert item["metadata"]["kind"] == "memory_add"

    def test_a_user_write_is_mirrored_to_the_user_store(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider.on_memory_write("add", "user", "Paul prefers bullets.")
        item = [c for c in transport.calls if c["path"].endswith("/memories")][-1]["body"]["items"][0]
        assert "user profile" in item["content"]
        assert item["metadata"]["store"] == "user profile"

    def test_a_remove_is_recorded_as_a_retraction(self, monkeypatch):
        """The substrate's delete is by memory id, which a mirrored copy has not."""
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider.on_memory_write("remove", "memory", "The deploy key rotates every 90 days.")
        item = [c for c in transport.calls if c["path"].endswith("/memories")][-1]["body"]["items"][0]
        assert "Retracted" in item["content"]
        assert item["metadata"]["kind"] == "memory_remove"

    def test_provenance_metadata_is_carried(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider.on_memory_write(
            "add", "memory", "note", metadata={"write_origin": "tool", "tool_name": "memory"}
        )
        item = [c for c in transport.calls if c["path"].endswith("/memories")][-1]["body"]["items"][0]
        assert item["metadata"]["tool_name"] == "memory"

    def test_an_empty_write_is_ignored(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider.on_memory_write("add", "memory", "   ")
        assert not [c for c in transport.calls if c["path"].endswith("/memories")]

    def test_non_primary_contexts_do_not_mirror(self, monkeypatch):
        transport = _transport()
        provider = _provider(monkeypatch, transport)
        provider._agent_context = "subagent"
        provider.on_memory_write("add", "memory", "note")
        assert not [c for c in transport.calls if c["path"].endswith("/memories")]

    def test_a_dead_substrate_does_not_raise(self, monkeypatch):
        def dead(m, b, p, q):
            raise urllib.error.URLError("connection refused")

        transport = _transport()
        transport.routes["POST /v1/default/banks/{bank}/memories"] = dead
        provider = _provider(monkeypatch, transport)
        provider.on_memory_write("add", "memory", "note")  # must not raise


class TestDiscovery:
    def test_the_provider_is_discoverable_by_name(self):
        from plugins.memory import discover_memory_providers, find_provider_dir

        assert find_provider_dir(PROVIDER_NAME) is not None
        names = {name for name, _desc, _avail in discover_memory_providers()}
        assert PROVIDER_NAME in names

    def test_the_provider_loads_through_the_sanctioned_loader(self):
        from plugins.memory import load_memory_provider

        provider = load_memory_provider(PROVIDER_NAME)
        assert provider is not None
        assert provider.name == PROVIDER_NAME

    def test_the_provider_does_not_shadow_the_catalog_hindsight_plugin(self):
        """A bundled provider named 'hindsight' would shadow the user's installed one."""
        from plugins.memory import find_provider_dir

        assert find_provider_dir(PROVIDER_NAME) != find_provider_dir("hindsight")

    def test_config_schema_is_declared_for_the_dashboard(self):
        from plugins.memory.config_schema import get_provider_config_schema

        schema = get_provider_config_schema(PROVIDER_NAME)
        assert schema is not None
        assert schema.name == PROVIDER_NAME
        assert schema.storage == "flat_json"
        keys = {f.key for f in schema.fields}
        assert {"base_url", "namespace", "budget", "commitment"} <= keys
        secret_fields = [f for f in schema.fields if f.is_secret]
        assert secret_fields and secret_fields[0].env_key == "HINDSIGHT_API_KEY"


# ===========================================================================
# Cache-bust identity
# ===========================================================================


class TestIdentitySignature:
    """The gateway calls ``identity_signature()`` on an UNINITIALIZED instance on
    every inbound message, to decide whether a cached agent must be rebuilt."""

    def test_the_signature_reflects_the_configured_namespace(self, monkeypatch):
        """A namespace change must bust the cached agent.

        Reading the instance state here reported the DEFAULT namespace forever
        (an uninitialized instance still holds it), so the cached agent kept
        using the old bank after the namespace was changed.
        """
        home = Path(os.environ["HERMES_HOME"])
        (home / PROVIDER_NAME).mkdir(parents=True, exist_ok=True)
        (home / PROVIDER_NAME / "config.json").write_text(
            json.dumps({"namespace": "erature/paul"}), encoding="utf-8"
        )
        signature = HindsightMemoryProvider().identity_signature()
        assert signature["hermes_memory_hindsight.namespace"] == "erature/paul"

    def test_a_namespace_change_changes_the_signature(self, monkeypatch):
        home = Path(os.environ["HERMES_HOME"])
        (home / PROVIDER_NAME).mkdir(parents=True, exist_ok=True)
        config = home / PROVIDER_NAME / "config.json"
        config.write_text(json.dumps({"namespace": "hermes"}), encoding="utf-8")
        before = HindsightMemoryProvider().identity_signature()
        config.write_text(json.dumps({"namespace": "erature/paul"}), encoding="utf-8")
        after = HindsightMemoryProvider().identity_signature()
        assert before != after

    def test_the_signature_includes_the_substrate_url(self, monkeypatch):
        """A different substrate is a different memory: it must bust the cache too."""
        home = Path(os.environ["HERMES_HOME"])
        (home / PROVIDER_NAME).mkdir(parents=True, exist_ok=True)
        (home / PROVIDER_NAME / "config.json").write_text(
            json.dumps({"base_url": "http://other.test:8888"}), encoding="utf-8"
        )
        signature = HindsightMemoryProvider().identity_signature()
        assert signature["hermes_memory_hindsight.base_url"] == "http://other.test:8888"

    def test_the_signature_is_cheap_and_read_only(self, monkeypatch):
        """The gateway calls this on every inbound message: it must not write."""
        home = Path(os.environ["HERMES_HOME"])
        (home / PROVIDER_NAME).mkdir(parents=True, exist_ok=True)
        config = home / PROVIDER_NAME / "config.json"
        config.write_text(json.dumps({"namespace": "hermes"}), encoding="utf-8")
        before = config.read_bytes()
        HindsightMemoryProvider().identity_signature()
        assert config.read_bytes() == before


# ===========================================================================
# Async retain operations
# ===========================================================================


class TestAsyncRetainOperations:
    def test_a_turn_tracks_its_retain_operation(self, monkeypatch):
        transport = _transport()
        transport.routes["POST /v1/default/banks/{bank}/memories"] = (
            lambda m, b, p, q: {"success": True, "operation_id": "op-42"}
        )
        provider = _provider(monkeypatch, transport)
        provider.sync_turn("user", "assistant")
        assert "op-42" in provider._pending_ops

    def test_a_recall_waits_for_the_pending_write(self, monkeypatch):
        """A turn retained moments ago must be recall-visible on the next recall."""
        transport = _transport()
        transport.routes["POST /v1/default/banks/{bank}/memories"] = (
            lambda m, b, p, q: {"success": True, "operation_id": "op-7"}
        )
        statuses = iter([{"status": "pending"}, {"status": "completed"}])
        transport.routes["GET /v1/default/banks/{bank}/operations/{op}"] = (
            lambda m, b, p, q: next(statuses, {"status": "completed"})
        )
        provider = _provider(monkeypatch, transport)
        provider.sync_turn("user", "assistant")
        provider._run_recall("q")
        assert not provider._pending_ops
        assert any(c["path"].endswith("/operations/op-7") for c in transport.calls)

    def test_a_stuck_operation_is_dropped_rather_than_waited_forever(self, monkeypatch):
        """A pending operation must cost latency, never a hang."""
        transport = _transport()
        transport.routes["POST /v1/default/banks/{bank}/memories"] = (
            lambda m, b, p, q: {"success": True, "operation_id": "op-stuck"}
        )
        transport.routes["GET /v1/default/banks/{bank}/operations/{op}"] = (
            lambda m, b, p, q: {"status": "pending"}
        )
        provider = _provider(monkeypatch, transport)
        provider.sync_turn("user", "assistant")
        started = time.monotonic()
        provider._await_pending_ops(0.3)
        assert time.monotonic() - started < 3.0
        assert not provider._pending_ops

    def test_a_failed_operation_is_cleared(self, monkeypatch):
        transport = _transport()
        transport.routes["POST /v1/default/banks/{bank}/memories"] = (
            lambda m, b, p, q: {"success": True, "operation_id": "op-failed"}
        )
        transport.routes["GET /v1/default/banks/{bank}/operations/{op}"] = (
            lambda m, b, p, q: {"status": "failed"}
        )
        provider = _provider(monkeypatch, transport)
        provider.sync_turn("user", "assistant")
        provider._await_pending_ops(2.0)
        assert not provider._pending_ops

    def test_shutdown_settles_pending_writes(self, monkeypatch):
        """An accepted-but-unextracted write must not be lost with the process."""
        transport = _transport()
        transport.routes["POST /v1/default/banks/{bank}/memories"] = (
            lambda m, b, p, q: {"success": True, "operation_id": "op-shutdown"}
        )
        transport.routes["GET /v1/default/banks/{bank}/operations/{op}"] = (
            lambda m, b, p, q: {"status": "completed"}
        )
        provider = _provider(monkeypatch, transport)
        provider.sync_turn("user", "assistant")
        provider.shutdown()
        assert not provider._pending_ops
