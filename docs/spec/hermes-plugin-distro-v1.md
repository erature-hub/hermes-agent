# Hermes Plugin Distribution Specification — v1

**Status:** Normative (format version `distribution_version: 1`)
**Scope:** The `@scope/name` plugin distribution format, its trust model, its install
lifecycle, and the publisher tooling that produces it.
**Reference implementation:** `hermes_cli/plugin_distributions.py` (format + trust),
`hermes_cli/plugin_distributions_install.py` (lifecycle),
`plugins/distribution/packager.py` (publisher tooling).

This document specifies how an external operator installs a *complete, multi-plugin
stack* with a single command:

```
hermes plugins install @erature/vertical-stack
```

and how a publisher signs, publishes, and updates such a stack.

---

## 1. Purpose and scope

### 1.1 What a distribution is

A **distribution** is a named, signed bundle of plugin *components* that together
constitute one coherent capability — a "vertical stack". The first published
distribution is `@erature/vertical-stack`, which fills four roles:

| Role          | Component                 | Capability                                       |
| ------------- | ------------------------- | ------------------------------------------------ |
| `memory`      | `hermes-memory-hindsight` | episodic memory substrate (`memory_provider`)     |
| `reader`      | `hermes-rss`              | memory-aware ingestion surface (`hook:feed_subscribe`) |
| `dispatcher`  | `advanced-delegation`     | specialized task dispatch (`command:delegation`)  |
| `grader`      | `jev-judge`               | output grading (`tool:jev_ask`, `hook:pre_tool_call`) |

### 1.2 What a distribution is not

A distribution is **not** a plugin. It has no code of its own, it is not loaded by
the plugin loader, and it cannot be enabled. It is a *manifest plus a registry
entry* that fans out into ordinary plugin installs.

A distribution is also **not** a plugin pack (`hermes plugins pack`). A pack is a
local YAML file resolved against the plugin catalog; a distribution is a signed,
published artifact addressed by a scoped name and pinned to exact commit SHAs.
The pack format is not covered by this document.

### 1.3 Non-goals

- Defining the plugin manifest format itself (`plugin.yaml` / `plugin.json`).
- Defining the capability-consent registry or the isolation modes.
- Defining registry hosting, CDN behaviour, or publisher key distribution policy.

---

## 2. Terminology

| Term | Meaning |
| --- | --- |
| **Distribution name** | A scoped identifier, `@scope/name`. `scope` and `name` match `[a-z0-9][a-z0-9._-]*`. |
| **Registry entry** | A file in `plugin-distributions/<name>.json` naming where a distribution's signed manifest lives and which key verifies it. |
| **Manifest** | The signed JSON document describing the components (§4). |
| **Component** | One plugin within a distribution: a name, an exact commit, a content digest, a role, and its declared dependencies. |
| **Role** | A capability slot the stack must fill (`memory`, `reader`, `dispatcher`, `grader`). |
| **Publisher key** | An Ed25519 public key that verifies the manifest signature. |
| **Canonical bytes** | The deterministic serialization the signature covers (§4.4). |
| **Content digest** | A SHA-256 over a component's file tree (§4.5). |

---

## 3. Identifier and addressing

### 3.1 Syntax

A distribution is addressed as `@scope/name`. The `@` is mandatory and is what
distinguishes a distribution from an ordinary plugin identifier.

```
@erature/vertical-stack     # valid
erature/vertical-stack      # INVALID — this is owner/repo shorthand
vertical-stack              # INVALID — this is a plugin name
```

**Requirement D1.** A conforming implementation MUST reject an identifier that
does not begin with `@` when it is offered to the distribution path, and MUST NOT
interpret a bare `scope/name` as a distribution.

**Requirement D2.** The distribution path MUST be selected *before* the ordinary
single-plugin path. The ordinary path would otherwise misparse
`@erature/vertical-stack` as `owner/repo` shorthand and clone the wrong thing.

### 3.2 Resolution

A distribution resolves through the **distribution registry**: a directory of
JSON files, one per distribution, shipped with Hermes at `plugin-distributions/`.

