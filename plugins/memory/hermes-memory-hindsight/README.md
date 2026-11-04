# Hermes Sovereign Memory — Hindsight Provider & Tool Bridge

Bridges Hermes agent sessions to the **local** Hindsight episodic-memory substrate, so
Hermes stops starting every session blind and starts *knowing and going*.

This is Erature's sovereign provider. It is deliberately distinct from the community
`hindsight` catalog plugin: it is local-only, it adds the two behaviours the community
plugin does not have (proactive commencement recall and decision/directive retention),
and it adds the `hermes` / `erature/<owner>` namespace.

## What it does

### 1. Proactive commencement recall

At session start — and at every turn boundary — the provider recalls against the
active namespace **in the background** and stages the result. The next turn already
carries context instead of starting from nothing.

The recall is deliberately off the turn's critical path: a slow or dead substrate
degrades to "no context", never to a hang. A turn is never blocked by memory.

### 2. Automatic decision/directive retention (`sync_turn`)

Every completed turn is retained. Beyond that, a *decision* the agent made is promoted
into a Hindsight **directive** — a first-class standing instruction the substrate hands
back at recall time. A rule the agent decided once keeps applying across sessions.

Only the assistant's side is mined for decisions (the user's request is context, not a
rule the agent adopted), the cue list is conservative, and a decision is promoted
**once** — an existing directive with the same name is left alone.

Only the **primary** agent context writes. Subagent, cron and flush contexts do not.

> **Retains are asynchronous.** The substrate accepts a retain immediately and
> extracts the fact in the background. That is deliberate: measured against the live
> shared bank (13k facts), a *synchronous* retain took 2–4s for a normal turn and
> 9–10s when extraction flaked — and a flaky extraction answers **500**, losing the
> fact outright. The async path returns in ~24ms, the substrate owns the retry
> (`retry_count` / `next_retry_at` on the operation record), and the write is not lost.
>
> The provider tracks each operation id and waits (bounded, **in the background**) for
> it to settle before the next recall — so a turn is recall-visible on the next turn
> without the reply ever waiting on the substrate.

### 3. Built-in memory mirror

Hermes' built-in MEMORY/USER stores are the operator's most durable artifact — an
explicit "remember this" note. They are mirrored into the substrate, so a note written
on one surface is recallable on every other. Without this, the note lives only on local
disk, which is exactly the amnesia this provider exists to remove.

A `remove` cannot be applied precisely: a mirrored copy has no stable memory id, so
there is nothing to invalidate. The retraction is *recorded* instead — the extractor
sees "X was retracted" and can drop the superseded claim.

> **The substrate has no per-memory DELETE.** `DELETE
> /v1/default/banks/{bank}/memories/{memory}` answers **405 Method Not Allowed**
> (verified against the live substrate; the OpenAPI lists only GET and PATCH there).
> The real per-memory removal is the **curation** route — `PATCH
> /memories/{memory_id}` with `state="invalidated"`, which excludes the memory from
> recall, consolidation and graph maintenance while keeping it for audit. The
> client's `delete_memory()` uses that route.

> **There is no single-bank GET either.** `GET /v1/default/banks/{bank_id}` answers
> **405 Method Not Allowed** — that path registers only DELETE/PATCH/PUT. A bank is
> therefore read through the LIST route (`GET /v1/default/banks?q=…`), matched on an
> EXACT `bank_id`, because `q` is a substring filter over bank id, name and aliases.
> The client's `get_bank()` does that.

### 4. Bank namespacing

| Namespace | Meaning |
|---|---|
| `hermes` | the shared personal substrate |
| `erature/<owner>` | a per-owner namespace, e.g. `erature/paul` |

A bare owner name (`paul`) is accepted as the ergonomic spelling of `erature/paul`.

> **Each owner gets its OWN bank.** An owner namespace is provisioned as a dedicated
> bank (`erature/paul` → the bank `erature-paul`), not the shared one — otherwise
> every owner reads and writes the same corpus and the namespace stops being a
> boundary. The dedicated bank is created on first use and reused thereafter.
>
> A lossy owner name (`pa ul` → `pa-ul`) gets a short digest appended, so two
> different owners can never be provisioned onto one bank.

> **Substrate routing constraint (verified, not assumed).** Hindsight addresses a bank
> by a REST *path segment*, and its router does not decode `%2F` into a bank id — so a
> slash can never be addressed directly. The slash form is therefore carried as an
> **alias** on the canonical bank, and the provider resolves a namespace to the
> canonical bank id it actually addresses. The alias is what makes `erature/<owner>`
> visible in the substrate (`matched_aliases` on the bank list).

### 5. Substrate identity gate (the credential never reaches a stranger)

Before anything is sent, the peer on the configured `base_url` must **prove it is the
Hindsight substrate**. The proof is an **unauthenticated** probe of two endpoints:

