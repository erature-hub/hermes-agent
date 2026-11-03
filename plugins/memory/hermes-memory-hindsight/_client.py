"""Transport for the local Hindsight episodic-memory substrate (REST over HTTP).

Stdlib only: the sovereign substrate is a local container, so the plugin must not pull a
client dependency into the Hermes install. Everything here is a thin, typed wrapper over
``/v1/default/banks/{bank_id}/...``.

Auth: Hindsight requires ``Authorization: Bearer <key>`` on every ``/v1/*`` path
(``X-API-Key`` is rejected — verified against the live substrate). The key is read by the
caller through ``agent.secret_scope.get_secret`` and never logged, echoed or persisted.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8888"
DEFAULT_TIMEOUT = 30.0
# Transient failures only (connection reset / 5xx). 4xx is a contract error: never retried,
# because retrying a rejected request against a local substrate just burns the turn budget.
RETRY_STATUSES = frozenset({500, 502, 503, 504})
RETRY_DELAYS = (0.25, 0.75)

#: The OpenAPI document title the substrate publishes. It is the PRODUCT identity of
#: the peer, not a liveness signal: ``{"status": "healthy"}`` is answered by half the
#: HTTP servers in the world, while this title is set by the substrate's own app
#: factory (``FastAPI(title="Hindsight HTTP API")``).
#:
#: Matched as a PREFIX so a substrate release that appends to the title still
#: verifies, while a foreign product ("Unsloth UI Backend") does not. A prefix
#: match also rejects a title that merely CONTAINS the word, e.g. "Not Hindsight".
SUBSTRATE_TITLE_PREFIX = "Hindsight"

#: Upper bound on the OpenAPI document the identity probe will read. The substrate's
#: document is ~340 kB, but the ``info`` block (which carries the title) sits in the
#: first few hundred bytes, so the probe reads a bounded PREFIX of it. That keeps the
#: per-probe cost tiny AND stops a peer from making the probe buffer an unbounded body.
OPENAPI_PROBE_LIMIT = 8 * 1024

#: Timeout for the identity probe. It runs before the provider binds, so it must be
#: quick: a peer that cannot answer within this window is treated as "not the
#: substrate" and the bearer is never sent to it.
IDENTITY_TIMEOUT = 5.0

#: How long an identity verdict stays valid, in seconds.
#:
#: The verdict is CACHED so a normal turn does not re-probe, but it is NOT valid
#: forever: the substrate on a given port can be REPLACED while a session is live
#: (that is exactly what happened on 2026-11-01, when the substrate was decommissioned
#: and an unrelated product took the port). Without expiry, a verdict earned against
#: the substrate would keep authorising a bearer to be sent to whatever now owns the
#: port. A bounded window turns that from "for the rest of the session" into "for at
#: most this long".
IDENTITY_TTL = 60.0

#: How long a REFUSAL (the peer is not the substrate) stays valid, in seconds. It
#: is short so a substrate that comes back up is picked up again promptly — a
#: negative verdict blocks sovereign memory, and blocking it for a full minute
#: after a blip would strand the session.
NEGATIVE_IDENTITY_TTL = 5.0


class HindsightError(RuntimeError):
    """A Hindsight request failed. ``status`` is 0 for transport failures."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class SubstrateIdentityError(HindsightError):
    """The peer on the configured base URL did not identify as the Hindsight substrate.

    Raised BEFORE any credential is attached to a request, so a peer that is not the
    substrate never receives the bearer and never receives a retained transcript.
    """


@dataclass(frozen=True)
class SubstrateIdentity:
    """The verdict of an UNAUTHENTICATED identity probe.

    ``identified`` is True only when the peer proved it IS the Hindsight substrate.
    """

    identified: bool
    reason: str = ""
    title: str = ""
    version: str = ""