```json
{
  "name": "@erature/vertical-stack",
  "repo": "https://github.com/erature-hub/hermes-agent.git",
  "ref": "a8b7a09c9323f2a99a03351af68717c7c04bfab6",
  "path": "plugins/distribution/manifest.json",
  "publisher_public_key": "NKMo2gCzVw55Iqw63kmZnIYE0zECASMLARrv1C2hCJM=",
  "version": "1.0.0",
  "description": "The Erature sovereign vertical stack…",
  "maintainer": "Erature"
}
```

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | The scoped name. MUST parse as `@scope/name`. |
| `repo` | yes | Git URL of the repository holding the manifest. |
| `ref` | yes | Exact 40-character commit SHA the manifest is read from. |
| `path` | no | Manifest path within the repository. Default `distribution.json`. |
| `publisher_public_key` | yes | Base64 (or hex) Ed25519 public key. |
| `version`, `description`, `maintainer` | no | Human-facing metadata. |

**Requirement D3.** A registry entry MUST pin the manifest to an exact 40-character
commit SHA. Branch names and tags MUST be rejected — an unpinned manifest is a
moving target, and a signature over a moving target authenticates nothing.

**Requirement D4.** A malformed registry entry MUST be skipped with a warning, not
raise. One bad file MUST NOT make the whole registry unusable.

**Requirement D5.** The publisher key MUST come from the registry entry or from an
operator-supplied override. It MUST NOT be read from the manifest itself: a
manifest that carries its own key certifies nothing.

---

## 4. Manifest format

### 4.1 Shape

```json
{
  "distribution_version": 1,
  "name": "@erature/vertical-stack",
  "version": "1.0.0",
  "description": "The Erature sovereign vertical stack…",
  "publisher": "Erature",
  "requires_hermes": ">=0.21.4",
  "required_roles": ["memory", "reader", "dispatcher", "grader"],
  "signature_algorithm": "ed25519",
  "signature": "bzWP2Yefqhs9UdtRqP2S1cgKLt7mfPWmjErm5Qrofa6xIUPoXnpamtv0JrjSpTyPbNRJubECcLDYAPWUH9qLAQ==",
  "components": [ … ]
}
```

### 4.2 Fields

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `distribution_version` | integer ≥ 1 | yes (default 1) | Format version. A reader MUST refuse a version it does not implement. |
| `name` | string | yes | Scoped name. MUST match the registry entry. |
| `version` | string | no | Publisher's version of the stack. |
| `description` | string | no | One-line summary. |
| `publisher` | string | no | Publisher identity. |
| `publisher_key_id` | string | no | Publisher-chosen key identifier, for key rotation. |
| `requires_hermes` | string | no | Hermes version range this stack supports. |
| `required_roles` | list of strings | no | Roles a complete stack must fill. |
| `signature_algorithm` | string | yes (default `ed25519`) | Only `ed25519` is defined. |
| `signature` | string | yes | Base64 (or hex) Ed25519 signature over the canonical bytes. |
| `components` | list | yes | Non-empty. The component list. |

**Requirement D6.** A manifest MUST be at most 1 MiB. Larger manifests are refused
rather than parsed.

**Requirement D7.** Parsing MUST be strict and MUST fail closed. Every structural
error — a missing field, a wrong type, an oversized string, an unknown signature
algorithm — MUST raise a parse error. A partially-understood manifest would mean
installing an unverified bundle.

**Requirement D8.** Only `ed25519` is defined. A manifest naming any other
algorithm MUST be refused.

### 4.3 Components

```json
{
  "name": "hermes-memory-hindsight",
  "repo": "erature-hub/hermes-agent",
  "subdir": "plugins/memory/hermes-memory-hindsight",
  "ref": "a8b7a09c9323f2a99a03351af68717c7c04bfab6",
  "digest": "2efdbc17950e9596106c0ecc7ed9b787e43c69b35ed581973bad9331c2e0f39d",
  "role": "memory",
  "requires": [],
  "capabilities": ["memory_provider", "tool:hindsight_recall", "tool:hindsight_retain"]
}
```

