"""POSIX bundle inventory and bounded archive validation, preserving internal links."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat
import tarfile

MAX_BYTES = 2_000_000_000
MAX_MEMBERS = 12000
TOP = 'VibeJobRadar'


def require(value, message):
    if not value:
        raise ValueError(message)


def digest(stream):
    value = hashlib.sha256()
    for block in iter(lambda: stream.read(1024 * 1024), b''):
        value.update(block)
    return value.hexdigest()


def path_name(name):
    require(isinstance(name, str) and name and '\\' not in name
            and not any(ord(c) < 32 for c in name), 'Invalid bundle path')
    path = PurePosixPath(name)
    require(not path.is_absolute() and str(path) == name
            and path.parts[0] == TOP and '..' not in path.parts,
            'Bundle path escapes its root')
    return path


def validate_links(rows):
    require(TOP in rows and rows[TOP]['kind'] == 'directory', 'Bundle root missing')
    for name, row in rows.items():
        parts = path_name(name).parts
        for i in range(1, len(parts)):
            ancestor = rows.get('/'.join(parts[:i]))
            require(ancestor and ancestor['kind'] == 'directory', 'Non-directory bundle ancestor')
        if row['kind'] != 'symlink':
            continue
        target = row['target']
        require(isinstance(target, str) and target and not target.startswith('/')
                and '\\' not in target and not any(ord(c) < 32 for c in target),
                'Invalid bundle link')
        current, seen = name, set()
        for _ in range(100):
            current_parts = path_name(current).parts
            linked = False
            for i in range(1, len(current_parts) + 1):
                prefix = '/'.join(current_parts[:i])
                candidate = rows.get(prefix)
                if candidate and candidate['kind'] == 'symlink':
                    require(prefix not in seen, 'Cyclic bundle link')
                    seen.add(prefix)
                    value = candidate['target']
                    require(value and not value.startswith('/') and '\\' not in value
                            and not any(ord(c) < 32 for c in value), 'Invalid bundle link')
                    current = posixpath.normpath(posixpath.join(posixpath.dirname(prefix), value,
                                                               *current_parts[i:]))
                    linked = True
                    break
            if not linked:
                require(current in rows, 'Dangling bundle link')
                break
        else:
            raise ValueError('Bundle link depth exceeded')


def inventory(bundle):
    bundle = Path(bundle)
    require(bundle.name == TOP and bundle.is_dir() and not bundle.is_symlink(), 'Invalid bundle directory')
    rows, total = {}, 0
    for path in [bundle, *sorted(bundle.rglob('*'))]:
        name = TOP if path == bundle else TOP + '/' + path.relative_to(bundle).as_posix()
        mode = path.lstat().st_mode
        require(not stat.S_IMODE(mode) & ~0o777, 'Special bundle permission')
        if path.is_symlink():
            row = dict(kind='symlink', target=os.readlink(path), mode=stat.S_IMODE(mode))
        elif path.is_dir():
            row = dict(kind='directory', mode=stat.S_IMODE(mode))
        else:
            require(stat.S_ISREG(mode), 'Special bundle file')
            size = path.stat().st_size
            total += size
            require(total <= MAX_BYTES, 'Bundle too large')
            with path.open('rb') as stream:
                row = dict(kind='file', size=size, sha256=digest(stream), mode=stat.S_IMODE(mode))
        rows[name] = row
        require(len(rows) <= MAX_MEMBERS, 'Too many bundle entries')
    require(len({name.casefold() for name in rows}) == len(rows), 'Case-colliding bundle names')
    validate_links(rows)
    return rows


def archive_inventory(path):
    rows, total = {}, 0
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            name = member.name
            path_name(name)
            require(name not in rows and len(rows) < MAX_MEMBERS, 'Duplicate or excessive archive members')
            require(not member.mode & ~0o777, 'Special archive permission')
            if member.isdir():
                row = dict(kind='directory', mode=member.mode)
            elif member.issym():
                row = dict(kind='symlink', target=member.linkname, mode=member.mode)
            else:
                require(member.isfile(), 'Special archive member')
                total += member.size
                require(total <= MAX_BYTES and member.size >= 0, 'Archive exceeds size limit')
                with archive.extractfile(member) as stream:
                    row = dict(kind='file', size=member.size, sha256=digest(stream), mode=member.mode)
            rows[name] = row
    require(len({name.casefold() for name in rows}) == len(rows), 'Case-colliding archive names')
    validate_links(rows)
    return rows


def pack(bundle, output):
    expected = inventory(bundle)
    output = Path(output)
    require(not output.exists() and not output.is_symlink(), 'Archive output already exists')
    with tarfile.open(output, 'x:gz', dereference=False, format=tarfile.PAX_FORMAT) as archive:
        for name in expected:
            source = Path(bundle).parent / name
            row = archive.gettarinfo(str(source), arcname=name)
            row.uid = row.gid = row.mtime = 0
            row.uname = row.gname = ''
            if expected[name]['kind'] == 'file':
                # Source hardlinks are stored as ordinary bytes, never archive
                # hardlinks whose extraction order could alter the payload.
                row.type = tarfile.REGTYPE
                row.linkname = ''
                row.size = expected[name]['size']
                with source.open('rb') as stream:
                    archive.addfile(row, stream)
            else:
                archive.addfile(row)
    require(archive_inventory(output) == expected, 'Archive differs from built bundle')
    return expected


def extract_verified(archive_path, destination, expected):
    destination = Path(destination)
    require(not destination.exists() and not destination.is_symlink(), 'Extraction output already exists')
    require(archive_inventory(archive_path) == expected, 'Archive inventory changed')
    destination.mkdir(parents=True)
    # No member may have a symlink ancestor. Create ordinary entries first,
    # then the validated internal links; extraction never follows a link.
    with tarfile.open(archive_path, 'r:gz') as archive:
        for member in archive:
            path = destination / member.name
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
                path.chmod(member.mode)
            elif member.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                with archive.extractfile(member) as source, path.open('xb') as target:
                    for block in iter(lambda: source.read(1024 * 1024), b''):
                        target.write(block)
                path.chmod(member.mode)
        for name, row in expected.items():
            if row['kind'] == 'symlink':
                os.symlink(row['target'], destination / name)
    require(inventory(destination / TOP) == expected, 'Extracted bundle differs from archive')
    return destination / TOP
