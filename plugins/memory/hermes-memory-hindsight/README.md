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

### 3. Built-in memory mirror

Hermes' built-in MEMORY/USER stores are the operator's most durable artifact — an
explicit "remember this" note. They are mirrored into the substrate, so a note written
on one surface is recallable on every other. Without this, the note lives only on local
disk, which is exactly the amnesia this provider exists to remove.

A `remove` cannot be applied precisely (the substrate's delete is by memory id, and a
mirrored copy has no stable id), so the retraction is *recorded* instead — the
extractor sees "X was retracted" and can drop the superseded claim.

### 4. Bank namespacing

| Namespace | Meaning |
|---|---|
| `hermes` | the shared personal substrate |
| `erature/<owner>` | a per-owner namespace, e.g. `erature/paul` |

A bare owner name (`paul`) is accepted as the ergonomic spelling of `erature/paul`.

> **Substrate routing constraint (verified, not assumed).** Hindsight addresses a bank
> by a REST *path segment*, and its router does not decode `%2F` into a bank id — so a
> slash can never be addressed directly. The slash form is therefore carried as an
> **alias** on the canonical bank, and the provider resolves a namespace to the
> canonical bank id it actually addresses. The alias is what makes `erature/<owner>`
> visible in the substrate (`matched_aliases` on the bank list).

## Configuration

Non-secret settings live in `$HERMES_HOME/hermes-memory-hindsight/config.json`
(profile-scoped, mode 0600) — the same file the Dashboard panel writes — with a
`memory.hermes-memory-hindsight` config.yaml block as the hand-editable fallback.

| Key | Default | Meaning |
|---|---|---|
| `base_url` | `http://127.0.0.1:8888` | substrate base URL |
| `namespace` | `hermes` | `hermes` or `erature/<owner>` |
| `budget` | `mid` | recall budget: `low` / `mid` / `high` |
| `commitment` | `true` | promote detected decisions into directives |

The credential is `HINDSIGHT_API_KEY`, read through `agent.secret_scope.get_secret` so
the active profile's scope applies (and a multiplexed profile never inherits another
profile's key). It is never logged, never written to the repo, and never included in
an error message.

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

Read-only checks run against the **real** shared bank (that is where the standing
directives and the real recall corpus live). Write checks run against a dedicated
**throwaway bank** which is deleted afterwards — the substrate has no per-memory
`DELETE`, only a destructive whole-bank clear, so writing into the shared bank would
leave residue that cannot be removed one memory at a time. The net effect on the
substrate is none.