| Field | Type | Required | Meaning |
| --- | --- | --- | --- |
| `name` | string | yes | Component identifier; unique within the manifest. |
| `ref` | string | yes | Exact 40-character commit SHA. Tags and branches MUST be rejected. |
| `repo` | string | no | `owner/repo`, a Git URL, or a `file://` path. Absent ⇒ the component is a bare catalog name. |
| `subdir` | string | no | Directory within the repository holding the plugin. |
| `digest` | string | no | 64-character hex SHA-256 content digest (§4.5). |
| `role` | string | no | The role this component fills. |
| `requires` | list of strings | no | Names of components that must install first. |
| `capabilities` | list of strings | no | Declared capabilities, shown in the review screen. |

**Requirement D9.** `ref` MUST be an exact commit SHA. The error message MUST name
the reason, because a publisher who wrote a tag will otherwise not know why the
bundle was refused.

**Requirement D10.** `digest` MUST be a 64-character lowercase hex string when
present. An absent digest is permitted at parse time but is refused at install
time (§6.4) — unverifiable bytes are never waved through.

**Requirement D11.** A component with no `repo` is a **bare name** resolved
through the plugin catalog at install time, exactly as a pack's bare name is. The
catalog entry's own name and repo are then what the kill list matches on (§6.6).

### 4.4 Canonical bytes and signature

The signature covers a deterministic serialization of the manifest **without** the
`signature` field (a signature cannot cover itself):

1. Normalize to the canonical payload: exactly the keys in §4.2 plus the
   component keys in §4.3, with the documented defaults applied and absent fields
   filled with their empty values.
2. Serialize as JSON with sorted keys and no insignificant whitespace:
   `json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`,
   encoded UTF-8.

**Requirement D12.** A parsed manifest and the raw dict it was parsed from MUST
normalize to byte-identical canonical payloads. A field present in one form and
absent in the other would otherwise make signing and verification disagree, and
every signature would fail.

**Requirement D13.** Verification MUST fail closed. An absent, malformed, or
mismatched signature MUST raise, never return "probably fine".

### 4.5 Content digest

The content digest is a SHA-256 over a component's file tree:

- Every regular file under the component root, sorted by POSIX-relative path.
- For each file: the relative path bytes, a `\0` separator, then the SHA-256 of
  the file's contents.
- `__pycache__`, `.git`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, and
  `node_modules` are skipped.
- A symlink is hashed by its own target text, so a link cannot smuggle a
  different tree past the digest.

The digest is filesystem-independent (no mtimes) and reproducible across
platforms.

**Requirement D14.** The digest MUST cover paths as well as contents. A digest
over contents alone would not detect a file being renamed or moved.

### 4.6 Roles

`required_roles` declares the capability slots a *complete* stack must fill. The
manifest's `missing_roles()` reports any required role with no component.

**Requirement D15.** A publisher MUST NOT declare a role the bundle does not fill.
A manifest that promises a role but ships no component for it is incomplete: the
operator would get a stack that silently lacks a capability. The gap is reported
rather than papered over.

---

## 5. Roles, dependencies, and ordering

### 5.1 Dependency graph

`requires` declares ordering constraints between components. The install order is
a topological sort of that graph.

**Requirement D16.** The install order MUST be a topological sort of the `requires`
graph, so every component follows the ones it requires.

**Requirement D17.** A cycle in `requires` MUST be refused. There is no valid
install order for a cycle, and guessing one would produce a half-wired stack.

### 5.2 Cross-component dependencies

A component's `requires` names other components **in the same manifest**.

**Requirement D17a.** A `requires` entry naming a component that is not in the
manifest MUST be refused. The bundle would otherwise claim an ordering guarantee
it cannot keep: the missing component might never be installed, and the operator
would get a stack whose declared dependency order is fiction.

**Requirement D17b.** A cycle in `requires` MUST be refused. There is no valid
install order for a cycle, and guessing one would produce a half-wired stack.

**Requirement D17c.** Ordering MUST be deterministic. Ties are broken by
declaration order, so the same manifest always produces the same sequence, and
two operators installing the same bundle get the same order.

---

## 6. Install lifecycle

The install is a **layered sequence**. Each layer is fail-closed, and every layer
that can abort MUST abort **before** any component is installed.

### 6.1 Layer 1 — Resolve

Look the scoped name up in the registry. An unpublished name MUST be refused with
a "not published" error.

