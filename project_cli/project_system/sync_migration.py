"""Deterministic migration of provably terminal 0.6 GitHub transport packs."""
from pathlib import Path

from .sync_bindings import (
    SyncBindingError,
    build_terminal_binding,
    load_sync_bindings,
    terminalize_binding,
)
from .sync_finalization import (
    SyncFinalizeError,
    _git_preflight,
    _load_previous_finalization,
    _load_verification,
    _prove_commit,
    _resolve_integrity_inputs,
    _run_git,
)


class SyncMigrationError(RuntimeError):
    exit_code = 3


def _classify_active(root, binding, apply):
    pack = binding.pack
    transport = (pack.get('provenance') or {}).get('transport')
    if not isinstance(transport, dict) or transport.get('kind') != 'github_issue':
        return {'pack_id': pack['pack_id'], 'status': 'direct_intake_skipped'}
    output = root / '.generated' / 'sync' / pack['pack_id']
    if not (output / 'plan.json').is_file() or not (output / 'manifest.json').is_file():
        return {'pack_id': pack['pack_id'], 'status': 'active_unverified'}
    if not (output / 'verification.json').is_file():
        return {'pack_id': pack['pack_id'], 'status': 'planned_unverified'}
    try:
        integrity = _resolve_integrity_inputs(root, binding.path, require_base_head=False)
        verification = _load_verification(integrity['output'], integrity['plan'])
        finalization = _load_previous_finalization(integrity['output'])
    except (SyncFinalizeError, OSError, ValueError, TypeError) as exc:
        return {'pack_id': pack['pack_id'], 'status': 'conflict', 'detail': str(exc)}
    if finalization is None:
        return {'pack_id': pack['pack_id'], 'status': 'verified_not_finalized'}
    if finalization.get('state') not in {'pushed', 'completed'} or finalization.get('push_result') not in {
        'pushed', 'already_synchronized'
    }:
        if finalization.get('commit_sha'):
            return {'pack_id': pack['pack_id'], 'status': 'awaiting_push'}
        return {'pack_id': pack['pack_id'], 'status': 'prepared_not_terminal'}
    try:
        _prove_commit(root, finalization, verification)
        preflight = _git_preflight(root)
        upstream = _run_git(root, ['rev-parse', '@{upstream}'], allow_failure=True)
        if upstream.returncode or upstream.stdout.strip().lower() != finalization['commit_sha']:
            raise SyncMigrationError('local upstream does not prove the recorded pushed commit')
        if not apply:
            return {'pack_id': pack['pack_id'], 'status': 'ready_to_archive'}
        raw = binding.raw
        terminal = build_terminal_binding(
            pack,
            raw,
            outcome='pushed',
            reason=None,
            verification_fingerprint=verification['verification_fingerprint'],
            commit_sha=finalization['commit_sha'],
            verified_paths=verification['actual_changed_canonical_paths'],
            push_proof={
                'remote': preflight['remote'],
                'upstream': preflight['upstream'],
                'commit_sha': finalization['commit_sha'],
            },
        )
        terminalize_binding(root, binding.path, pack, raw, terminal)
        return {'pack_id': pack['pack_id'], 'status': 'archived'}
    except (SyncBindingError, SyncFinalizeError, SyncMigrationError, OSError, ValueError) as exc:
        return {'pack_id': pack['pack_id'], 'status': 'conflict', 'detail': str(exc)}


def migrate_bindings(root, *, apply=False):
    """Audit, or explicitly archive, only deterministic legacy terminal bindings."""
    root = Path(root).resolve()
    try:
        bindings = load_sync_bindings(root)
    except SyncBindingError as exc:
        raise SyncMigrationError(str(exc)) from exc
    results = []
    for binding in bindings:
        if binding.state == 'completed':
            if binding.recoverable_active_path and apply:
                try:
                    terminalize_binding(
                        root,
                        binding.recoverable_active_path,
                        binding.pack,
                        binding.raw,
                        binding.terminal,
                    )
                    results.append({'pack_id': binding.pack['pack_id'], 'status': 'recovered_cleanup'})
                except SyncBindingError as exc:
                    results.append({'pack_id': binding.pack['pack_id'], 'status': 'conflict', 'detail': str(exc)})
            else:
                results.append({'pack_id': binding.pack['pack_id'], 'status': 'already_completed'})
            continue
        results.append(_classify_active(root, binding, apply))
    return results