def _openapi_identity(raw: bytes) -> tuple[str, str]:
    """Pull ``(title, version)`` out of an OpenAPI document's ``info`` block.

    The document is read as a PREFIX, so the ``info`` object may be truncated. The
    parser therefore decodes the ``info`` value on its own (JSON decoder
    ``raw_decode``) instead of parsing the whole document, and tolerates the
    truncation by returning what it could decode.

    ``raw_decode`` does NOT skip leading whitespace, so the whitespace between the
    ``"info"`` key and its value is stripped explicitly -- a document serialised
    with ``json.dumps`` defaults puts a space there, and the substrate's own
    document does not.
    """
    text = raw.decode("utf-8", "replace")
    marker = text.find('"info"')
    if marker < 0:
        return "", ""
    colon = text.find(":", marker + len('"info"'))
    if colon < 0:
        return "", ""
    start = colon + 1
    while start < len(text) and text[start] in " \t\r\n":
        start += 1
    try:
        info, _ = json.JSONDecoder().raw_decode(text, start)
    except ValueError:
        return "", ""
    if not isinstance(info, dict):
        return "", ""
    title = str(info.get("title") or "").strip()
    version = str(info.get("version") or "").strip()
    return title, version


def probe_substrate(
    base_url: str,
    *,
    timeout: float = IDENTITY_TIMEOUT,
    opener: Any = None,
) -> SubstrateIdentity:
    """Ask the peer at *base_url* whether it is the Hindsight substrate.

    Every request here is UNAUTHENTICATED: this probe is what decides whether the
    bearer may be attached to anything, so attaching it during the probe would
    defeat the purpose. ``client.health()`` is deliberately NOT reused for this --
    it routes through ``_request``, which attaches the bearer.

    Two signals are required, and the second is the decisive one:

    1. ``GET /health`` -- the substrate's readiness contract (unauthenticated). It
       answers ``{"status": "healthy", ...}``. This proves a live peer, but NOT
       which peer: that shape is generic.
    2. ``GET /openapi.json`` -- the substrate's published API description
       (unauthenticated). Its ``info.title`` names the PRODUCT. A different
       product on the same port cannot pass this, which is the whole point: the
       configured port may hold an unrelated third-party service, and attaching
       the bearer to it would hand that service the credential AND every retained
       transcript.

    This is a guard against MISCONFIGURATION, not an authentication mechanism. A
    local process that deliberately impersonates the Hindsight substrate can still
    pass it -- that threat needs mutual TLS or a pinned digest, not an
    unauthenticated probe.
    """
    url = base_url.rstrip("/")
    opener = opener or urllib.request.urlopen

    def _get(path: str, limit: int | None = None) -> tuple[int, bytes]:
        """One UNAUTHENTICATED GET. No Authorization header is ever attached."""
        request = urllib.request.Request(
            f"{url}{path}", method="GET", headers={"Accept": "application/json"}
        )
        try:
            with opener(request, timeout=timeout) as resp:
                raw = resp.read(limit) if limit is not None else resp.read()
                status = int(getattr(resp, "status", 200) or 200)
            return status, raw
        except urllib.error.HTTPError as exc:
            try:
                return int(exc.code), exc.read()
            except Exception:  # pragma: no cover - body already consumed
                return int(exc.code), b""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SubstrateIdentityError(f"the peer did not answer: {exc}") from exc

    try:
        health_status, health_body = _get("/health")
    except SubstrateIdentityError as exc:
        return SubstrateIdentity(False, str(exc))
    if health_status != 200:
        return SubstrateIdentity(
            False, f"the peer answered /health with HTTP {health_status}"
        )
    try:
        health = json.loads(health_body.decode("utf-8", "replace") or "{}")
    except ValueError:
        return SubstrateIdentity(False, "the peer's /health answer was not JSON")
    if not isinstance(health, dict) or health.get("status") != "healthy":
        return SubstrateIdentity(False, "the peer is not reporting a healthy substrate")

    try:
        spec_status, spec_body = _get("/openapi.json", limit=OPENAPI_PROBE_LIMIT)
    except SubstrateIdentityError as exc:
        return SubstrateIdentity(False, str(exc))
    if spec_status != 200:
        return SubstrateIdentity(
            False, f"the peer answered /openapi.json with HTTP {spec_status}"
        )
    title, version = _openapi_identity(spec_body)
    if not title.startswith(SUBSTRATE_TITLE_PREFIX):
        return SubstrateIdentity(
            False,
            f"the peer identifies as {title or 'an unknown product'!r}, "
            f"not a {SUBSTRATE_TITLE_PREFIX} substrate",
            title=title,
            version=version,
        )
    return SubstrateIdentity(True, "", title=title, version=version)