### 6.2 Layer 2 — Fetch

Clone the registry entry's repository (blobless and sparse) and check out the
pinned commit **detached**. The resolved `HEAD` MUST equal the pinned `ref`;
a mismatch MUST abort.

**Requirement D18.** The manifest MUST be read from the pinned commit, never from
a branch tip.

### 6.3 Layer 3 — Verify the signature

Verify the manifest's Ed25519 signature against the registry's publisher key (or
an operator-supplied `--publisher-key`).

**Requirement D19.** An unsigned manifest MUST be refused. There is no
"unsigned but trusted" path.

**Requirement D20.** An operator MUST be able to supply a publisher key obtained
out of band, without editing the registry. This supports key rotation and
air-gapped verification.

### 6.4 Layer 4 — Verify the Hermes version floor

If the manifest declares `requires_hermes`, the running Hermes version MUST
satisfy it, or the bundle MUST be refused before anything is installed.

**Requirement D21.** The version floor MUST use the platform's existing version
gate (`requires_hermes_error`), so a distribution and an ordinary plugin manifest
are judged by the same rule. Note that this gate is *permissive* when the running
version cannot be determined (a source checkout with no release tags) — the same
platform-wide semantics the plugin loader applies.

### 6.5 Layer 5 — Verify content digests

For components hosted **in the distribution's own repository**, the fetched tree
is compared against the declared digest. Components hosted in their own
repositories are verified later, at install time (§6.8), because their trees are
not present in the distribution clone.

**Requirement D22.** A digest mismatch MUST abort the whole install with nothing
installed. A component whose bytes differ from the publisher's signed manifest is
not a component any operator asked for.

### 6.6 Layer 6 — Kill-list check

Before a component is installed, the catalog kill list MUST be consulted for it.
For a bare-name component the catalog entry's name and repo are the candidates;
for a repo-hosted component the component's own name and repo are.

**Requirement D23.** The kill list MUST apply to distribution components exactly
as it applies to a single-plugin install. A distribution MUST NOT become a path
around a control that recalls a plugin for a security or policy reason.

**Requirement D24.** `--allow-removed` MUST be the only way past the kill list,
and it MUST be explicit and per-invocation. A recalled component fails on its own
(the rest of the bundle still installs) and the command exits non-zero.

### 6.7 Layer 7 — Review screen

A mandatory screen is printed before the first byte lands, showing: the
distribution name, version, description and publisher; where it is pinned; and a
table of every component with its name, the role it fills, the repository it
comes from, its pinned ref, its requirements, its declared capabilities, and its
digest.

The screen also states the sandbox mode in force and states that capability
consent stays per-plugin.

**Requirement D25.** A distribution MUST NEVER bulk-grant capability consent.
Each component's declared capabilities still require individual consent, exactly
as for a single-plugin install and as for a pack.

### 6.8 Layer 8 — Per-component install

Each component is installed through the **ordinary pinned-install path**, in
dependency order:

1. Clone the component's repository at its pinned commit.
2. **Verify the content digest against the pristine clone**, before the security
   scan and before anything is published. The digest covers exactly what was
   fetched, because it runs before any user-file merge.
3. Run the security scan on the candidate tree. A dangerous verdict blocks.
4. Publish the tree atomically.
5. Enable the component.
6. Run the per-plugin capability consent.

**Requirement D26.** The digest check MUST run on the pristine clone, before the
scan and before publication.

**Requirement D27.** A failure in one component MUST NOT abort the others. The
fan-out reports per-component outcomes and the operator sees the complete picture
rather than the first error.

**Requirement D28.** The command MUST exit non-zero if **any** component failed. A
partial install is a failed install: returning 0 would let a CI job read "exit 0"
as "the stack is installed" while the stack is missing a component.

### 6.9 Activation and dependency consent

Components are enabled only after their dependencies are installed, so an
activation-order violation cannot leave a half-wired stack.

| Flag | Effect |
| --- | --- |
| *(default)* | Components are enabled, as a pack install does. |
| `--no-enable` | Components are installed and left disabled. |
| `--no-deps` | Dependency consent is skipped; components stay disabled. |
| `--yes-deps` | The operator's own consent to prepare Python dependencies is carried, so a headless install is not refused non-interactively. |

