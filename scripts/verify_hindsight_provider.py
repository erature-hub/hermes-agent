"""LIVE end-to-end verification of the sovereign Hindsight provider.

This script drives the REAL provider against the REAL local Hindsight substrate
(127.0.0.1:8888) — no fakes. It proves the whole path works: namespace resolution,
commencement recall, decision/directive retention, the built-in memory mirror, and the
tool bridge.

Safety model, because the substrate has no per-memory delete:

* **Read-only checks** run against the REAL shared bank (``erature-hermes``) — that is
  where the standing directives and the real recall corpus live, so it is the only
  place the check means anything. The provider is REBOUND onto that bank for the
  read-only section and put back on the profile's own bank afterwards: a profile
  configured for an isolated ``erature/<owner>`` namespace has its own (initially
  empty) bank, and reading that instead would report a healthy substrate as broken.
* **Write checks** run against a dedicated THROWAWAY bank, which is deleted
  afterwards. The substrate exposes no per-memory ``DELETE`` (only a destructive
  whole-bank clear), so writing into the shared bank would leave residue that cannot
  be removed one memory at a time. The throwaway bank is deleted instead, which
  cascades its memories and aliases away.

Net effect on the substrate: none. The shared bank's fact count is compared before
and after the run, so a leaked write is caught rather than assumed away.

Usage:
    ./.venv/Scripts/python.exe scripts/verify_hindsight_provider.py
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_PKG = "plugins.memory.hermes-memory-hindsight"
_provider = importlib.import_module(_PKG)
_client_mod = importlib.import_module(f"{_PKG}._client")

HindsightMemoryProvider = _provider.HindsightMemoryProvider
HindsightError = _client_mod.HindsightError

BASE_URL = os.environ.get("HINDSIGHT_API_URL") or "http://127.0.0.1:8888"
THROWAWAY_BANK = "sovereign-memory-verify"


def _api_key() -> str:
    """The key from the operator's .env — never printed."""
    for path in (
        pathlib.Path.home() / "src" / "hindsight" / ".env",
        pathlib.Path.home() / "AppData" / "Local" / "hermes" / ".env",
    ):
        if not path.exists():
            continue
        raw = path.read_bytes()
        text = ""
        for enc in ("utf-8-sig", "utf-16", "utf-8"):
            try:
                text = raw.decode(enc)
                break
            except Exception:
                continue
        for line in text.splitlines():
            if line.strip().startswith("HINDSIGHT_API_KEY="):
                value = line.split("=", 1)[1].strip().strip('"').strip("'")
                if value:
                    return value
    raise SystemExit("no HINDSIGHT_API_KEY found in the operator .env files")


def _section(title: str) -> None:
    print(f"\n=== {title}")


def _configured_namespace() -> str | None:
    """The namespace the profile's own config asks for, if it asks for one.

    Read straight from the provider's config file so the check is independent of
    the provider's own parsing. ``None`` means the profile is on the default.
    """
    path = (
        pathlib.Path.home()
        / "AppData"
        / "Local"
        / "hermes"
        / "profiles"
        / "sentinel"
        / "hermes-memory-hindsight"
        / "config.json"
    )
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    value = data.get("namespace") if isinstance(data, dict) else None
    return str(value).strip() or None if value else None


