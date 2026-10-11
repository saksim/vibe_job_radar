"""Local process/source observations, never a signed or live-site attestation.

The workbench calls bootstrap before importing its product services. Embedders
that imported services earlier remain explicitly unknown. Only bounded metadata
is exported; paths, machine names, command lines and environment values are not.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import uuid

from .qualification import fingerprint, source_files

ALGORITHM = 'sha256-source-text-lf-v1'
HEX64 = re.compile(r'[a-f0-9]{64}')
_STATE = None
_ROOT = None
_PORTABLE = None
_OWNER_PID = os.getpid()


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def content_identity(root):
    """Separate identity; never replace qualification-v1 or old plan hashes."""
    hashes = {}
    for name, path in source_files(Path(root)).items():
        raw = path.read_bytes()
        if not name.startswith('examples/evidence/') and b'\0' not in raw:
            try:
                raw.decode('utf-8')
                raw = raw.replace(b'\r\n', b'\n')
            except UnicodeDecodeError:
                pass
        hashes[name] = _sha(raw)
    raw = json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()
    return {'algorithm': ALGORITHM, 'sha256': _sha(raw), 'file_count': len(hashes)}


def source_stamp(root):
    raw = fingerprint(root)
    content = content_identity(root)
    revision = 'unknown'
    try:
        value = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'],
            stderr=subprocess.DEVNULL, timeout=3, text=True).strip()
        if re.fullmatch(r'[a-f0-9]{40}', value):
            revision = value
    except (OSError, subprocess.SubprocessError):
        pass
    return {'kind': 'source_entry_snapshot', 'revision': revision,
            'raw_sha256': raw['sha256'], 'content_sha256': content['sha256'],
            'content_algorithm': ALGORITHM, 'file_count': raw['file_count'],
            'executable_sha256': None}


def portable_stamp(root):
    path = Path(root)/'PORTABLE.json'
    if path.is_symlink() or path.stat().st_size > 1_000_000:
        raise ValueError()
    value = json.loads(path.read_bytes())
    stamp = value['runtime_source']
    validate_source(stamp)
    if stamp['kind'] != 'portable_build_manifest' or stamp['executable_sha256'] != _sha(Path(sys.executable).read_bytes()):
        raise ValueError()
    if stamp['raw_sha256'] != value['source']['sha256']:
        raise ValueError()
    return stamp


def validate_source(value):
    fields = {'kind', 'revision', 'raw_sha256', 'content_sha256', 'content_algorithm',
              'file_count', 'executable_sha256'}
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError()
    if value['kind'] not in {'source_entry_snapshot', 'portable_build_manifest', 'unknown'}:
        raise ValueError()
    if value['revision'] != 'unknown' and not re.fullmatch('[a-f0-9]{40}', value['revision']):
        raise ValueError()
    for key in ('raw_sha256', 'content_sha256', 'executable_sha256'):
        if value[key] is not None and (not isinstance(value[key], str) or not HEX64.fullmatch(value[key])):
            raise ValueError()
    if value['content_algorithm'] != ALGORITHM or type(value['file_count']) is not int or not 0 <= value['file_count'] <= 10000:
        raise ValueError()
    if value['kind'] != 'unknown' and (not value['raw_sha256'] or not value['content_sha256'] or not value['file_count']):
        raise ValueError()
    if value['kind'] == 'portable_build_manifest' and value['executable_sha256'] is None:
        raise ValueError()
    if value['kind'] == 'source_entry_snapshot' and value['executable_sha256'] is not None:
        raise ValueError()
    if value['kind'] == 'unknown' and (value['revision'] != 'unknown' or value['file_count']
            or any(value[k] is not None for k in ('raw_sha256','content_sha256','executable_sha256'))):
        raise ValueError()


def _unknown_source():
    return {'kind': 'unknown', 'revision': 'unknown', 'raw_sha256': None,
            'content_sha256': None, 'content_algorithm': ALGORITHM,
            'file_count': 0, 'executable_sha256': None}


def _version(value):
    return value if isinstance(value, str) and re.fullmatch(r'[0-9][0-9A-Za-z.+_-]{0,79}', value) else 'unknown'


def _base():
    try:
        playwright = _version(importlib.metadata.version('playwright'))
    except (importlib.metadata.PackageNotFoundError, OSError, ValueError):
        playwright = 'unknown'
    system = platform.system()
    return {'process_id': uuid.uuid4().hex, 'started_at': datetime.now(timezone.utc).isoformat(),
            'python_version': _version(platform.python_version()), 'playwright_version': playwright,
            'os': system if system in {'Windows', 'Linux', 'Darwin'} else 'unknown',
            'os_release': _version(platform.release()), 'source': _unknown_source()}


def bootstrap():
    global _STATE, _ROOT, _PORTABLE
    if _STATE is not None:
        return
    _STATE = _base()
    # A later import cannot manufacture a pre-import source observation.
    if any(name in sys.modules for name in ('vibe_job_radar.workspace', 'vibe_job_radar.guided.service',
                                            'vibe_job_radar.network_policy')):
        return
    try:
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            _PORTABLE = Path(sys.executable).resolve().parent
            _STATE['source'] = portable_stamp(_PORTABLE)
        else:
            root = Path(__file__).resolve().parents[2]
            if not (root/'pyproject.toml').is_file():
                return
            _ROOT = root
            _STATE['source'] = source_stamp(root)
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        _STATE['source'] = _unknown_source()


def snapshot(*, check_current=True):
    global _STATE, _OWNER_PID, _ROOT, _PORTABLE
    if _OWNER_PID != os.getpid():
        # A forked library process must not reuse its parent's process identity.
        _OWNER_PID = os.getpid()
        _STATE, _ROOT, _PORTABLE = None, None, None
    if _STATE is None:
        # Direct library use is intentionally not promoted to entrypoint proof.
        _STATE = _base()
    if _STATE['source']['kind'] == 'unknown':
        result = copy.deepcopy(_STATE)
        result['freshness'] = 'uninitialized'
        return result
    result = copy.deepcopy(_STATE)
    result['freshness'] = 'unavailable'
    if result['source']['kind'] == 'unknown':
        return result
    if not check_current:
        result['freshness'] = 'startup_only'
        return result
    try:
        if _ROOT is not None:
            same = (content_identity(_ROOT)['sha256'] == result['source']['content_sha256']
                    and fingerprint(_ROOT)['sha256'] == result['source']['raw_sha256'])
        else:
            same = portable_stamp(_PORTABLE) == result['source']
        result['freshness'] = 'unchanged' if same else 'changed'
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        pass
    return result


def validate(value):
    if not isinstance(value, dict) or set(value) != {'process_id', 'started_at', 'python_version',
            'playwright_version', 'os', 'os_release', 'source', 'freshness'}:
        raise ValueError()
    if not isinstance(value['process_id'], str) or not re.fullmatch('[a-f0-9]{32}', value['process_id']):
        raise ValueError()
    date = datetime.fromisoformat(value['started_at'])
    if date.utcoffset() is None:
        raise ValueError()
    if value['os'] not in {'Windows', 'Linux', 'Darwin', 'unknown'}:
        raise ValueError()
    if value['freshness'] not in {'uninitialized', 'unavailable', 'startup_only', 'unchanged', 'changed'}:
        raise ValueError()
    for key in ('python_version', 'playwright_version', 'os_release'):
        if _version(value[key]) != value[key]:
            raise ValueError()
    validate_source(value['source'])