**Requirement D29.** The distribution path MUST honour `--enable`/`--no-enable`,
`--no-deps`, and `--yes-deps` with the same semantics as the single-plugin path.
Silently ignoring an operator's flag is a failure mode, not a convenience.

### 6.10 Sandboxed execution

Third-party plugin code never runs inside the Hermes process. The isolation mode
in force is read from `plugins.isolation` (`in_process` — the default — or
`host`), and the review screen states which mode is active.

**Requirement D30.** A distribution MUST NOT weaken the isolation mode. Bundling
more third-party plugins into one command does not change the boundary each
plugin runs behind.

**Requirement D31.** The review screen MUST state the active isolation mode and
MUST state that capability consent remains per-plugin, so the operator is not
led to believe that one consent covers the bundle.

---

## 7. Security model

### 7.1 Trust boundaries

```
  publisher key  ──signs──▶  manifest  ──pins──▶  component commits + digests
        │                            │
        │                            └──▶ registry entry (public key, commit SHA)
        ▼
  operator's Hermes  ──verifies──▶  manifest  ──installs──▶  components
```

Three independent controls protect the operator:

1. **Authenticity** — the Ed25519 signature proves the manifest came from the
   publisher who holds the key, and that nobody edited it in transit.
2. **Immutability** — every component is pinned to an exact commit SHA and to a
   content digest. The digest catches a repository that rewrites history under a
   pinned ref; the ref catches a moved branch.
3. **Authorization** — the manifest signature says *what the publisher intends*,
   not *what the operator may run*. Capability consent, the security scan, the
   kill list, and the platform's own plugin gates all still apply, per plugin.

**Requirement D32.** The publisher signature MUST NOT be treated as a substitute
for the platform's per-plugin gates. It authenticates the bundle; it does not
authorize the code.

### 7.2 Failure modes and their responses

| Condition | Response |
| --- | --- |
| Unknown distribution name | Refuse; nothing installed. |
| Registry entry malformed | Skip that entry with a warning; other entries unaffected. |
| Manifest absent at the pinned commit | Refuse; nothing installed. |
| Manifest unsigned | Refuse; nothing installed. |
| Signature mismatch | Refuse; nothing installed. |
| `requires_hermes` unsatisfied | Refuse; nothing installed. |
| Component digest mismatch (bundle-hosted) | Refuse the whole bundle; nothing installed. |
| Component digest mismatch (install-time) | Refuse that component; others proceed; exit non-zero. |
| Component has no declared digest | Refuse that component ("cannot be verified"). |
| Component on the catalog kill list | Refuse that component unless `--allow-removed`; exit non-zero. |
| Security scan blocks a component | Refuse that component; others proceed; exit non-zero. |
| Component's repository unreachable | Record the failure; others proceed; exit non-zero. |
| Capability consent declined | Component stays ungranted and degrades via `ctx.has_capability()`. |
| Dependency admission refused | Component stays installed but disabled; recorded as a failure. |

### 7.3 Threat model

| Threat | Control |
| --- | --- |
| Forged manifest | Ed25519 signature over canonical bytes; key from the registry, never from the manifest. |
| Tampered manifest in transit | Signature covers every field; any edit invalidates it. |
| Repo rewrites a pinned commit | Content digest over the whole tree, verified on the pristine clone. |
| Publisher ships a recalled plugin | Kill-list check per component; `--allow-removed` is explicit. |
| Bundle smuggles extra capabilities | Per-plugin capability consent; never bulk-granted. |
| Bundle installs on an incompatible Hermes | `requires_hermes` floor, checked before anything installs. |
| Dependency confusion (`requires` naming an absent component) | The manifest is refused: an unsatisfiable ordering guarantee is not shipped. |
| Silent partial install | Non-zero exit on any component failure; per-component report. |
| Malicious plugin code in the Hermes process | Isolation mode unchanged; third-party code never runs in-process. |

### 7.4 Key handling

The publisher's private key MUST NOT live in the repository. It is read from a
file path or from `HERMES_DISTRIBUTION_SIGNING_KEY`, and it MUST be excluded from
the working tree.

