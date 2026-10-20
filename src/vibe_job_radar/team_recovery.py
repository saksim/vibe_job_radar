"""Offline verification and whole-service restoration of quiescent backups.

The manifest detects corruption and mixed snapshots; it is not a signature.
The operator must retire the source before taking the snapshot and keep it
retired. A historical backup cannot account for later grants or consumption.
"""
from __future__ import annotations

import hashlib
import math
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import closing
from pathlib import Path

from .guided.rate import RateLedger
from .team_store import TeamError, LEGACY_TABLES, TABLES, private_json, strict_json

MANIFEST = 'team-backup.json'
DATABASES = {'control.sqlite', 'rates.sqlite', 'catalog.sqlite'}
DIRECTORIES = {'refresh-workspace', 'refresh-workspace/local_public',
               'refresh-workspace/public_examples'}
REFRESH_RATES = 'refresh-workspace/public_examples/rates.sqlite'
MAX_MANIFEST_BYTES = 2_000_000


def _checked(path, *, directory=False):
    path = Path(path).absolute()
    # Resolve OS-level ancestors (for example macOS /var) before traversing
    # the snapshot. Payload entries and their immediate parent may not link
    # elsewhere; _inventory checks each descendant directory before entering.
    for entry in (path.parent, path):
        info = entry.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, 'st_file_attributes', 0) & 0x400):
            raise TeamError('team_backup_unsafe')
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise TeamError('team_backup_unsafe')
    return path.resolve(strict=True)


def _inventory(root):
    root = _checked(root, directory=True)
    files = {}
    pending = [root]
    while pending:
        directory = pending.pop()
        for path in directory.iterdir():
            name = path.relative_to(root).as_posix()
            if name in DIRECTORIES:
                _checked(path, directory=True)
                pending.append(path)
                continue
            _checked(path)
            allowed = (name in DATABASES or name in {MANIFEST, REFRESH_RATES}
                       or path.parent == root / 'refresh-workspace' / 'local_public'
                       and not path.name.startswith('.'))
            if not allowed:
                raise TeamError('team_backup_unexpected_file')
            if name == MANIFEST:
                continue
            digest = hashlib.sha256()
            size = 0
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    size += len(block)
                    digest.update(block)
            files[name] = {'bytes': size, 'sha256': digest.hexdigest()}
            if len(files) > 10000:
                raise TeamError('team_backup_too_many_files')
    if not DATABASES <= files.keys():
        raise TeamError('team_backup_incomplete')
    return files


def _validate_definition(conn, name, version):
    from .team_store import SCHEMA_SQL as CONTROL_SCHEMA
    from .team_catalog import SCHEMA_SQL as CATALOG_SCHEMA
    # Include every table and index: matching column names alone does not
    # establish the primary/unique/check constraints relied upon by requests.
    def definition(db):
        return {(kind, name, table, re.sub(r'\s+', '', sql).casefold() if sql else None)
                for kind, name, table, sql in db.execute(
                    'SELECT type,name,tbl_name,sql FROM sqlite_master')}
    with closing(sqlite3.connect(':memory:')) as expected:
        expected.executescript(CONTROL_SCHEMA if name == 'control.sqlite' else CATALOG_SCHEMA)
        if name == 'control.sqlite' and version == 1:
            expected.execute('DROP TABLE task_leases')
        if definition(conn) != definition(expected):
            raise TeamError('team_backup_database_invalid')


def _create_restore_marker(target):
    # Unlike a normal JSON publication, a failed flush must retain this file:
    # its mere presence prevents use of a destination with uncertain durability.
    marker = target / '.restore-in-progress'
    with marker.open('xb') as stream:
        marker.chmod(0o600)
        stream.write(b'{"schema_version": 1}\n')
        stream.flush()
        os.fsync(stream.fileno())
    from .team_service import sync_directory
    sync_directory(target)


def _validate_database(path, name):
    # Completed backups have no live WAL/journal. Immutable mode also prevents
    # verification from creating sidecars or changing a read-only backup.
    with closing(sqlite3.connect(path.as_uri() + '?mode=ro&immutable=1', uri=True)) as conn:
        conn.execute('PRAGMA trusted_schema=OFF')
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise TeamError('team_backup_database_invalid')
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger')").fetchone():
            raise TeamError('team_backup_database_invalid')
        if name.endswith('rates.sqlite'):
            if not RateLedger._schema_ready(conn):
                raise TeamError('team_backup_database_invalid')
            return
        version = conn.execute('PRAGMA user_version').fetchone()[0]
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if name == 'control.sqlite':
            if (version, tables) not in ((1, LEGACY_TABLES), (2, TABLES)):
                raise TeamError('team_backup_database_invalid')
            _validate_definition(conn, name, version)
            # Check the columns consumed by service startup and authentication.
            conn.execute('SELECT tenant,member,role,active FROM members LIMIT 0')
            conn.execute('SELECT id,tenant,member,digest,expires,revoked FROM devices LIMIT 0')
            conn.execute('SELECT tenant,id,site,origins,limits FROM resources LIMIT 0')
            conn.execute('SELECT id,ts FROM clock LIMIT 0')
        else:
            if version != 1 or tables != {'meta', 'jobs', 'changes', 'sources'}:
                raise TeamError('team_backup_database_invalid')
            _validate_definition(conn, name, version)
            meta = dict(conn.execute('SELECT key,value FROM meta'))
            if (set(meta) != {'revision', 'floor', 'clock', 'secret'}
                    or not re.fullmatch('[0-9a-fA-F]{64}', meta['secret'])
                    or not 0 <= int(meta['floor']) <= int(meta['revision'])
                    or not math.isfinite(float(meta['clock']))):
                raise TeamError('team_backup_database_invalid')
            conn.execute('SELECT source,id,body,digest FROM jobs LIMIT 0')
            conn.execute('SELECT id,definition,observed,revision FROM sources LIMIT 0')


