"""``session.compact``: the owner compacts its own state.db.

The compaction rewrites rows (the reasoning_content echo, a finished worker's read receipts)
and then VACUUMs the file, so it must run as the OWNER of the store. A CLI writing the store
under a live gateway would be a second writer (#110054), so the CLI asks the owner instead --
exactly as ``session.prune`` does.

The VACUUM inside the owner needs an exclusive lock, and this process holds the store open
itself, so the pass reports ``vacuumed: False`` unless the store is provably quiet. That is the
honest outcome: the rows are reclaimed either way, and the file shrinks on the next quiet pass.
"""
import asyncio

from hermes_state_runtime import RuntimeStoreError

_SCALARS = (type(None), bool, int, float, str)


def _params(raw):
    if not isinstance(raw, dict):
        raise RuntimeStoreError('invalid_params')
    allowed = {'idle_days', 'drop_duplicate_reasoning', 'vacuum', 'include_archived'}
    if set(raw) - allowed:
        raise RuntimeStoreError('invalid_params')
    if not all(isinstance(value, _SCALARS) for value in raw.values()):
        raise RuntimeStoreError('invalid_params')
    idle_days = raw.get('idle_days', 30.0)
    if isinstance(idle_days, bool) or not isinstance(idle_days, (int, float)) or idle_days < 0:
        raise RuntimeStoreError('invalid_params')
    return {
        'idle_days': float(idle_days),
        'drop_duplicate_reasoning': bool(raw.get('drop_duplicate_reasoning', True)),
        'vacuum': bool(raw.get('vacuum', True)),
        'include_archived': bool(raw.get('include_archived', True)),
    }


async def compact(connection, ref, params):
    authority, actor = connection.authority, connection.actor
    # Store-wide rewriting across every principal's rows: the local operator only (a ticket from
    # this host's private control socket), never a remote or browser grant.
    if not connection.native_owner or 'session:operator' not in actor.capabilities:
        raise RuntimeStoreError('permission_denied')
    if actor.profile_id != authority.profile_id:
        raise RuntimeStoreError('profile_mismatch')
    options = _params(params)
    authority._require_admission_open()
    from gateway.session_runtime_workers import track_mutation
    task = track_mutation(authority, _apply(authority, options))
    return await asyncio.shield(task)


async def _apply(authority, options):
    from hermes_constants import get_hermes_home
    # Same transcript dir the CLI's local prune unlinks under (dispatch runs in the owner's scope).
    sessions_dir = get_hermes_home() / 'sessions'
    return await asyncio.to_thread(
        authority.db.compact_store,
        idle_days=options['idle_days'],
        include_archived=options['include_archived'],
        drop_duplicate_reasoning=options['drop_duplicate_reasoning'],
        vacuum=options['vacuum'],
        sessions_dir=sessions_dir,
    )
