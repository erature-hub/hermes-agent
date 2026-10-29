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
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable, Mapping

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8888"
DEFAULT_TIMEOUT = 30.0
# Transient failures only (connection reset / 5xx). 4xx is a contract error: never retried,
# because retrying a rejected request against a local substrate just burns the turn budget.
RETRY_STATUSES = frozenset({500, 502, 503, 504})
RETRY_DELAYS = (0.25, 0.75)


class HindsightError(RuntimeError):
    """A Hindsight request failed. ``status`` is 0 for transport failures."""

    def __init__(self, message: str, *, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


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
        """One HTTP call with bounded retry on transient failures; returns parsed JSON."""
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
        payload = self._request("GET", f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}")
        return payload if isinstance(payload, dict) else {}

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

    # -- memories ---------------------------------------------------------------

    def retain(
        self,
        bank_id: str,
        content: str,
        *,
        context: str | None = None,
        metadata: Mapping[str, str] | None = None,
        document_id: str | None = None,
    ) -> dict[str, Any]:
        """Retain one content item into a bank.

        The wire shape is ``{"items": [{content, context?, document_id?}]}`` — the
        substrate's ``RetainRequest`` takes a LIST of items, not a bare ``content``
        field (verified against the live substrate: a bare ``content`` is a 422).
        """
        item: dict[str, Any] = {"content": content}
        if context:
            item["context"] = context
        if document_id:
            item["document_id"] = document_id
        if metadata:
            item["metadata"] = {str(k): str(v) for k, v in metadata.items()}
        payload = self._request(
            "POST", f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/memories",
            body={"items": [item]},
        )
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
        """Delete one retained memory by id."""
        payload = self._request(
            "DELETE",
            f"/v1/default/banks/{urllib.parse.quote(bank_id, safe='')}/memories/"
            f"{urllib.parse.quote(memory_id, safe='')}",
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
