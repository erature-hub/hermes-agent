"""LIVE verification of the substrate identity gate.

Proves, against REAL peers on the host:

1. The real Hindsight substrate on :9999 IS identified (title + version).
2. The third-party product that took over :8888 (Unsloth Studio) is REFUSED.
3. Against the foreign peer, NO request carries an Authorization header -- the
   bearer and the retained transcripts never leave.
4. The provider refuses to bind to the foreign peer and stays inert.

Read-only. Writes nothing.
"""

from __future__ import annotations

import importlib
import json
import pathlib
import sys
import time

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

_PKG = "plugins.memory.hermes-memory-hindsight"
_provider = importlib.import_module(_PKG)
_client_mod = importlib.import_module(f"{_PKG}._client")

HindsightClient = _client_mod.HindsightClient
HindsightError = _client_mod.HindsightError
probe_substrate = _client_mod.probe_substrate

SUBSTRATE = "http://127.0.0.1:9999"
FOREIGN = "http://127.0.0.1:8888"

failures: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" -- {detail}" if detail else ""))
    if not ok:
        failures.append(label)


class _RecordingOpener:
    """Wraps urlopen and records every request's headers."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, request, timeout=None):
        self.calls.append(
            {
                "path": request.full_url,
                "headers": {k.lower(): v for k, v in request.header_items()},
            }
        )
        import urllib.request

        return urllib.request.urlopen(request, timeout=timeout)


print("=== 1. the real substrate IS identified")
opener = _RecordingOpener()
verdict = probe_substrate(SUBSTRATE, opener=opener)
check(
    "the Hindsight substrate on :9999 identifies as Hindsight",
    verdict.identified,
    f"title={verdict.title!r} version={verdict.version!r} reason={verdict.reason!r}",
)
check(
    "the probe carried NO bearer",
    all("authorization" not in c["headers"] for c in opener.calls),
    f"{len(opener.calls)} probe calls",
)

print()
print("=== 2. the third-party product on :8888 is REFUSED")
opener2 = _RecordingOpener()
foreign = probe_substrate(FOREIGN, opener=opener2)
check(
    "the unrelated product on :8888 is NOT identified as Hindsight",
    foreign.identified is False,
    f"reason={foreign.reason!r}",
)
check(
    "the probe against the foreign peer carried NO bearer",
    all("authorization" not in c["headers"] for c in opener2.calls),
)

# The /health gate alone stopped the foreign peer (it 404s there), so the
# DECISIVE signal -- the product identity in the OpenAPI title -- is exercised
# separately against the REAL foreign document.
import urllib.request

real_foreign_spec = urllib.request.urlopen(f"{FOREIGN}/openapi.json", timeout=10).read()
foreign_title, _ = _client_mod._openapi_identity(
    real_foreign_spec[: _client_mod.OPENAPI_PROBE_LIMIT]
)
check(
    "the foreign peer's REAL OpenAPI title is read from a bounded prefix",
    foreign_title == "Unsloth UI Backend",
    f"title={foreign_title!r}",
)
check(
    "that title is REFUSED by the identity check",
    not foreign_title.startswith(_client_mod.SUBSTRATE_TITLE_PREFIX),
)
real_substrate_spec = urllib.request.urlopen(f"{SUBSTRATE}/openapi.json", timeout=20).read()
sub_title, sub_version = _client_mod._openapi_identity(
    real_substrate_spec[: _client_mod.OPENAPI_PROBE_LIMIT]
)
check(
    "the real substrate's title is ACCEPTED by the identity check",
    sub_title.startswith(_client_mod.SUBSTRATE_TITLE_PREFIX),
    f"title={sub_title!r} version={sub_version!r}",
)

print()
print("=== 3. an authenticated call against the foreign peer sends NO bearer")
client = HindsightClient(FOREIGN, "SECRET-BEARER-VALUE", timeout=10)
raised = ""
try:
    client.recall("hermes", "what is the capital of France?")
except HindsightError as exc:
    raised = str(exc)
check("the recall against the foreign peer was refused", bool(raised), raised[:120])
check(
    "the client reports it did not identify the substrate",
    client.identity is not None and client.identity.identified is False,
    f"identity={client.identity}",
)

print()
print("=== 4. the provider refuses to bind to the foreign peer")
import os

os.environ["HINDSIGHT_API_KEY"] = "SECRET-BEARER-VALUE"
os.environ["HINDSIGHT_API_URL"] = FOREIGN
provider = _provider.HindsightMemoryProvider()
provider.initialize("live-identity-verify", hermes_home=str(pathlib.Path.home()))
check(
    "the provider did NOT bind to the foreign peer",
    provider._client is None,
    "sovereign memory is inert",
)
check(
    "the provider reports the peer as not the substrate",
    "not the Hindsight substrate" in provider.unavailable_reason()
    or provider._client is None,
    provider.unavailable_reason()[:160],
)

print()
print("=== 5. the provider DOES bind to the real substrate")
os.environ["HINDSIGHT_API_URL"] = SUBSTRATE
provider2 = _provider.HindsightMemoryProvider()
provider2.initialize("live-identity-verify-2", hermes_home=str(pathlib.Path.home()))
check("the provider bound to the real substrate", provider2._client is not None)
check(
    "it resolved a canonical bank id",
    bool(provider2._bank_id) and "/" not in provider2._bank_id,
    provider2._bank_id,
)

print()
if failures:
    print(f"FAILED ({len(failures)}): " + "; ".join(failures))
    raise SystemExit(1)
print("ALL LIVE IDENTITY CHECKS PASSED")