def main() -> int:
    key = _api_key()
    os.environ["HINDSIGHT_API_KEY"] = key
    os.environ["HINDSIGHT_API_URL"] = BASE_URL

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ""))
        if not ok:
            failures.append(label)

    client = _client_mod.HindsightClient(BASE_URL, key, timeout=60)

    _section("1. substrate reachability + auth")
    health = client.health()
    check("health reports healthy", health.get("status") == "healthy", json.dumps(health)[:110])

    _section("2. provider initializes against the live substrate")
    provider = HindsightMemoryProvider()
    provider.initialize(
        "live-verify-session",
        hermes_home=str(
            pathlib.Path.home() / "AppData" / "Local" / "hermes" / "profiles" / "sentinel"
        ),
    )
    check("provider bound to a client", provider._client is not None)
    # The CONFIGURED namespace, not a hardcoded one: a profile may legitimately run
    # the shared ``hermes`` namespace or an isolated ``erature/<owner>`` one, and
    # the provider must honor whichever the profile asked for. The check is that
    # the config -> namespace -> canonical-bank chain holds, not that a particular
    # namespace happens to be configured.
    configured = _configured_namespace()
    check(
        "the configured namespace is what the provider resolved",
        configured is None or provider._namespace == configured,
        f"configured={configured} resolved={provider._namespace}",
    )
    check(
        "bank resolved to a canonical id (no slash, no %2F)",
        "/" not in provider._bank_id and "%2F" not in provider._bank_id.upper(),
        provider._bank_id,
    )
    check(
        "the resolved bank exists on the substrate",
        bool(client.get_bank(provider._bank_id).get("bank_id")),
        provider._bank_id,
    )
    check(
        "credential present but not exposed",
        bool(provider._api_key) and "hs_live" not in repr(provider.get_context()),
    )
    # The bank this profile is bound to, captured BEFORE the read-only checks
    # rebind the provider onto the shared bank.
    profile_bank = provider._bank_id

    _section("2b. per-owner namespace isolation (real substrate)")
    # An owner namespace must get its OWN bank, not the shared one: otherwise one
    # owner's memories are recalled under another owner's namespace.
    owner_bank = f"erature-liveverify{int(time.time()) % 100000}"
    owner_alias = f"erature/liveverify{int(time.time()) % 100000}"
    try:
        client._request("PUT", f"/v1/default/banks/{owner_bank}", body={"name": "verify owner"})
        client._request("POST", f"/v1/default/banks/{owner_bank}/aliases", body={"alias": owner_alias})
        resolver = provider._resolver
        resolved = resolver.ensure_namespace(owner_alias)
        check(
            "an owner namespace resolves to a bank of its own",
            resolved == owner_bank and resolved != provider._bank_id,
            resolved,
        )
        check(
            "the owner namespace is addressable as an alias",
            any(owner_alias in (b.get("matched_aliases") or []) for b in client.list_banks(query=owner_alias)),
            owner_alias,
        )
    finally:
        client._request("DELETE", f"/v1/default/banks/{owner_bank}")

    _section("3. commencement recall (background, real shared bank)")
    # The read-only checks below MUST run against the shared bank: that is where the
    # standing directives and the real recall corpus live, so it is the only place the
    # check means anything. A profile configured for an isolated owner namespace has
    # its own (initially empty) bank, and reading THAT would report a working
    # substrate as broken. So the provider is rebound onto the shared bank for the
    # read-only section and restored afterwards.
    shared_bank = provider._resolver.canonical_bank("hermes")
    shared_facts_before = int(client.get_bank(shared_bank).get("fact_count") or 0)
    check(
        "the shared bank resolves to a canonical id",
        bool(shared_bank) and "/" not in shared_bank,
        shared_bank,
    )
    check(
        "the shared bank holds the standing corpus",
        shared_facts_before > 0,
        f"{shared_facts_before} facts",
    )
    provider._namespace = "hermes"
    provider._owner = ""
    provider._bank_id = shared_bank
    provider._request_recall()

    deadline = time.time() + 40
    while time.time() < deadline and not provider._prefetch_text:
        time.sleep(0.2)
    staged = provider._prefetch_text
    check("commencement recall staged context", bool(staged), f"{len(staged)} chars")
    check(
        "staged context carries the substrate's own standing directives",
        "Standing directives" in staged,
        staged[:120].replace("\n", " | "),
    )
    consumed = provider.prefetch("live verification")
    check("prefetch() returns the staged context", bool(consumed), f"{len(consumed)} chars")

    _section("4. tool bridge (live, read-only)")
    recall = json.loads(
        provider.handle_tool_call("hindsight_recall", {"query": "preferences", "budget": "low"})
    )
    check(
        "hindsight_recall returns a result envelope",
        "results" in recall,
        f"{len(recall.get('results', []))} results",
    )
    check(
        "recall names the canonical bank",
        recall.get("bank") == provider._bank_id,
        str(recall.get("bank")),
    )
    reflect = json.loads(
        provider.handle_tool_call(
            "hindsight_reflect", {"query": "What do you know about the user?", "budget": "low"}
        )
    )
    check(
        "hindsight_reflect answers",
        bool(reflect.get("answer")),
        str(reflect.get("answer"))[:90].replace("\n", " "),
    )
    directives = json.loads(provider.handle_tool_call("hindsight_directives", {}))
    check(
        "hindsight_directives lists standing directives",
        len(directives.get("directives", [])) >= 1,
        f"{len(directives.get('directives', []))} directives",
    )

    _section("5. write checks (dedicated throwaway bank, deleted afterwards)")
    marker = f"live-verify-probe-{int(time.time())}"
    try:
        client._request("PUT", f"/v1/default/banks/{THROWAWAY_BANK}", body={"name": "verify probe"})
        provider._bank_id = THROWAWAY_BANK

        provider.sync_turn(
            "Which marker should the verifier use?",
            f"We will always use the {marker} marker when verifying sovereign memory.",
        )
        listed = client.list_directives(THROWAWAY_BANK, active_only=False, limit=500)
        hits = [
            d
            for d in listed
            if marker in str(d.get("content") or "") or marker in str(d.get("name") or "")
        ]
        check(
            "the decision was promoted to a standing directive",
            bool(hits),
            f"{len(hits)} match(es)",
        )

        provider.on_memory_write("add", "memory", f"The verification marker is {marker}.")
        # The mirror is an ASYNC retain: the substrate accepts it and extracts in
        # the background, so the fact is not recall-visible until the operation
        # settles. Wait for it — that is exactly what the provider's own
        # commencement recall does before it recalls.
        provider._await_pending_ops(10.0)
        mirrored = client.recall(THROWAWAY_BANK, marker, budget="low")
        check(
            "a built-in memory write reached the substrate",
            any(marker in str(f.get("text") or "") for f in mirrored),
            f"{len(mirrored)} recall hits",
        )
    finally:
        provider._bank_id = shared_bank

    _section("6. cleanup")
    client._request("DELETE", f"/v1/default/banks/{THROWAWAY_BANK}")
    still_there = client.list_banks(query=THROWAWAY_BANK)
    check(
        "throwaway bank deleted (cascades its memories and aliases)",
        not still_there,
        "gone" if not still_there else "STILL PRESENT",
    )
    # The shared bank must be byte-for-byte unchanged: the same fact count it had
    # before the run. A write that leaked into it would show up here.
    shared_facts_after = int(client.get_bank(shared_bank).get("fact_count") or 0)
    check(
        "shared bank untouched (no facts written into it)",
        shared_facts_after == shared_facts_before,
        f"{shared_facts_before} -> {shared_facts_after} facts",
    )
    # The provider is put back on the bank the PROFILE is bound to, not on the
    # shared bank the read-only checks used.
    provider._bank_id = profile_bank
    check(
        "the profile's own bank was left intact",
        bool(client.get_bank(profile_bank).get("bank_id")),
        profile_bank,
    )
    provider.shutdown()

    _section("VERDICT")
    if failures:
        print(f"  {len(failures)} check(s) FAILED: {failures}")
        return 1
    print("  ALL CHECKS PASSED — the sovereign provider works against the live substrate.")
    print("  Net effect on the substrate: none (writes went to a bank that was deleted).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
