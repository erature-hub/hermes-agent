"""Hermes Sovereign Memory — the Hindsight provider and tool bridge.

Purpose: replace session amnesia with sovereign continuity. Hermes should *just know and
go* — it recalls before it is asked, and it keeps the decisions it makes.

Three sovereign behaviours, all built on the local Hindsight substrate:

1. **Proactive commencement recall.** On session start (and at each turn boundary) the
   provider recalls against the active namespace in the BACKGROUND and stages the result,
   so the first turn already carries context instead of starting blind. Recall never
   blocks a turn: a slow or dead substrate degrades to "no context", never to a hang.

2. **Automatic decision/directive retention (``sync_return``).** Every completed turn is
   retained, and the decisions/directives it contains are promoted into Hindsight
   *directives* — first-class standing instructions the substrate hands back at recall
   time, so a rule the agent decided once keeps applying across sessions.

3. **Bank namespacing.** ``hermes`` (shared) or ``erature/<owner>`` (per owner), resolved
   to the canonical bank id the substrate addresses. See ``_namespaces.py``.

Credential handling: ``HINDSIGHT_API_KEY`` is read through ``agent.secret_scope.get_secret``
so the active profile's scope applies, and the key is never logged or persisted.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from agent.memory_provider import (
    MemoryProvider,
    RecallStatus,
    spawn_context_thread,
)
from agent.secret_scope import get_secret

from ._client import (
    DEFAULT_BASE_URL,
    DEFAULT_TIMEOUT,
    HindsightClient,
    HindsightError,
)
from ._namespaces import (
    DEFAULT_NAMESPACE,
    BankResolver,
    NamespaceError,
    normalize_namespace,
    owner_of,
)

logger = logging.getLogger(__name__)

PROVIDER_NAME = "hermes-memory-hindsight"

#: How long a background recall may run before it is abandoned for this turn.
PREFETCH_TIMEOUT = 12.0
#: Upper bound on how much recalled text is injected into the prompt.
MAX_INJECTED_CHARS = 6000
#: Upper bound on the number of recalled facts injected.
MAX_INJECTED_FACTS = 12
#: The most recent N turns kept in memory for turn-level retention.
TURN_HISTORY_LIMIT = 20

#: A directive/decision cue. Deliberately conservative: an over-eager matcher turns
#: ordinary prose into standing instructions, which is worse than missing one.
_DECISION_CUES = (
    "decided",
    "decision",
    "we will",
    "we'll",
    "going forward",
    "from now on",
    "the rule is",
    "policy is",
    "always ",
    "never ",
    "must ",
    "should always",
    "should never",
    "prefer to",
    "always use",
    "never use",
    "let's stick",
    "lets stick",
)
_DIRECTIVE_MAX_LEN = 600


def _looks_like_directive(text: str) -> bool:
    lowered = text.lower()
    return any(cue in lowered for cue in _DECISION_CUES)


def _directive_name(text: str, *, max_len: int = 60) -> str:
    """A stable, human-readable name for a directive derived from a turn."""
    cleaned = re.sub(r"\s+", " ", text).strip()
    if not cleaned:
        return "decision"
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1].rstrip() + "…"


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text or "")
    return [p.strip() for p in parts if p and p.strip()]


def _config_path() -> Path:
    """Profile-scoped config file: ``$HERMES_HOME/hermes-memory-hindsight/config.json``.

    This is the same path the Dashboard's flat-json storage writes
    (``get_hermes_home()/<provider name>/config.json``), so the panel, ``hermes memory
    setup`` and the running provider all read one file.
    """
    from hermes_constants import get_hermes_home

    return get_hermes_home() / PROVIDER_NAME / "config.json"


def _load_config() -> dict[str, Any]:
    """Provider config: the profile-scoped JSON file, then the config.yaml block.

    The JSON file is the surface the Dashboard writes; the config.yaml block is the
    hand-editable fallback (``memory.hermes-memory-hindsight``). Values from the file
    win, so a panel edit is authoritative.
    """
    cfg: dict[str, Any] = {}
    try:
        path = _config_path()
        if path.exists():
            raw = path.read_text(encoding="utf-8-sig")
            data = json.loads(raw) if raw.strip() else {}
            if isinstance(data, dict):
                cfg.update(data)
    except Exception:
        logger.debug("hindsight: provider config file unreadable; using config.yaml only")
    try:
        from hermes_cli.config import load_config_readonly

        block = load_config_readonly().get("memory", {}).get(PROVIDER_NAME, {})
    except Exception:
        block = None
    if isinstance(block, dict):
        for key, value in block.items():
            cfg.setdefault(key, value)
    return cfg


def _schema(name: str, description: str, properties: dict[str, Any] | None = None,
            required: tuple = ()) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": properties or {},
            "required": list(required),
        },
    }


def _p(description: str, type_: str = "string", **extra: Any) -> dict[str, Any]:
    return {"type": type_, **extra, "description": description}


_SCHEMAS = (
    _schema(
        "hindsight_recall",
        "Recall long-term memory from the sovereign Hindsight substrate for the current task.",
        {"query": _p("What to recall.", minLength=1), "budget": _p("Recall budget: low, mid or high.", enum=["low", "mid", "high"])},
        ("query",),
    ),
    _schema(
        "hindsight_reflect",
        "Ask the sovereign memory substrate to reason across everything it knows and answer.",
        {"query": _p("The question to reflect on.", minLength=1), "budget": _p("Budget: low, mid or high.", enum=["low", "mid", "high"])},
        ("query",),
    ),
    _schema(
        "hindsight_retain",
        "Persist a fact, preference or decision into the sovereign memory substrate.",
        {
            "content": _p("The fact, preference or decision to remember.", minLength=1),
            "as_directive": _p("Also promote this to a standing directive that applies to future sessions.", type_="boolean"),
        },
        ("content",),
    ),
    _schema(
        "hindsight_directives",
        "List the standing directives the sovereign substrate holds for this namespace.",
    ),
)


class HindsightMemoryProvider(MemoryProvider):
    """Sovereign memory provider backed by the local Hindsight substrate."""

    def __init__(self) -> None:
        self._client: Optional[HindsightClient] = None
        self._resolver: Optional[BankResolver] = None
        self._namespace: str = DEFAULT_NAMESPACE
        self._bank_id: str = DEFAULT_NAMESPACE
        self._owner: str = ""
        self._budget: str = "mid"
        self._session_id: str = ""
        self._hermes_home: str = ""
        self._platform: str = ""
        self._agent_context: str = "primary"
        self._api_key: str = ""
        self._base_url: str = DEFAULT_BASE_URL
        self._timeout: float = DEFAULT_TIMEOUT

        # Commencement recall state: a background recall stages its result here, and the
        # next prefetch() consumes it. One lock, one slot — the newest staged recall wins.
        self._lock = threading.Lock()
        self._prefetch_text: str = ""
        self._prefetch_count: int = 0
        self._prefetch_stamp: float = 0.0
        self._recall_thread: Optional[threading.Thread] = None
        self._turn_history: list[dict[str, str]] = []
        self._session_ended = False
        self._shutdown = False

    # -- identity ---------------------------------------------------------------

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    def is_available(self) -> bool:
        """Configured and credentialed? No network here — activation must not block."""
        try:
            return bool(self._resolve_api_key())
        except Exception:
            return False

    def unavailable_reason(self) -> str:
        if self.is_available():
            return ""
        return (
            "HINDSIGHT_API_KEY is not set for this profile. Add it to the profile .env "
            "(or the shared Hermes .env) and restart, or run `hermes memory setup "
            "hermes-memory-hindsight`."
        )

    def _resolve_api_key(self) -> str:
        key = get_secret("HINDSIGHT_API_KEY", "") or ""
        return key.strip()

    # -- configuration ----------------------------------------------------------

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {
                "key": "api_key",
                "description": "Hindsight API key (bearer token for the substrate).",
                "secret": True,
                "required": True,
                "env_var": "HINDSIGHT_API_KEY",
                "url": "http://127.0.0.1:8888",
            },
            {
                "key": "base_url",
                "description": "Hindsight substrate base URL.",
                "default": DEFAULT_BASE_URL,
            },
            {
                "key": "namespace",
                "description": (
                    "Memory namespace: 'hermes' for the shared substrate, or "
                    "'erature/<owner>' for a per-owner namespace."
                ),
                "default": DEFAULT_NAMESPACE,
            },
            {
                "key": "budget",
                "description": "Recall budget: low, mid or high.",
                "default": "mid",
                "choices": ["low", "mid", "high"],
            },
            {
                "key": "commitment",
                "description": "Promote detected decisions into standing directives automatically.",
                "type": "boolean",
                "default": True,
            },
        ]

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        """Merge non-secret setup values into the provider's own config file.

        Writes ``<hermes_home>/hermes-memory-hindsight/config.json`` (0600) — the same
        file the Dashboard panel writes, so the two surfaces never disagree. Secrets are
        NOT written here: the API key belongs in the profile .env.
        """
        from utils import atomic_json_write

        path = Path(hermes_home) / PROVIDER_NAME / "config.json"
        existing: dict[str, Any] = {}
        try:
            if path.exists():
                raw = path.read_text(encoding="utf-8-sig")
                data = json.loads(raw) if raw.strip() else {}
                if isinstance(data, dict):
                    existing = data
        except Exception:
            existing = {}
        for key in ("base_url", "namespace", "budget", "commitment"):
            if key in values and values[key] not in (None, ""):
                existing[key] = values[key]
        atomic_json_write(path, existing, mode=0o600)

    # -- lifecycle --------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        """Bind the provider to one profile and stage the commencement recall."""
        self._session_id = session_id or ""
        self._hermes_home = str(kwargs.get("hermes_home") or "")
        self._platform = str(kwargs.get("platform") or "")
        self._agent_context = str(kwargs.get("agent_context") or "primary")

        cfg = _load_config()
        self._base_url = str(
            get_secret("HINDSIGHT_API_URL", "") or cfg.get("base_url") or DEFAULT_BASE_URL
        ).rstrip("/")
        self._api_key = self._resolve_api_key()
        try:
            self._timeout = float(
                get_secret("HINDSIGHT_TIMEOUT", "") or cfg.get("timeout") or DEFAULT_TIMEOUT
            )
        except (TypeError, ValueError):
            self._timeout = DEFAULT_TIMEOUT
        self._budget = str(cfg.get("budget") or "mid")
        if self._budget not in ("low", "mid", "high"):
            self._budget = "mid"

        namespace = str(cfg.get("namespace") or DEFAULT_NAMESPACE)
        try:
            self._namespace = normalize_namespace(namespace)
        except NamespaceError as exc:
            logger.warning("hindsight: %s; falling back to %r", exc, DEFAULT_NAMESPACE)
            self._namespace = DEFAULT_NAMESPACE
        self._owner = owner_of(self._namespace)

        if not self._api_key:
            logger.warning(
                "hindsight: no HINDSIGHT_API_KEY for this profile — sovereign memory is inactive"
            )
            return

        self._client = HindsightClient(self._base_url, self._api_key, timeout=self._timeout)
        self._resolver = BankResolver(self._client)
        try:
            self._bank_id = self._resolver.ensure_namespace(self._namespace)
        except HindsightError as exc:
            logger.warning("hindsight: namespace resolution failed: %s", exc)
            self._bank_id = self._namespace

        logger.info(
            "hindsight: sovereign memory ready (bank=%s namespace=%s url=%s)",
            self._bank_id,
            self._namespace,
            self._base_url,
        )
        self._start_commencement_recall()

    def shutdown(self) -> None:
        self._shutdown = True
        thread = self._recall_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)

    # -- commencement recall ----------------------------------------------------

    def _start_commencement_recall(self, query: str = "") -> None:
        """Kick a background recall so the NEXT turn starts with context.

        This is the "commencement" hook: it fires at session start (before the user has
        said anything meaningful) and again at each turn boundary. It is deliberately
        background — the substrate may be slow, and a slow substrate must never stall a
        turn.
        """
        if self._client is None or self._shutdown:
            return
        with self._lock:
            if self._recall_thread is not None and self._recall_thread.is_alive():
                return
        search = query.strip() or self._commencement_query()
        thread = spawn_context_thread(
            self._run_recall, name="hindsight-commencement-recall", kwargs={"query": search}
        )
        self._recall_thread = thread
        thread.start()

    def _commencement_query(self) -> str:
        """The query used for the startup recall.

        Derived from the profile's own identity so the substrate returns what is relevant
        to THIS agent (its user, its standing work), not a generic greeting match.
        """
        owner = self._owner or "the user"
        return (
            f"Standing context for {owner}: active directives, decisions, preferences, "
            f"ongoing work and commitments."
        )

    def _run_recall(self, query: str) -> None:
        """Background recall worker: stage the result for the next prefetch()."""
        client = self._client
        if client is None:
            return
        try:
            facts = client.recall(self._bank_id, query, budget=self._budget)
            directives = self._fetch_directives()
        except HindsightError as exc:
            logger.debug("hindsight: commencement recall failed: %s", exc)
            return
        text, count = self._format_context(facts, directives)
        with self._lock:
            self._prefetch_text = text
            self._prefetch_count = count
            self._prefetch_stamp = time.time()

    def _fetch_directives(self) -> list[dict[str, Any]]:
        """Active standing directives for this namespace (never raises)."""
        client = self._client
        if client is None:
            return []
        try:
            return client.list_directives(self._bank_id, active_only=True, limit=50)
        except HindsightError as exc:
            logger.debug("hindsight: directive read failed: %s", exc)
            return []

    def _format_context(
        self, facts: list[dict[str, Any]], directives: list[dict[str, Any]]
    ) -> tuple[str, int]:
        """Render recalled facts + directives into the block injected into the prompt."""
        blocks: list[str] = []
        count = 0

        if directives:
            lines = []
            for directive in directives[:MAX_INJECTED_FACTS]:
                content = str(directive.get("content") or "").strip()
                if not content:
                    continue
                name = str(directive.get("name") or "directive").strip()
                lines.append(f"- {name}: {content}")
                count += 1
            if lines:
                blocks.append("### Standing directives\n" + "\n".join(lines))

        fact_lines = []
        for fact in facts[:MAX_INJECTED_FACTS]:
            if not isinstance(fact, dict):
                continue
            text = str(fact.get("text") or "").strip()
            if not text:
                continue
            fact_lines.append(f"- {text}")
            count += 1
        if fact_lines:
            blocks.append("### Recalled context\n" + "\n".join(fact_lines))

        if not blocks:
            return "", 0
        rendered = "\n\n".join(blocks)
        if len(rendered) > MAX_INJECTED_CHARS:
            # Reserve room for the ellipsis so the RESULT is within the cap, not the cap
            # plus one character.
            rendered = rendered[: max(0, MAX_INJECTED_CHARS - 1)].rstrip() + "…"
        return rendered, count

    # -- MemoryProvider surface -------------------------------------------------

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return the staged context (fast — the recall already ran in background)."""
        with self._lock:
            text = self._prefetch_text
            self._prefetch_text = ""
        if not text:
            # Nothing staged yet: stage a recall now so the NEXT turn has context.
            self._start_commencement_recall(query)
            return ""
        return text

    def recall_status(self) -> Optional[RecallStatus]:
        with self._lock:
            count = self._prefetch_count
            self._prefetch_count = 0
        if not count:
            return None
        return RecallStatus(provider_label="Sovereign memory", count=count, glyph="🧿")

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Stage the next turn's recall in the background."""
        self._start_commencement_recall(query)

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """Per-turn tick: refresh the standing context for the upcoming message."""
        if self._shutdown:
            return
        self._start_commencement_recall(message or "")

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs,
    ) -> None:
        """Rebind per-session state; a genuinely new conversation clears staged context."""
        self._session_id = new_session_id or ""
        if reset:
            with self._lock:
                self._prefetch_text = ""
                self._prefetch_count = 0
            self._turn_history.clear()
        self._start_commencement_recall()

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """Session boundary: retain the closing context once, then stop."""
        if self._session_ended or self._shutdown:
            return
        self._session_ended = True
        if not self._should_write():
            return
        try:
            user_content, assistant_content = _last_exchange(messages)
        except Exception:
            return
        if user_content or assistant_content:
            self._retain_turn(user_content, assistant_content, final=True)

    # -- retention (sync_turn) --------------------------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[list[dict[str, Any]]] = None,
        turn_author: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist a completed turn and promote its decisions into directives."""
        if self._shutdown or not self._should_write():
            return
        self._retain_turn(user_content, assistant_content)
        self._promote_decisions(user_content, assistant_content)

    def _should_write(self) -> bool:
        """Only the primary agent writes. Subagent/cron/flush contexts do not."""
        if self._client is None:
            return False
        return self._agent_context == "primary"

    def _retain_turn(
        self, user_content: str, assistant_content: str, *, final: bool = False
    ) -> None:
        """Retain one turn into the substrate. Never raises into the turn loop."""
        client = self._client
        if client is None:
            return
        user_text = (user_content or "").strip()
        assistant_text = (assistant_content or "").strip()
        if not user_text and not assistant_text:
            return
        self._turn_history.append({"user": user_text, "assistant": assistant_text})
        if len(self._turn_history) > TURN_HISTORY_LIMIT:
            del self._turn_history[: len(self._turn_history) - TURN_HISTORY_LIMIT]
        content = f"User: {user_text}\nAssistant: {assistant_text}".strip()
        metadata = {
            "source": "hermes-agent",
            "namespace": self._namespace,
            "platform": self._platform or "unknown",
            "session_id": self._session_id or "unknown",
            "kind": "session_end" if final else "turn",
        }
        try:
            client.retain(
                self._bank_id,
                content,
                context="conversation turn" if not final else "session summary",
                metadata=metadata,
            )
        except HindsightError as exc:
            logger.debug("hindsight: turn retention failed: %s", exc)

    def _promote_decisions(self, user_content: str, assistant_content: str) -> None:
        """Turn detected decisions/directives into standing Hindsight directives.

        A decision is promoted once: the directive name is the decision text, and an
        existing directive with that name is left alone (no duplicate spam).
        """
        client = self._client
        if client is None:
            return
        cfg = _load_config()
        if cfg.get("commitment") is False:
            return
        candidates = _extract_directive_candidates(user_content, assistant_content)
        if not candidates:
            return
        try:
            existing = client.list_directives(self._bank_id, active_only=False, limit=200)
        except HindsightError as exc:
            logger.debug("hindsight: directive listing failed: %s", exc)
            return
        known = {
            str(d.get("name") or "").strip() for d in existing if isinstance(d, dict)
        }
        for text in candidates:
            name = _directive_name(text)
            if not name or name in known:
                continue
            try:
                client.create_directive(
                    self._bank_id,
                    name,
                    text,
                    priority=10,
                    tags=["hermes", "auto"],
                )
                known.add(name)
            except HindsightError as exc:
                logger.debug("hindsight: directive creation failed: %s", exc)

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
    ) -> None:
        """Mirror a built-in memory-tool write into the sovereign substrate.

        Hermes' built-in MEMORY/USER stores are the most durable artifact an operator
        has — an explicit "remember this" note. Without mirroring, that note lives
        only on local disk and the substrate cannot recall it, which is precisely the
        amnesia this provider exists to remove.

        ``add`` and ``replace`` are retained. A ``remove`` cannot be applied
        precisely (the substrate's delete is by memory id, and the mirrored copy has
        no stable id), so the retraction is recorded instead: the substrate's
        extractor sees "X was retracted" and can drop the superseded claim.
        """
        if self._shutdown or not self._client or not self._should_write():
            return
        text = (content or "").strip()
        if not text:
            return
        action = (action or "").strip().lower()
        store = "user profile" if str(target).strip().lower() == "user" else "agent memory"
        if action == "remove":
            text = f"Retracted from the Hermes {store}: {text}"
        else:
            text = f"From the Hermes {store}: {text}"
        attrs = {
            "source": "hermes-agent",
            "namespace": self._namespace,
            "kind": f"memory_{action or 'write'}",
            "store": store,
        }
        if isinstance(metadata, dict):
            for key in ("write_origin", "tool_name", "session_id"):
                value = metadata.get(key)
                if value:
                    attrs[key] = str(value)
        try:
            self._client.retain(self._bank_id, text, context=store, metadata=attrs)
        except HindsightError as exc:
            logger.debug("hindsight: memory mirror failed: %s", exc)

    # -- tool bridge ------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return list(_SCHEMAS)

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        """Dispatch one sovereign-memory tool; always returns a JSON string."""
        try:
            if tool_name == "hindsight_recall":
                return json.dumps(self._tool_recall(args))
            if tool_name == "hindsight_reflect":
                return json.dumps(self._tool_reflect(args))
            if tool_name == "hindsight_retain":
                return json.dumps(self._tool_retain(args))
            if tool_name == "hindsight_directives":
                return json.dumps(self._tool_directives())
            return json.dumps({"error": f"Unknown tool {tool_name!r}"})
        except Exception as exc:  # a tool must never take the turn down
            logger.debug("hindsight tool %s failed: %s", tool_name, exc)
            return json.dumps({"error": str(exc)})

    def _require_client(self) -> HindsightClient:
        if self._client is None:
            raise RuntimeError("sovereign memory is not initialized for this profile")
        return self._client

    def _tool_recall(self, args: dict[str, Any]) -> dict[str, Any]:
        client = self._require_client()
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        budget = str(args.get("budget") or self._budget)
        facts = client.recall(self._bank_id, query, budget=budget)
        return {
            "query": query,
            "bank": self._bank_id,
            "namespace": self._namespace,
            "results": [
                {
                    "id": f.get("id"),
                    "text": f.get("text"),
                    "type": f.get("type"),
                    "entities": f.get("entities"),
                }
                for f in facts
                if isinstance(f, dict)
            ],
        }

    def _tool_reflect(self, args: dict[str, Any]) -> dict[str, Any]:
        client = self._require_client()
        query = str(args.get("query") or "").strip()
        if not query:
            return {"error": "query is required"}
        budget = str(args.get("budget") or self._budget)
        answer = client.reflect(self._bank_id, query, budget=budget)
        return {
            "query": query,
            "bank": self._bank_id,
            "answer": answer,
        }

    def _tool_retain(self, args: dict[str, Any]) -> dict[str, Any]:
        client = self._require_client()
        content = str(args.get("content") or "").strip()
        if not content:
            return {"error": "content is required"}
        result = client.retain(
            self._bank_id,
            content,
            context="explicit memory",
            metadata={"source": "hermes-agent", "namespace": self._namespace, "kind": "tool"},
        )
        promoted = None
        if args.get("as_directive"):
            try:
                promoted = client.create_directive(
                    self._bank_id,
                    _directive_name(content),
                    content,
                    priority=10,
                    tags=["hermes", "manual"],
                )
            except HindsightError as exc:
                logger.debug("hindsight: directive promotion failed: %s", exc)
        return {"retained": bool(result), "bank": self._bank_id, "directive": promoted}

    def _tool_directives(self) -> dict[str, Any]:
        client = self._require_client()
        directives = client.list_directives(self._bank_id, active_only=True, limit=100)
        return {
            "bank": self._bank_id,
            "namespace": self._namespace,
            "directives": [
                {
                    "id": d.get("id"),
                    "name": d.get("name"),
                    "content": d.get("content"),
                    "priority": d.get("priority"),
                    "tags": d.get("tags"),
                }
                for d in directives
                if isinstance(d, dict)
            ],
        }

    # -- introspection ----------------------------------------------------------

    def identity_signature(self) -> dict[str, Any]:
        """Identity-mapping values that must bust a cached gateway agent when changed."""
        cfg = _load_config()
        return {
            "hermes_memory_hindsight.namespace": self._namespace or str(cfg.get("namespace") or ""),
            "hermes_memory_hindsight.bank": self._bank_id,
        }

    def get_context(self) -> dict[str, Any]:
        """Small read-only snapshot for diagnostics (never raises)."""
        snapshot: dict[str, Any] = {
            "namespace": self._namespace,
            "bank": self._bank_id,
            "owner": self._owner,
            "base_url": self._base_url,
            "budget": self._budget,
            "credentialed": bool(self._api_key),
        }
        if self._resolver is not None:
            snapshot["namespaces"] = self._resolver.namespaces()
        return snapshot


def _extract_directive_candidates(user_content: str, assistant_content: str) -> list[str]:
    """Decision/directive sentences worth promoting to a standing instruction.

    Only the ASSISTANT side is mined: the user's own request is context, not a rule the
    agent adopted. A short list, de-duplicated, each capped in length.
    """
    out: list[str] = []
    seen: set[str] = set()
    for sentence in _sentences(assistant_content or ""):
        if len(sentence) > _DIRECTIVE_MAX_LEN:
            continue
        if not _looks_like_directive(sentence):
            continue
        key = sentence.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(sentence)
        if len(out) >= 3:
            break
    return out


def _last_exchange(messages: list[dict[str, Any]]) -> tuple[str, str]:
    """The final user/assistant text pair of a transcript."""
    user_text = ""
    assistant_text = ""
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        content = message.get("content")
        if isinstance(content, list):
            content = " ".join(
                str(part.get("text") or "") for part in content if isinstance(part, dict)
            )
        text = str(content or "").strip()
        if not text:
            continue
        if role == "user":
            user_text = text
        elif role == "assistant":
            assistant_text = text
    return user_text, assistant_text


def register(ctx) -> None:
    """Plugin registration entry point."""
    ctx.register_memory_provider(HindsightMemoryProvider())