def write_manifest(root, *, clock=time.time):
    root = Path(root).absolute()
    created = clock()
    if type(created) not in (float, int) or not math.isfinite(created) or created < 0:
        raise TeamError('team_backup_time_invalid')
    files = _inventory(root)
    for name in DATABASES | ({REFRESH_RATES} & files.keys()):
        _validate_database(root / name, name)
    private_json(root / MANIFEST, {'schema_version': 1, 'created_at': created, 'files': files})


def verify_backup(root):
    root = _checked(root, directory=True)
    path = root / MANIFEST
    if not path.exists():
        raise TeamError('team_backup_manifest_required')
    _checked(path)
    with path.open('rb') as stream:
        raw = stream.read(MAX_MANIFEST_BYTES + 1)
    if len(raw) > MAX_MANIFEST_BYTES:
        raise TeamError('team_backup_manifest_invalid')
    try:
        value = strict_json(raw)
    except RecursionError:
        raise TeamError('team_backup_manifest_invalid') from None
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'created_at', 'files'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['created_at']) not in (float, int)
            or not math.isfinite(value['created_at']) or value['created_at'] < 0
            or not isinstance(value['files'], dict)):
        raise TeamError('team_backup_manifest_invalid')
    files = _inventory(root)
    for record in value['files'].values():
        if (not isinstance(record, dict) or set(record) != {'bytes', 'sha256'}
                or type(record['bytes']) is not int or record['bytes'] < 0
                or not isinstance(record['sha256'], str)
                or not re.fullmatch('[0-9a-f]{64}', record['sha256'])):
            raise TeamError('team_backup_manifest_invalid')
    if files != value['files']:
        raise TeamError('team_backup_digest_mismatch')
    try:
        for name in DATABASES | ({REFRESH_RATES} & files.keys()):
            _validate_database(root / name, name)
    except (sqlite3.Error, ValueError, TypeError, KeyError) as error:
        if isinstance(error, TeamError):
            raise
        raise TeamError('team_backup_database_invalid') from error
    return {'ok': True, 'created_at': value['created_at'], 'file_count': len(files),
            'bytes': sum(r['bytes'] for r in files.values()), 'files': files,
            'manifest_sha256': hashlib.sha256(raw).hexdigest()}


def restore_backup(source, target, *, confirm_quiescent=False):
    from .team_service import sync_directory
    if confirm_quiescent is not True:
        raise TeamError('team_restore_quiescent_confirmation_required')
    source = _checked(source, directory=True)
    target = Path(target).absolute()
    target = _checked(target.parent, directory=True) / target.name
    if target.exists() or target.is_symlink():
        raise TeamError('team_output_exists', 409)
    # Never publish within the backup, even through a relative path.
    if source == target or source in target.parents:
        raise TeamError('team_backup_unsafe')
    verified = verify_backup(source)
    if any(n.startswith('refresh-workspace/local_public/') for n in verified['files']) and REFRESH_RATES not in verified['files']:
        raise TeamError('team_backup_refresh_ledger_missing')
    # Stage beneath an exclusively created destination. All service entry
    # points refuse the recovery marker until the complete copy is published.
    target.mkdir(mode=0o700)
    _create_restore_marker(target)
    try:
        # Persist the new directory entry in its parent while recovery remains
        # blocked. Syncing target alone cannot make its own name durable.
        sync_directory(target.parent)
        with tempfile.TemporaryDirectory(prefix='.restore-', dir=target) as directory:
            staged = Path(directory)
            for name in [*verified['files'], MANIFEST]:
                origin = _checked(source / name)
                destination = staged / name
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with origin.open('rb') as incoming, destination.open('xb') as outgoing:
                    shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                destination.chmod(0o600)
            copied = verify_backup(staged)
            if copied['manifest_sha256'] != verified['manifest_sha256']:
                raise TeamError('team_backup_digest_mismatch')
            # Persist nested directory entries before publishing the root and
            # removing its recovery marker (file fsync alone is insufficient).
            for directory in sorted((p for p in staged.rglob('*') if p.is_dir()),
                                    key=lambda p: len(p.parts), reverse=True):
                sync_directory(directory)
            sync_directory(staged)
            # Publication is serialized by the normal service ownership lock.
            from .collection import writer_lock
            with writer_lock(target):
                for path in list(staged.iterdir()):
                    if path.name == MANIFEST:
                        continue
                    if (target / path.name).exists():
                        raise TeamError('team_output_exists', 409)
                    path.rename(target / path.name)
                sync_directory(target)
                (target / '.restore-in-progress').unlink()
                try:
                    sync_directory(target)
                except BaseException:
                    # Re-block while still holding the publication lock.
                    _create_restore_marker(target)
                    raise
        return {k: v for k, v in verified.items() if k != 'files'}
    except BaseException:
        # Retain the marked destination for inspection; never delete or replace
        # operator data. The unchanged source can be restored to another path.
        if not (target / '.restore-in-progress').exists():
            _create_restore_marker(target)
        raise
