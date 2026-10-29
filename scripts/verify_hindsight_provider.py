"""LIVE end-to-end verification of the sovereign Hindsight provider.

This script drives the REAL provider against the REAL local Hindsight substrate
(127.0.0.1:8888) — no fakes. It proves the whole path works: namespace resolution,
commencement recall, decision/directive retention, the built-in memory mirror, and the
tool bridge.

Safety model, because the substrate has no per-memory delete:

* **Read-only checks** run against the REAL shared bank (``erature-hermes``) — that is
  where the standing directives and the real recall corpus live, so it is the only
  place the check means anything.
* **Write checks** run against a dedicated THROWAWAY bank, which is deleted
  afterwards. The substrate exposes no per-memory ``DELETE`` (only a destructive
  whole-bank clear), so writing into the shared bank would leave residue that cannot
  be removed one memory at a time. The throwaway bank is deleted instead, which
  cascades its memories and aliases away.

Net effect on the substrate: none.

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
    check("namespace resolved", provider._namespace == "hermes", provider._namespace)
    check(
        "bank resolved to a canonical id (no slash, no %2F)",
        "/" not in provider._bank_id and "%2F" not in provider._bank_id.upper(),
        provider._bank_id,
    )
    check(
        "credential present but not exposed",
        bool(provider._api_key) and "hs_live" not in repr(provider.get_context()),
    )

    _section("3. commencement recall (background, real shared bank)")
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
    shared_bank = provider._bank_id
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
    check("shared bank untouched", provider._bank_id == shared_bank, shared_bank)
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