| Probe | Signal | Why |
|---|---|---|
| `GET /health` | `{"status": "healthy", …}` | liveness — but a GENERIC shape |
| `GET /openapi.json` | `info.title` starts with `Hindsight` | **product identity** — decisive |

Only after both pass is the `Authorization: Bearer …` header attached. The gate sits
in the transport's single request choke point, so **no call site can leak the
credential by forgetting the check**.

> **Why this matters, concretely.** The substrate is a *local* service on a fixed
> port, and that port can be taken over by an unrelated product. It was: the
> substrate was decommissioned on 2026-11-01 and a third-party product took `:8888`.
> It was restored on 2026-11-04 and now answers on `:9999`; `:8888` is Unsloth Studio.
> Without the gate, the provider would have handed that product the API key **and**
> every `User:/Assistant:` transcript it retained — 4 of 4 requests carried the
> bearer, and the provider still reported a healthy bank.

> **The probe itself carries no bearer.** `client.health()` cannot be reused for it:
> that routes through the request path, which attaches the credential — probing
> through it would hand the secret to the very peer being vetted. The probe reads
> only a bounded prefix of the OpenAPI document (the `info` block sits in the first
> few hundred bytes), so a peer cannot make it buffer an unbounded body.

> **The verdict expires.** A positive verdict is cached for 60s and a refusal for 5s.
> Without expiry, a verdict earned against the real substrate would keep
> authorising the bearer to be sent to whatever later took the port. A refusal
> expires quickly so a substrate that comes back up is picked up again.

> **This is a misconfiguration guard, not authentication.** A local process that
> deliberately impersonates the substrate can still pass it; that threat needs
> mutual TLS or a pinned digest, not an unauthenticated probe.

When the peer is refused the provider stays **inert** (no recall, no retain, no
tools) and says so: `identity_warning()` names the peer and the reason, and
`get_context()` reports `substrate_identified: false` rather than a healthy-looking
bank.

## Configuration

Non-secret settings live in `$HERMES_HOME/hermes-memory-hindsight/config.json`
(profile-scoped, mode 0600) — the same file the Dashboard panel writes — with a
`memory.hermes-memory-hindsight` config.yaml block as the hand-editable fallback.

| Key | Default | Meaning |
|---|---|---|
| `base_url` | `http://127.0.0.1:9999` | substrate base URL |
| `namespace` | `hermes` | `hermes` or `erature/<owner>` |
| `budget` | `mid` | recall budget: `low` / `mid` / `high` |
| `commitment` | `true` | promote detected decisions into directives |

The credential is `HINDSIGHT_API_KEY`, read through `agent.secret_scope.get_secret` so
the active profile's scope applies (and a multiplexed profile never inherits another
profile's key). It is never logged, never written to the repo, and never included in
an error message.

As a fallback, an `api_key` in the provider's own `config.json` is honored — that is
the same location the Dashboard's flat-json storage and the community `hindsight`
plugin use, so a key placed there activates the provider. The **env store wins** when
both are present, so a rotated key is never shadowed by a stale file value.

Activate it with `memory.provider: hermes-memory-hindsight` in the profile's
`config.yaml`.

## Tools

| Tool | Purpose |
|---|---|
| `hindsight_recall` | ranked recall from the substrate |
| `hindsight_reflect` | reasoned answer across everything the substrate knows |
| `hindsight_retain` | persist a fact / preference / decision (optionally as a directive) |
| `hindsight_directives` | list the standing directives for this namespace |

## Layout

```
plugins/memory/hermes-memory-hindsight/
├── __init__.py       # the MemoryProvider: lifecycle, retention, tool bridge
├── _client.py        # stdlib REST transport (bearer auth, bounded retry)
├── _namespaces.py    # namespace -> canonical bank id resolution
├── config_schema.py  # declarative config surface for the Dashboard
└── plugin.yaml       # plugin manifest
```

Stdlib only: the substrate is a local container, so the plugin pulls no client
dependency into the Hermes install.

## Tests

```bash
./.venv/Scripts/python.exe -m pytest tests/test_hindsight_provider.py -q
```

The suite is **hermetic**: it runs against a fake transport and an autouse fixture
makes any real outbound socket an immediate failure, so it can never silently touch a
live substrate.

Live end-to-end verification against the real substrate:

```bash
./.venv/Scripts/python.exe scripts/verify_hindsight_provider.py
```

Live verification of the identity gate against the real peers (the substrate, and
whatever unrelated product currently owns the default port):

```bash
./.venv/Scripts/python.exe scripts/verify_substrate_identity.py
```

Read-only checks run against the **real** shared bank (that is where the standing
directives and the real recall corpus live). Write checks run against a dedicated
**throwaway bank** which is deleted afterwards — the substrate has no per-memory
`DELETE`, only a destructive whole-bank clear, so writing into the shared bank would
leave residue that cannot be removed one memory at a time. The net effect on the
substrate is none.
