"""``session.compact``: the owner compacts its own store, and only the owner may.

The compaction rewrites rows and may VACUUM the file, so it must run as the OWNER of the
store. A CLI writing the store under a live gateway is the second writer the held-store
refusal exists to stop, so the CLI asks the owner instead -- exactly as ``session.prune``
does.
"""
import asyncio
from types import SimpleNamespace

import pytest

from gateway.session_compact import _apply, _params, compact
from hermes_state_runtime import RuntimeStoreError


def _connection(*, native_owner=True, capabilities=("session:operator",), profile_id="p"):
    authority = SimpleNamespace(
        db=None, epoch=1, profile_id=profile_id,
        _require_admission_open=lambda: None)
    actor = SimpleNamespace(profile_id=profile_id, capabilities=set(capabilities), subject="s")
    return SimpleNamespace(authority=authority, actor=actor, native_owner=native_owner)


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #


def test_params_defaults_and_coercion():
    out = _params({})
    assert out["idle_days"] == 30.0
    assert out["drop_duplicate_reasoning"] is True
    assert out["vacuum"] is True
    assert out["include_archived"] is True


def test_params_rejects_unknown_keys():
    with pytest.raises(RuntimeStoreError):
        _params({"idle_days": 30, "nope": 1})


def test_params_rejects_a_negative_idle_days():
    with pytest.raises(RuntimeStoreError):
        _params({"idle_days": -1})


def test_params_rejects_a_non_scalar():
    with pytest.raises(RuntimeStoreError):
        _params({"idle_days": [30]})


def test_params_rejects_a_boolean_idle_days():
    """A bool is an int in Python; a True would silently become a 1-day window."""
    with pytest.raises(RuntimeStoreError):
        _params({"idle_days": True})


# --------------------------------------------------------------------------- #
# Authority
# --------------------------------------------------------------------------- #


def test_a_non_owner_cannot_compact():
    with pytest.raises(RuntimeStoreError):
        asyncio.run(compact(_connection(native_owner=False), "ref", {}))


def test_an_actor_without_the_operator_capability_cannot_compact():
    with pytest.raises(RuntimeStoreError):
        asyncio.run(compact(_connection(capabilities=()), "ref", {}))


def test_a_cross_profile_actor_is_refused():
    """The actor's profile must match the authority's: a compact is store-wide."""
    conn = _connection(profile_id="p")
    conn.actor.profile_id = "other"
    with pytest.raises(RuntimeStoreError):
        asyncio.run(compact(conn, "ref", {}))


# --------------------------------------------------------------------------- #
# The apply path
# --------------------------------------------------------------------------- #


def test_apply_calls_compact_store_on_the_owner_db(tmp_path, monkeypatch):
    """The owner compacts ITS OWN store, with the options the caller sent."""
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("s", "cli")
        seen = {}

        def _record(**kwargs):
            seen.update(kwargs)
            return {"pruned": 0, "receipts_compacted": 0, "reasoning_deduped": 0,
                    "vacuumed": False, "freed_bytes": 0}

        monkeypatch.setattr(db, "compact_store", _record)
        authority = SimpleNamespace(db=db, epoch=1, profile_id="p",
                                    _require_admission_open=lambda: None)
        result = asyncio.run(_apply(authority, {
            "idle_days": 7.0, "drop_duplicate_reasoning": False,
            "vacuum": False, "include_archived": True}))

        assert result["pruned"] == 0
        assert seen["idle_days"] == 7.0
        assert seen["drop_duplicate_reasoning"] is False
        assert seen["vacuum"] is False
    finally:
        db.close()