**Requirement D33.** The private key MUST never be committed, printed, or embedded
in a manifest. The manifest carries a public key reference only.

---

## 8. Publisher tooling

### 8.1 `scripts/build_vertical_stack_distribution.py`

Release-time tool for the reference distribution. It computes each component's
content digest from its **pinned tree** (the tree at the exact commit SHA, not the
working copy), assembles the manifest, and signs it.

```
python scripts/build_vertical_stack_distribution.py \
    --key-file C:/secure/erature-publisher-ed25519.key \
    --output plugins/distribution/manifest.json
```

Without `--key-file` the tool emits an unsigned manifest and says so — an
unsigned distribution is refused at install, so this is a development aid only.

### 8.2 `plugins/distribution/packager.py`

The general publisher tool. Subcommands:

| Command | Purpose |
| --- | --- |
| `build` | Assemble a manifest from local component trees. |
| `sign` | Sign an existing manifest with the publisher key. |
| `verify` | Verify a manifest file and its component digests. |
| `show` | Print a manifest's components, roles, and digests. |

**Requirement D34.** The packager MUST refuse to sign a manifest containing an
unpinned component (empty `ref`). A signature over an unpinned component
authenticates nothing reproducible.

**Requirement D35.** The packager MUST record a content digest for every component
it builds.

### 8.3 Publishing procedure

1. Update each component's pinned commit and re-verify it upstream.
2. `packager build` → manifest with digests.
3. `packager sign` → signed manifest.
4. Commit the manifest to the distribution repository.
5. Update the registry entry's `ref` to the new manifest commit and publish the
   entry under review.
6. Publish the new component commits.

**Requirement D36.** The registry entry's `ref` MUST be updated in the same
change that publishes the new manifest. A registry pointing at an older manifest
serves the older stack — which is safe, but not what the publisher intended.

### 8.4 Updating a published distribution

The operator-facing update path is `hermes plugins update <installed-plugin>`,
which operates per component. A distribution is a bundle of ordinary plugins
once installed, so it inherits the ordinary update machinery; the distribution
manifest is updated by re-publishing per §8.3.

---

## 9. The reference distribution: `@erature/vertical-stack`

### 9.1 Registry entry

Published in `plugin-distributions/erature-vertical-stack.json`, pointing at the
signed manifest in `plugins/distribution/manifest.json`.

### 9.2 Components and pins

| Role | Component | Repository | Pin |
| --- | --- | --- | --- |
| `memory` | `hermes-memory-hindsight` | `erature-hub/hermes-agent` | `a8b7a09c…` |
| `reader` | `hermes-rss` | `Adolanium/hermes-rss` | `11f8ea42…` |
| `dispatcher` | `advanced-delegation` | `NousResearch/hermes-official-plugins` | `e404b681…` |
| `grader` | `jev-judge` | `DoGMaTiiC/hermes-jev` | `5dddbeaa…` |

`hermes-rss` declares `requires: ["hermes-memory-hindsight"]`, so the memory
substrate installs first — the reader reads what the memory holds.

### 9.3 Operator experience

```
$ hermes plugins install @erature/vertical-stack

@erature/vertical-stack v1.0.0
The Erature sovereign vertical stack: episodic memory (Hindsight), a memory-aware
reading surface, specialized task dispatch, and output grading.
Publisher: Erature
Pinned at https://github.com/erature-hub/hermes-agent.git @ a8b7a09c9323

┌─────────────┬────────────┬─────────────┬──────────────┬─────────────┬──────────────┬─────────────┐
│ Component   │ Role       │ Source      │ Pinned ref   │ Requires    │ Declared     │ Digest      │
│             │            │             │              │             │ capabilities │             │
├─────────────┼────────────┼─────────────┼──────────────┼─────────────┼──────────────┼─────────────┤
│ hermes-mem… │ memory     │ erature-hu… │ a8b7a09c9323 │ —           │ memory_prov… │ sha256:2ef… │
│             │            │             │              │             │ tool:hindsi… │             │
│             │            │             │              │             │ tool:hindsi… │             │
│ hermes-rss  │ reader     │ Adolanium/… │ 11f8ea42e353 │ hermes-mem… │ hook:feed_s… │ sha256:809… │
│ advanced-d… │ dispatcher │ NousResear… │ e404b6816b20 │ —           │ command:del… │ sha256:7b1… │
│ jev-judge   │ grader     │ DoGMaTiiC/… │ 5dddbeaac5be │ —           │ tool:jev_as… │ sha256:995… │
│             │            │             │              │             │ hook:pre_to… │             │
└─────────────┴────────────┴─────────────┴──────────────┴─────────────┴──────────────┴─────────────┘

Sandbox: components run under plugins.isolation=in_process (host = host — third-party code never
runs inside the Hermes process).

Installing a distribution runs third-party code × 4 plugins. Each plugin's declared capabilities
still require individual consent after install — a distribution never bulk-grants.

✓ Signature verified for @erature/vertical-stack.
Installing hermes-memory-hindsight @ a8b7a09c9323...
…
✓ Distribution @erature/vertical-stack installed (4 component(s)).
```