class HindsightClient:
    """Minimal REST client for one Hindsight base URL + bearer key.

    Instances are bound to a single profile (the key and base URL are resolved once at
    ``initialize()``) and never re-read ambient environment, so two profiles in one
    multiplexing process cannot cross-contaminate each other's credentials.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        opener: Any = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key or ""
        self.timeout = float(timeout)
        # ``opener`` is injectable so the hermetic test suite can drive a fake transport
        # without binding a socket.
        self._opener = opener or urllib.request.urlopen
        # The identity verdict, and the time it was earned. ``None`` means "not probed
        # yet" -- and an unprobed client refuses to send the bearer (see _request).
        self._identity: Optional[SubstrateIdentity] = None
        self._identity_at: float = 0.0
        # The recall worker and the main thread both call into the client, so the
        # verdict cache is guarded: without it, two threads could probe at once and
        # a half-written verdict could be observed.
        self._identity_lock = threading.Lock()

    # -- identity ----------------------------------------------------------------

    def identify(self, *, force: bool = False) -> SubstrateIdentity:
        """Prove the peer at the base URL IS the Hindsight substrate.

        The probe is UNAUTHENTICATED and its verdict is cached for ``IDENTITY_TTL``
        seconds. Callers that must not proceed against an unverified peer check
        ``identity.identified`` and refuse to continue.

        A NEGATIVE verdict is cached only briefly: refusing to talk to a peer for a
        full minute after it was unreachable would strand sovereign memory for the
        rest of a session even after the substrate came back. The positive verdict
        gets the full TTL, because that one authorises sending the credential.
        """
        with self._identity_lock:
            now = time.monotonic()
            cached = self._identity
            if (
                not force
                and cached is not None
                and (now - self._identity_at) < self._identity_ttl(cached)
            ):
                return cached
            verdict = probe_substrate(
                self.base_url,
                timeout=min(self.timeout, IDENTITY_TIMEOUT),
                opener=self._opener,
            )
            self._identity = verdict
            self._identity_at = now
            return verdict

    @staticmethod
    def _identity_ttl(verdict: SubstrateIdentity) -> float:
        """How long this verdict stays valid. A refusal expires fast."""
        return IDENTITY_TTL if verdict.identified else NEGATIVE_IDENTITY_TTL

    def require_substrate(self) -> SubstrateIdentity:
        """Identify the peer, raising if it is not the Hindsight substrate.

        This is the gate every authenticated call passes through: it raises BEFORE
        the bearer is attached, so a peer that is not the substrate never receives the
        credential and never receives a retained transcript.
        """
        verdict = self.identify()
        if not verdict.identified:
            raise SubstrateIdentityError(
                f"the peer at {self.base_url} is not the Hindsight substrate "
                f"({verdict.reason}); no credential was sent"
            )
        return verdict

    @property
    def identity(self) -> Optional[SubstrateIdentity]:
        """The last identity verdict (``None`` before the first probe)."""
        return self._identity

    # -- plumbing ---------------------------------------------------------------

    def _url(self, path: str, params: Mapping[str, Any] | None = None) -> str:
        url = f"{self.base_url}{path}"
        if params:
            clean = {k: v for k, v in params.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean, doseq=True)}"
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
    ) -> Any:
        """One HTTP call with bounded retry on transient failures; returns parsed JSON.

        The bearer is attached ONLY after the peer has been proven to be the
        Hindsight substrate. This is the single choke point through which every
        credentialed request passes, so gating HERE (rather than at each call site)
        means no future call site can leak the credential by forgetting the check.
        """
        # Fail closed: an unverified peer never sees the Authorization header, so it
        # never receives the credential and never receives a retained transcript.
        self.require_substrate()
        url = self._url(path, params)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        last_error: Exception | None = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            req = urllib.request.Request(url, data=data, method=method.upper(), headers=headers)
            try:
                with self._opener(req, timeout=self.timeout) as resp:
                    raw = resp.read()
                if not raw:
                    return {}
                try:
                    return json.loads(raw)
                except ValueError:
                    return {"text": raw.decode("utf-8", "replace")}
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:400]
                except Exception:  # pragma: no cover - body already consumed
                    detail = ""
                if exc.code in RETRY_STATUSES and attempt < len(RETRY_DELAYS):
                    last_error = HindsightError(
                        f"{method.upper()} {path} failed ({exc.code}): {detail}", status=exc.code
                    )
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                # Never echo the request headers: the bearer key must not reach a log.
                raise HindsightError(
                    f"{method.upper()} {path} failed ({exc.code}): {detail}", status=exc.code
                ) from exc
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = exc
                if attempt < len(RETRY_DELAYS):
                    time.sleep(RETRY_DELAYS[attempt])
                    continue
                raise HindsightError(
                    f"{method.upper()} {path} failed: {exc}", status=0
                ) from exc
        raise HindsightError(f"{method.upper()} {path} failed: {last_error}", status=0)

    # -- banks ------------------------------------------------------------------

    def list_banks(self, *, query: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        payload = self._request("GET", "/v1/default/banks", params={"q": query, "limit": limit})
        banks = payload.get("banks") if isinstance(payload, dict) else None
        return list(banks) if isinstance(banks, list) else []

    def get_bank(self, bank_id: str) -> dict[str, Any]:
        """One bank's record, looked up through the bank LIST.

        The substrate has NO ``GET /v1/default/banks/{bank_id}`` route: that path
        answers 405 Method Not Allowed (only DELETE/PATCH/PUT are registered on
        it), so a single-bank GET is a dead route against the live substrate.
        The list route IS the way to read one bank's record, and it carries the
        same fields (``bank_id``, ``fact_count``, ``display_alias``).

        The lookup is by EXACT ``bank_id`` match: ``?q=`` is a substring filter
        over bank id, name and aliases, so a loose match could return a
        different bank entirely.
        """
        wanted = str(bank_id or "").strip()
        if not wanted:
            return {}
        for bank in self.list_banks(query=wanted, limit=1000):
            if not isinstance(bank, dict):
                continue
            if str(bank.get("bank_id") or "").strip() == wanted:
                return bank
        return {}

    def bank_aliases(self, bank_id: str) -> dict[str, Any]:
        """``{"bank_id": ..., "aliases": [{"alias": ..., "primary": bool}, ...]}``."""
        payload = self._request("GET", f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/aliases")
        return payload if isinstance(payload, dict) else {"bank_id": bank_id, "aliases": []}

    def add_bank_alias(self, bank_id: str, alias: str, *, primary: bool = False) -> dict[str, Any]:
        payload = self._request(
            "POST",
            f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/aliases",
            body={"alias": alias, "primary": primary},
        )
        return payload if isinstance(payload, dict) else {}

    def ensure_bank(self, bank_id: str, *, name: str = "") -> dict[str, Any]:
        """Create the bank if it does not exist; return its record.

        The substrate has no ``POST /banks`` — ``PUT /banks/{bank_id}`` is the
        upsert. It is NOT destructive: a field the request omits keeps its current
        value (verified against the live substrate: re-PUTting a bank preserved its
        ``mission``), so this is safe to call for a bank that already exists.
        """
        body: dict[str, Any] = {}
        if name:
            body["name"] = name
        payload = self._request(
            "PUT",
            f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}",
            body=body,
        )
        return payload if isinstance(payload, dict) else {}

    # -- memories ---------------------------------------------------------------

    def retain(
        self,
        bank_id: str,
        content: str,
        *,
        context: str | None = None,
        metadata: Mapping[str, str] | None = None,
        document_id: str | None = None,
        asynchronous: bool = True,
    ) -> dict[str, Any]:
        """Retain one content item into a bank.

        The wire shape is ``{"items": [{content, context?, document_id?}]}`` — the
        substrate's ``RetainRequest`` takes a LIST of items, not a bare ``content``
        field (verified against the live substrate: a bare ``content`` is a 422).

        ``asynchronous=True`` (the default) asks the substrate to extract in the
        background and return an ``operation_id`` immediately. That matters for the
        turn path: a SYNCHRONOUS retain waits for extraction to finish, which on the
        live shared bank (13k facts) measured **2-4s for a normal turn and 9-10s when
        extraction flaked** — and the substrate answers a flaky extraction with a
        **500**, losing the fact entirely. The async path returns in ~24ms, the
        substrate owns the retry (``retry_count``/``next_retry_at`` on the operation
        record), and the retained fact still becomes recall-visible.

        Callers that need the fact to be recall-visible immediately should track the
        returned ``operation_id`` and wait for it (see ``operation_status``).
        """
        item: dict[str, Any] = {"content": content}
        if context:
            item["context"] = context
        if document_id:
            item["document_id"] = document_id
        if metadata:
            item["metadata"] = {str(k): str(v) for k, v in metadata.items()}
        body: dict[str, Any] = {"items": [item]}
        if asynchronous:
            body["async"] = True
        payload = self._request(
            "POST", f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/memories",
            body=body,
        )
        return payload if isinstance(payload, dict) else {}

    def operation_status(self, bank_id: str, operation_id: str) -> dict[str, Any]:
        """The status record of an async retain operation.

        ``status`` is ``pending``/``processing`` while the substrate extracts, then
        ``completed`` or ``failed``. An operation the substrate has evicted (404)
        is reported as ``{"status": "completed"}`` — a missing record is not evidence
        of a failed write, and treating it as one would retry a write that landed.
        """
        try:
            payload = self._request(
                "GET",
                f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}"
                f"/operations/{urllib.parse.quote(operation_id, safe='')}",
            )
        except HindsightError as exc:
            if getattr(exc, "status", None) == 404:
                return {"status": "completed"}
            raise
        return payload if isinstance(payload, dict) else {}

    def recall(
        self,
        bank_id: str,
        query: str,
        *,
        budget: str = "mid",
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"query": query, "budget": budget}
        if limit is not None:
            body["limit"] = int(limit)
        payload = self._request(
            "POST", f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/memories/recall", body=body
        )
        if isinstance(payload, dict):
            results = payload.get("results")
            if isinstance(results, list):
                return results
        return []

    def delete_memory(self, bank_id: str, memory_id: str) -> dict[str, Any]:
        """Retire one retained memory by id (reversible soft-delete).

        The substrate has NO per-memory DELETE route: ``DELETE
        /v1/default/banks/{bank}/memories/{memory}`` is **405 Method Not Allowed**
        (verified against the live substrate; the OpenAPI lists only GET and PATCH
        there). The real per-memory removal is the CURATION route — ``PATCH
        /memories/{memory_id}`` with ``state="invalidated"``, which excludes the
        memory from recall, consolidation and graph maintenance while keeping it
        for audit. That is what this calls.
        """
        payload = self._request(
            "PATCH",
            f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/memories/"
            f"{urllib.parse.quote(memory_id, safe='')}",
            body={"state": "invalidated", "reason": "removed from Hermes"},
        )
        return payload if isinstance(payload, dict) else {}

    def reflect(
        self,
        bank_id: str,
        query: str,
        *,
        budget: str = "mid",
    ) -> str:
        payload = self._request(
            "POST",
            f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/reflect",
            body={"query": query, "budget": budget},
        )
        if isinstance(payload, dict):
            for key in ("answer", "text", "result", "insight"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value
        return ""

    # -- directives -------------------------------------------------------------

    def list_directives(
        self,
        bank_id: str,
        *,
        tags: Iterable[str] | None = None,
        active_only: bool = True,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        tag_list = [str(t) for t in (tags or [])]
        payload = self._request(
            "GET",
            f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/directives",
            params={
                "tags": tag_list or None,
                "active_only": active_only,
                "limit": limit,
            },
        )
        if isinstance(payload, dict):
            items = payload.get("items")
            if isinstance(items, list):
                return items
            directives = payload.get("directives")
            if isinstance(directives, list):
                return directives
        return []

    def create_directive(
        self,
        bank_id: str,
        name: str,
        content: str,
        *,
        priority: int = 10,
        tags: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "name": name,
            "content": content,
            "priority": int(priority),
        }
        tag_list = [str(t) for t in (tags or [])]
        if tag_list:
            body["tags"] = tag_list
        payload = self._request(
            "POST", f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/directives", body=body
        )
        return payload if isinstance(payload, dict) else {}

    def delete_directive(self, bank_id: str, directive_id: str) -> dict[str, Any]:
        payload = self._request(
            "DELETE",
            f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/directives/{urllib.parse.quote(directive_id, safe='')}",
        )
        return payload if isinstance(payload, dict) else {}

    # -- health -----------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        payload = self._request("GET", "/health")
        return payload if isinstance(payload, dict) else {}
