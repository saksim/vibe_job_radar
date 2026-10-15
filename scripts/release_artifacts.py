"""Select one successful attempt's immutable artifacts without deleting older evidence."""
from __future__ import annotations
from datetime import datetime
import hashlib
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import zipfile

MAX_ARCHIVE_BYTES = 600_000_000
MAX_EXTRACTED_BYTES = 800_000_000
MAX_ENTRIES = 2000
REPOSITORY = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
DIGEST = re.compile(r'sha256:[0-9a-f]{64}')


def require(value, message):
    if not value:
        raise ValueError(message)


def timestamp(value):
    require(isinstance(value, str) and re.fullmatch(
        r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z', value),
        'Missing or invalid artifact attempt time')
    return datetime.fromisoformat(value[:-1] + '+00:00')


def select_artifacts(selected, listing, names):
    """The current successful attempt must have exactly one artifact per name."""
    require(type(selected.get('id')) is int and selected['id'] > 0
            and type(selected.get('run_attempt')) is int and selected['run_attempt'] > 0
            and isinstance(selected.get('head_sha'), str)
            and re.fullmatch('[0-9a-f]{40}', selected['head_sha'])
            and selected.get('status') == 'completed'
            and selected.get('conclusion') == 'success', 'Invalid successful attempt')
    start, end = timestamp(selected.get('run_started_at')), timestamp(selected.get('updated_at'))
    require(start <= end, 'Reversed attempt times')
    require(isinstance(listing, dict) and isinstance(listing.get('artifacts'), list)
            and type(listing.get('total_count')) is int
            and listing['total_count'] == len(listing['artifacts']), 'Incomplete artifact listing')
    chosen = {}
    for name in names:
        matches = []
        for row in listing['artifacts']:
            require(isinstance(row, dict), 'Invalid artifact row')
            if row.get('name') != name:
                continue
            created = timestamp(row.get('created_at'))
            if not start <= created <= end:
                continue
            require(row.get('workflow_run', {}).get('id') == selected['id']
                    and row.get('workflow_run', {}).get('head_sha') == selected['head_sha'],
                    'Artifact does not belong to selected run and source')
            if row.get('expired') is not False:
                continue
            require(type(row.get('id')) is int and row['id'] > 0
                    and type(row.get('size_in_bytes')) is int
                    and 0 < row['size_in_bytes'] <= MAX_ARCHIVE_BYTES
                    and isinstance(row.get('digest'), str) and DIGEST.fullmatch(row['digest']),
                    'Invalid immutable artifact metadata')
            matches.append(row)
        require(len(matches) == 1, 'Missing, expired or ambiguous successful-attempt artifact')
        row = matches[0]
        chosen[name] = {key: row[key] for key in ('id', 'digest', 'size_in_bytes', 'created_at')}
    require(len({row['id'] for row in chosen.values()}) == len(chosen), 'Duplicate artifact identity')
    return chosen


def file_digest(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return 'sha256:' + digest.hexdigest()


def download_artifact(repo, record, destination):
    """Download by ID, verify the outer ZIP, then extract bounded ordinary files."""
    require(isinstance(repo, str) and REPOSITORY.fullmatch(repo), 'Invalid repository')
    require(type(record.get('id')) is int and record['id'] > 0
            and type(record.get('size_in_bytes')) is int
            and 0 < record['size_in_bytes'] <= MAX_ARCHIVE_BYTES
            and isinstance(record.get('digest'), str) and DIGEST.fullmatch(record['digest']),
            'Invalid artifact download identity')
    destination = Path(destination)
    archive = destination.with_name(destination.name + '.artifact.zip')
    require(not destination.exists() and not destination.is_symlink()
            and not archive.exists() and not archive.is_symlink(),
            'Artifact output exists; inspect previous attempt')
    archive.parent.mkdir(parents=True, exist_ok=True)
    require(shutil.disk_usage(archive.parent).free >= record['size_in_bytes'] * 3,
            'Insufficient artifact preparation space')
    with archive.open('xb') as output:
        result = subprocess.run(
            ['gh', 'api', f"repos/{repo}/actions/artifacts/{record['id']}/zip"],
            stdout=output, stderr=subprocess.PIPE, timeout=600)
    require(result.returncode == 0, 'Artifact ID download failed; inspect retained partial file')
    require(archive.stat().st_size == record['size_in_bytes']
            and file_digest(archive) == record['digest'], 'Artifact size or digest mismatch')
    with zipfile.ZipFile(archive) as content:
        rows = content.infolist()
        require(0 < len(rows) <= MAX_ENTRIES
                and sum(row.file_size for row in rows) <= MAX_EXTRACTED_BYTES,
                'Artifact extraction exceeds limits')
        seen = set()
        for row in rows:
            # ZipInfo normalizes separators on Windows and truncates NULs;
            # inspect the original central-directory name before extraction.
            name = row.orig_filename
            path = PurePosixPath(name)
            require(not path.is_absolute() and path.parts and '..' not in path.parts
                    and '\\' not in name and ':' not in name
                    and not any(ord(c) < 32 for c in name)
                    and all(part not in {'.', ''} and not part.endswith((' ', '.'))
                            and not re.fullmatch(r'(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)
                            for part in path.parts)
                    and str(path).casefold() not in seen
                    and not row.flag_bits & 1
                    and stat.S_IFMT(row.external_attr >> 16) in (0, stat.S_IFREG, stat.S_IFDIR),
                    'Unsafe artifact entry')
            seen.add(str(path).casefold())
        destination.mkdir()
        for row in rows:
            path = destination / row.filename
            if row.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with content.open(row) as source, path.open('xb') as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)
