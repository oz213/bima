#!/usr/bin/env python3
"""BIMA Phase 1B-1D: consistent backup, isolated restore drill, NFS risk audit.

Never modifies production DB, runtime symlinks, service settings or alert delivery.
Only managed backups, isolated restore directories and local attestation are written.
Python >= 3.9, stdlib only. Never imports the crawler or opens a BIMA connection.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid

NFS = Path('/mnt/registrar-sandbox')
DB = NFS / 'bima/state/crawler_production.sqlite3'
LEARNING = NFS / 'bima/state/bima_learning_v450.sqlite3'
DATASET = NFS / 'bima/datasets/urls.json'
BACKUPS = NFS / 'bima/backups/verified-sqlite'
RECORD = Path('/var/lib/pandi-bima-health/last-rehearsal.json')
LOCK_ROOT = Path('/var/lib/pandi-bima-health')
MODEL = Path('/home/fauzi/bima-shadow/models/candidate')
VERSION = 'bima-verified-backup-v9'
BACKUP_NAME = re.compile(r'^verified-\d{8}T\d{12}Z-[0-9a-f]{8}$')
GIB = 1024**3
BACKUP_FILES = ('crawler_production.sqlite3', 'bima_learning_v450.sqlite3', 'urls.json')


class SafetyError(RuntimeError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(now: datetime | None = None) -> str:
    return (now or utcnow()).isoformat(timespec='seconds')


def json_print(value: dict) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for data in iter(lambda: stream.read(2 * 1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def ensure_within(path: Path, root: Path) -> Path:
    real_root = root.resolve(strict=True)
    real_path = path.resolve(strict=False)
    if real_path == real_root or not real_path.is_relative_to(real_root):
        raise SafetyError('path_outside_expected_root')
    return real_path


def mounted_fstype(path: Path) -> str:
    try:
        result = subprocess.run(
            ['findmnt', '-rn', '-T', str(path), '-o', 'FSTYPE'],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SafetyError('mount_check_unavailable') from exc
    if result.returncode:
        raise SafetyError('mount_check_failed')
    return (result.stdout.strip().splitlines() or ['unknown'])[0].lower()


def require_nfs(root: Path = NFS) -> None:
    if mounted_fstype(root) not in ('nfs', 'nfs4'):
        raise SafetyError('nfs_mount_missing_no_fallback')


@contextmanager
def local_lock(name: str):
    """Block overlapping jobs using an exclusive *local* advisory lock."""
    if not re.fullmatch(r'[a-z-]{3,40}', name):
        raise SafetyError('invalid_lock_name')
    if not LOCK_ROOT.is_dir() or LOCK_ROOT.is_symlink():
        raise SafetyError('local_state_directory_missing')
    path = LOCK_ROOT / (name + '.lock')
    flags = os.O_CREAT | os.O_RDWR
    if hasattr(os, 'O_NOFOLLOW'):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, 0o600)
    except OSError as exc:
        raise SafetyError('local_lock_unavailable') from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SafetyError('operation_already_running') from exc
        yield
    finally:
        os.close(fd)


def connection_ro(path: Path):
    if not path.is_file():
        raise SafetyError('sqlite_missing')
    conn = sqlite3.connect(path.resolve(strict=True).as_uri() + '?mode=ro',
                           uri=True, timeout=15)
    conn.execute('PRAGMA query_only=ON')
    conn.execute('PRAGMA busy_timeout=15000')
    return conn


def sqlite_check(path: Path, *, full=False, require_jobs=False) -> dict:
    with closing(connection_ro(path)) as conn:
        mode = str(conn.execute('PRAGMA journal_mode').fetchone()[0]).lower()
        check = 'integrity_check' if full else 'quick_check'
        answer = conn.execute('PRAGMA ' + check).fetchone()
        if answer is None or answer[0] != 'ok':
            raise SafetyError('sqlite_' + check + '_failed')
        snapshot = None
        if require_jobs:
            has_jobs = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'"
            ).fetchone()
            if not has_jobs:
                raise SafetyError('jobs_table_missing')
            has_active = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='registry_snapshot_active_v2'"
            ).fetchone()
            if not has_active:
                raise SafetyError('snapshot_table_missing')
            row = conn.execute(
                'SELECT a.cycle_id,a.ready,r.url_count,r.source_sha256 '
                'FROM registry_snapshot_active_v2 a '
                'JOIN registry_snapshot_runs_v2 r ON r.cycle_id=a.cycle_id '
                'WHERE a.singleton=1'
            ).fetchone()
            if not row or int(row[1]) != 1:
                raise SafetyError('snapshot_not_ready')
            queue_count = int(conn.execute(
                'SELECT COUNT(*) FROM registry_snapshot_queue_v2'
            ).fetchone()[0])
            if queue_count != int(row[2]):
                raise SafetyError('snapshot_queue_total_mismatch')
            snapshot = {'cycle': str(row[0]), 'total': int(row[2]),
                        'source_sha256': str(row[3])}
        return {'journal_mode': mode, 'snapshot': snapshot, 'integrity': 'ok'}


def _write_json_atomic(path: Path, data: dict) -> None:
    if path.is_symlink() or path.parent.is_symlink():
        raise SafetyError('unsafe_output_symlink')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8') as stream:
            os.chmod(temp, 0o600)
            json.dump(data, stream, sort_keys=True, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _copy_file(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise SafetyError('backup_target_exists')
    with source.open('rb') as src, target.open('xb') as dst:
        os.chmod(target, 0o600)
        shutil.copyfileobj(src, dst, 2 * 1024 * 1024)
        dst.flush()
        os.fsync(dst.fileno())


def _sqlite_backup(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise SafetyError('backup_target_exists')
    with closing(connection_ro(source)) as src:
        mode = str(src.execute('PRAGMA journal_mode').fetchone()[0]).lower()
        if mode == 'wal':
            raise SafetyError('sqlite_wal_on_nfs_not_supported')
        with closing(sqlite3.connect(str(target), timeout=30)) as dst:
            src.backup(dst, pages=512, sleep=0.2)
    os.chmod(target, 0o600)
    sqlite_check(target)


def _file_manifest(path: Path) -> dict:
    return {'sha256': sha256(path), 'bytes': path.stat().st_size}


def _open_backup(root: Path, backup: Path) -> tuple[Path, dict]:
    # Reject unknown/unmanaged directories and symlinks, including the parent.
    if root.is_symlink() or backup.is_symlink():
        raise SafetyError('unsafe_backup_symlink')
    if not BACKUP_NAME.fullmatch(backup.name):
        raise SafetyError('unrecognized_backup_directory')
    path = ensure_within(backup, root)
    if path.parent != root.resolve(strict=True) or not path.is_dir():
        raise SafetyError('backup_not_direct_child')
    manifest_path = path / 'manifest.json'
    if manifest_path.is_symlink():
        raise SafetyError('manifest_symlink')
    try:
        data = json.loads(manifest_path.read_text('utf-8'))
    except (ValueError, OSError) as exc:
        raise SafetyError('backup_manifest_missing_or_invalid') from exc
    if (not isinstance(data, dict) or data.get('format') != VERSION
            or data.get('id') != path.name or not isinstance(data.get('files'), dict)):
        raise SafetyError('backup_manifest_not_managed')
    if not data['files'] or not set(data['files']).issubset(BACKUP_FILES):
        raise SafetyError('backup_manifest_files_invalid')
    if 'crawler_production.sqlite3' not in data['files'] or 'urls.json' not in data['files']:
        raise SafetyError('backup_required_files_missing')
    return path, data


def newest_backup(root: Path) -> Path:
    if not root.is_dir() or root.is_symlink():
        raise SafetyError('backup_root_not_found')
    candidates = sorted(
        (p for p in root.iterdir()
         if BACKUP_NAME.fullmatch(p.name) and p.is_dir() and not p.is_symlink()),
        key=lambda p: p.name, reverse=True,
    )
    for candidate in candidates:
        try:
            _open_backup(root, candidate)
            return candidate
        except SafetyError:
            continue
    raise SafetyError('no_finalized_verified_backup')


def verify_backup(backup: Path, root: Path, *, full=False) -> dict:
    path, data = _open_backup(root, backup)
    for name, detail in data['files'].items():
        file = path / name
        if file.is_symlink() or not file.is_file():
            raise SafetyError('backup_file_missing_or_symlink')
        if (type(detail.get('bytes')) is not int or detail['bytes'] < 1
                or file.stat().st_size != detail['bytes'] or sha256(file) != detail.get('sha256')):
            raise SafetyError('backup_file_hash_or_size_mismatch')
    metadata = sqlite_check(path / 'crawler_production.sqlite3',
                            full=full, require_jobs=True)
    if metadata['snapshot'] != data.get('snapshot'):
        raise SafetyError('snapshot_metadata_mismatch')
    if (data['files']['urls.json']['sha256'] != metadata['snapshot']['source_sha256']):
        raise SafetyError('dataset_cycle_sha_mismatch')
    if 'bima_learning_v450.sqlite3' in data['files']:
        sqlite_check(path / 'bima_learning_v450.sqlite3', full=full)
    return {'ok': True, 'id': path.name, 'created_at': data.get('created_at'),
            'cycle': metadata['snapshot']['cycle'],
            'total': metadata['snapshot']['total'],
            'files': sorted(data['files']), 'integrity': 'full' if full else 'quick'}


def backup_once(*, db: Path = DB, learning: Path = LEARNING,
                dataset: Path = DATASET, root: Path = BACKUPS,
                nfs: Path = NFS, enforce_nfs=True, retain=0) -> dict:
    if retain and retain < 2:
        raise SafetyError('retention_must_be_at_least_two')
    if enforce_nfs:
        require_nfs(nfs)
        for file in (db, dataset, learning, root):
            ensure_within(file, nfs)
    if root.is_symlink():
        raise SafetyError('backup_root_is_symlink')
    if not db.is_file() or not dataset.is_file():
        raise SafetyError('required_source_missing')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Operates only on our own new .partial-* folder, never prior archives.
    sources = [('crawler_production.sqlite3', db), ('urls.json', dataset)]
    if learning.is_file():
        sources.append(('bima_learning_v450.sqlite3', learning))
    required_bytes = sum(p.stat().st_size for _, p in sources)
    if shutil.disk_usage(root).free < required_bytes * 2 + 256 * 1024**2:
        raise SafetyError('backup_storage_insufficient')
    identifier = 'verified-' + utcnow().strftime('%Y%m%dT%H%M%S%fZ') + '-' + uuid.uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix='.partial-', dir=str(root)) as partial:
        folder = Path(partial)
        details = {}
        for name, src in sources:
            dst = folder / name
            if name.endswith('.sqlite3'):
                _sqlite_backup(src, dst)
            else:
                _copy_file(src, dst)
            details[name] = _file_manifest(dst)
        # Snapshot must be published and correspond to the copied dataset.
        metadata = sqlite_check(folder / 'crawler_production.sqlite3', require_jobs=True)
        snapshot = metadata['snapshot']
        if details['urls.json']['sha256'] != snapshot['source_sha256']:
            raise SafetyError('source_changed_during_backup_or_snapshot_mismatch')
        manifest = {'format': VERSION, 'id': identifier, 'created_at': iso(),
                    'files': details, 'snapshot': snapshot}
        _write_json_atomic(folder / 'manifest.json', manifest)
        final = root / identifier
        if final.exists() or final.is_symlink():
            raise SafetyError('backup_id_collision')
        os.rename(folder, final)
        # tempfile context may later try to unlink its now-renamed temporary dir;
        # it is empty/nonexistent, so the original context exit is safe.
    outcome = verify_backup(final, root)
    if retain:
        outcome['pruned'] = prune_backups(root, keep=retain, current=final.name)
    outcome['backup_path'] = str(final)
    return outcome


def prune_backups(root: Path, *, keep: int, current: str) -> int:
    """Delete only recognized v9 verified backup folders, never rollover/manual."""
    if keep < 2:
        raise SafetyError('retention_must_be_at_least_two')
    recognized = []
    for child in root.iterdir():
        if child.is_symlink() or not child.is_dir() or not BACKUP_NAME.fullmatch(child.name):
            continue
        try:
            path, data = _open_backup(root, child)
        except SafetyError:
            continue
        if any(x.name not in set(data['files']) | {'manifest.json'} for x in path.iterdir()):
            continue  # Never delete backups that an operator extended manually.
        recognized.append(path)
    ordered = sorted(recognized, key=lambda x: x.name, reverse=True)
    protected = {child.name for child in ordered[:keep]} | {current}
    removed = 0
    for child in ordered:
        if child.name not in protected:
            shutil.rmtree(child)
            removed += 1
    return removed


def local_destination(path: Path, nfs: Path = NFS) -> Path:
    if path.is_symlink() or path.exists():
        raise SafetyError('restore_destination_must_not_exist')
    parent = path.parent.resolve(strict=True)
    real = parent / path.name
    if real.is_relative_to(nfs.resolve(strict=False)) or real.is_relative_to(Path('/home/fauzi/crawler-rebuild')):
        raise SafetyError('restore_destination_production_path_forbidden')
    if mounted_fstype(parent) in ('nfs', 'nfs4'):
        raise SafetyError('restore_destination_must_be_local')
    return real


def restore_to(*, backup: Path, root: Path, destination: Path,
               full_check=True, nfs: Path = NFS) -> dict:
    # Independent test destination; NEVER overwrite the production DB or symlinks.
    verify_backup(backup, root)
    dest = local_destination(destination, nfs)
    path, manifest = _open_backup(root, backup)
    bytes_needed = sum(record['bytes'] for record in manifest['files'].values())
    if shutil.disk_usage(dest.parent).free < bytes_needed * 2 + 128 * 1024**2:
        raise SafetyError('restore_local_space_insufficient')
    dest.mkdir(mode=0o700)
    try:
        for name in manifest['files']:
            _copy_file(path / name, dest / name)
        # Verify the *local restored copies* independently of the NFS originals.
        restored = dest / 'crawler_production.sqlite3'
        metadata = sqlite_check(restored, full=full_check, require_jobs=True)
        if metadata['snapshot'] != manifest['snapshot']:
            raise SafetyError('restored_snapshot_inconsistent')
        for name, entry in manifest['files'].items():
            if sha256(dest / name) != entry['sha256']:
                raise SafetyError('restored_file_hash_mismatch')
        if (sha256(dest / 'urls.json') != metadata['snapshot']['source_sha256']):
            raise SafetyError('restored_dataset_mismatch')
        if 'bima_learning_v450.sqlite3' in manifest['files']:
            sqlite_check(dest / 'bima_learning_v450.sqlite3', full=full_check)
        return {'ok': True, 'id': path.name, 'cycle': metadata['snapshot']['cycle'],
                'total': metadata['snapshot']['total'],
                'destination': str(dest), 'integrity': 'full' if full_check else 'quick'}
    except BaseException:
        # Do not delete files on a failed drill: diagnostics may be needed;
        # never write or roll back production data here.
        raise


def rehearse(*, backup: Path, root: Path, work_root: Path = Path('/var/tmp'),
             record: Path | None = RECORD, nfs: Path = NFS) -> dict:
    verify_backup(backup, root)
    if not work_root.is_dir() or mounted_fstype(work_root) in ('nfs', 'nfs4'):
        raise SafetyError('rehearsal_work_root_must_be_local')
    with tempfile.TemporaryDirectory(prefix='bima-restore-drill-', dir=str(work_root)) as parent:
        result = restore_to(backup=backup, root=root,
                            destination=Path(parent) / 'isolated', nfs=nfs)
        result.pop('destination', None)  # Avoid leaking temporary copy paths.
    result['rehearsed_at'] = iso()
    if record:
        if record.resolve(strict=False).is_relative_to(nfs.resolve(strict=False)):
            raise SafetyError('rehearsal_attestation_must_be_local')
        _write_json_atomic(record, result)
        result['attestation'] = str(record)
    return result


def storage_audit(*, db: Path = DB, nfs: Path = NFS,
                  model: Path = MODEL, local_root: Path = Path('/')) -> dict:
    issues, warnings = [], []
    result = {'status': 'unknown', 'checked_at': iso(), 'issues': issues, 'warnings': warnings}
    try:
        fstype = mounted_fstype(nfs)
        result['nfs_fstype'] = fstype
        if fstype not in ('nfs', 'nfs4'):
            issues.append('nfs_mount_missing_no_fallback')
            result['status'] = 'blocked'
            return result  # Never touch active DB on a missing mount.
        ensure_within(db, nfs)
        size = db.stat().st_size
        result['db_bytes'] = size
        result['nfs_free_gib'] = round(shutil.disk_usage(nfs).free / GIB, 2)
        result['local_free_gib'] = round(shutil.disk_usage(local_root).free / GIB, 2)
        result['local_migration_minimum_gib'] = round((size * 3 + 3 * GIB) / GIB, 2)
        if shutil.disk_usage(local_root).free < size * 3 + 3 * GIB:
            issues.append('local_disk_insufficient_for_safe_migration')
        with closing(connection_ro(db)) as conn:
            mode = str(conn.execute('PRAGMA journal_mode').fetchone()[0]).lower()
        result['sqlite_journal_mode'] = mode
        if mode != 'delete':
            issues.append('sqlite_nfs_requires_delete_journal_mode')
        warnings.append('sqlite_on_nfs_locking_risk')
        result['indobert_model_config_visible'] = (model / 'config.json').is_file()
        if not result['indobert_model_config_visible']:
            issues.append('indobert_model_config_missing')
        result['cutover'] = 'not_performed_preserve_running_cycle'
        result['recommendation'] = 'migrate_active_sqlite_to_local_ssd_after_tested_service_path_and_rollback'
    except (OSError, ValueError, sqlite3.Error, SafetyError) as exc:
        issues.append('storage_audit_' + type(exc).__name__)
    result['status'] = 'blocked' if issues else 'at_risk'
    return result


def parse_date(value: str) -> datetime:
    value = str(value).replace('Z', '+00:00')
    date = datetime.fromisoformat(value)
    if date.tzinfo is None:
        raise ValueError('date_without_timezone')
    return date.astimezone(timezone.utc)


def readiness(*, backup_root: Path = BACKUPS, record: Path = RECORD,
              health_json: Path | None = None, db: Path = DB,
              nfs: Path = NFS, model: Path = MODEL,
              max_backup_hours=36, max_rehearsal_days=8,
              now: datetime | None = None) -> dict:
    now = now or utcnow()
    failures, warnings = [], []
    report = {'checked_at': iso(now), 'failures': failures, 'warnings': warnings}
    storage = storage_audit(db=db, nfs=nfs, model=model)
    report['storage'] = {'status': storage['status'], 'issues': storage['issues']}
    if storage['issues']:
        failures.extend(storage['issues'])
    warnings.extend(storage['warnings'])
    health = None
    if health_json:
        try:
            health = json.loads(health_json.read_text('utf-8'))
        except (ValueError, OSError):
            failures.append('health_report_unreadable')
    else:
        script = Path(__file__).with_name('bima_production_health_v8.py')
        if not script.is_file():
            failures.append('phase1a_health_monitor_not_deployed')
        else:
            try:
                p = subprocess.run([sys.executable, '-B', str(script), '--json'],
                                   capture_output=True, text=True, timeout=75, check=False)
                health = json.loads(p.stdout)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                failures.append('phase1a_health_check_failed')
    if health is not None:
        report['health_status'] = health.get('status')
        if health.get('status') != 'healthy':
            failures.append('phase1a_health_degraded')
    try:
        if storage['status'] != 'blocked':
            latest = newest_backup(backup_root)
            # Read SHA256 + SQLite quick_check on the latest backup. Readiness
            # must not trust only a manifest filename or stale metadata.
            verify_backup(latest, backup_root, full=False)
            _path, manifest = _open_backup(backup_root, latest)
            backup_age = (now - parse_date(manifest['created_at'])).total_seconds() / 3600
            report['latest_backup_id'] = latest.name
            report['latest_backup_age_hours'] = round(backup_age, 2)
            if backup_age < -0.1 or backup_age > max_backup_hours:
                failures.append('verified_backup_too_old')
        else:
            failures.append('backup_unavailable_while_nfs_is_down')
    except (OSError, ValueError, SafetyError):
        failures.append('verified_backup_missing')
    try:
        att = json.loads(record.read_text('utf-8'))
        age = (now - parse_date(att['rehearsed_at'])).total_seconds() / 86400
        report['restore_drill_age_days'] = round(age, 2)
        if (att.get('ok') is not True or att.get('integrity') != 'full'
                or age < -0.01 or age > max_rehearsal_days):
            failures.append('restore_drill_missing_or_stale')
    except (OSError, ValueError, KeyError, TypeError):
        failures.append('restore_drill_missing_or_stale')
    report['status'] = 'not_ready' if failures else ('ready_with_nfs_risk' if warnings else 'ready')
    return report


def cli(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    backup_p = sub.add_parser('backup', help='Online consistent SQLite backup to independent NFS archive')
    backup_p.add_argument('--db', type=Path, default=DB)
    backup_p.add_argument('--learning-db', type=Path, default=LEARNING)
    backup_p.add_argument('--dataset', type=Path, default=DATASET)
    backup_p.add_argument('--backup-root', type=Path, default=BACKUPS)
    backup_p.add_argument('--retain', type=int, default=0,
                          help='Optional retention count, at least 2; default never prunes')
    verify_p = sub.add_parser('verify', help='Check finalized backup SHA and SQLite copy')
    verify_p.add_argument('--backup', type=Path, required=True)
    verify_p.add_argument('--backup-root', type=Path, default=BACKUPS)
    verify_p.add_argument('--full', action='store_true')
    drill_p = sub.add_parser('rehearse', help='Restore latest/specified NFS backup to isolated local temp')
    drill_p.add_argument('--backup', type=Path)
    drill_p.add_argument('--backup-root', type=Path, default=BACKUPS)
    drill_p.add_argument('--work-root', type=Path, default=Path('/var/tmp'))
    drill_p.add_argument('--record', type=Path, default=RECORD)
    restore_p = sub.add_parser('restore-copy', help='Copy to NEW local directory; never to production')
    restore_p.add_argument('--backup', type=Path, required=True)
    restore_p.add_argument('--backup-root', type=Path, default=BACKUPS)
    restore_p.add_argument('--destination', type=Path, required=True)
    audit_p = sub.add_parser('storage-audit', help='Read-only SQLite/NFS risk and local space check')
    audit_p.add_argument('--db', type=Path, default=DB)
    audit_p.add_argument('--model', type=Path, default=MODEL)
    ready_p = sub.add_parser('readiness', help='Combine Phase 1A, verified backup, restore drill and NFS risk')
    ready_p.add_argument('--health-json', type=Path)
    ready_p.add_argument('--backup-root', type=Path, default=BACKUPS)
    ready_p.add_argument('--record', type=Path, default=RECORD)
    ready_p.add_argument('--max-backup-hours', type=float, default=36)
    ready_p.add_argument('--max-rehearsal-days', type=float, default=8)
    args = parser.parse_args(argv)
    try:
        if args.action == 'backup':
            with local_lock('verified-backup'):
                output = backup_once(db=args.db, learning=args.learning_db,
                                     dataset=args.dataset, root=args.backup_root,
                                     retain=args.retain)
        elif args.action == 'verify':
            require_nfs()
            ensure_within(args.backup_root, NFS)
            ensure_within(args.backup, NFS)
            output = verify_backup(args.backup, args.backup_root, full=args.full)
        elif args.action == 'rehearse':
            require_nfs()
            ensure_within(args.backup_root, NFS)
            with local_lock('restore-rehearsal'):
                output = rehearse(backup=args.backup or newest_backup(args.backup_root),
                                  root=args.backup_root, work_root=args.work_root,
                                  record=args.record)
        elif args.action == 'restore-copy':
            require_nfs()
            ensure_within(args.backup_root, NFS)
            ensure_within(args.backup, NFS)
            output = restore_to(backup=args.backup, root=args.backup_root,
                                destination=args.destination)
        elif args.action == 'storage-audit':
            output = storage_audit(db=args.db, model=args.model)
        else:
            output = readiness(backup_root=args.backup_root, record=args.record,
                               health_json=args.health_json,
                               max_backup_hours=args.max_backup_hours,
                               max_rehearsal_days=args.max_rehearsal_days)
        json_print(output)
        if output.get('status') in ('blocked', 'not_ready'):
            return 1
        return 0
    except (SafetyError, OSError, sqlite3.Error, ValueError, KeyError) as exc:
        json_print({'ok': False, 'error': type(exc).__name__,
                    'reason': str(exc) if isinstance(exc, SafetyError) else 'operation_failed',
                    'action': args.action})
        return 2


if __name__ == '__main__':
    sys.exit(cli())