The table names each component, the role it fills, the repository it comes from,
its pin, its requirements, the capabilities it declares, and its digest — so the
operator can see the whole stack before the first byte lands.

---

## 10. CLI contract

### 10.1 Commands

```
hermes plugins install @scope/name [options]
```

| Option | Effect |
| --- | --- |
| `--publisher-key KEY` | Verify against an out-of-band publisher key (base64 or hex). |
| `--allow-removed` | Bypass the catalog kill list for the bundle's components. |
| `--enable` / `--no-enable` | Enable the components, or install them disabled. |
| `--no-deps` | Skip dependency consent; components stay disabled. |
| `--yes-deps` | Carry the operator's consent to prepare Python dependencies. |
| `--force` | Reinstall components that already exist. |

### 10.2 Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Every component installed successfully. |
| 1 | The distribution was refused, or at least one component failed. |

**Requirement D37.** Any component failure MUST produce exit code 1.

---

## 11. Conformance checklist

An implementation conforms to this specification if it satisfies all of the
following.

**Format**
- [ ] D1, D2 — scoped-name syntax and routing.
- [ ] D3, D4, D5 — registry semantics.
- [ ] D6, D7, D8 — strict, bounded, fail-closed parsing.
- [ ] D9, D10, D11 — component field validation.
- [ ] D12, D13 — canonical bytes and signature verification.
- [ ] D14, D15 — content digest and role completeness.

**Lifecycle**
- [ ] D16, D17, D17a, D17b, D17c — ordering, cycles, unknown references, determinism.
- [ ] D18–D24 — fetch, signature, version floor, digests, kill list.
- [ ] D25–D31 — review screen, per-component install, activation, isolation.
- [ ] D32 — signature is not a substitute for per-plugin gates.
- [ ] D33–D36 — key handling and publishing procedure.
- [ ] D37 — exit codes.

**Reference implementation:** 60 tests across
`tests/hermes_cli/test_plugin_distributions.py` (format, trust, ordering, CLI
routing, review screen) and
`tests/hermes_cli/test_plugin_distributions_install_e2e.py` (end-to-end install
against real local Git repositories).

---

## 12. Versioning

`distribution_version` is the format version and is independent of the
publisher's `version`. A change that breaks an existing manifest's meaning
increments `distribution_version`; a reader MUST refuse a version it does not
implement rather than guess.

The manifest format is additive within version 1: new optional fields MAY be
added, and a reader MUST ignore fields it does not recognize **inside the
signature-covered payload only if** doing so cannot change the meaning of a
field it does recognize. Because the canonical payload is a fixed key set, an
unrecognized field in version 1 is a format error.

---

## 13. References

| Artifact | Path |
| --- | --- |
| Format + trust model | `hermes_cli/plugin_distributions.py` |
| Install lifecycle | `hermes_cli/plugin_distributions_install.py` |
| Publisher tooling | `plugins/distribution/packager.py` |
| Reference build script | `scripts/build_vertical_stack_distribution.py` |
| Published manifest | `plugins/distribution/manifest.json` |
| Registry entry | `plugin-distributions/erature-vertical-stack.json` |
| Tests | `tests/hermes_cli/test_plugin_distributions.py`, `tests/hermes_cli/test_plugin_distributions_install_e2e.py` |
