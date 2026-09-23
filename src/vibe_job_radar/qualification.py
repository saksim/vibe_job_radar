"""Bind candidate evidence to source bytes; not a signed or live-site attestation.

Adapted from the independent PR29 source-bound qualification work. Runtime
workspaces and root .env files are not payload. Obvious secret/database files
inside a source directory are rejected, not silently bundled.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
from pathlib import Path

ROOT_FILES = {'README.md','README_CLI.md','START_HERE.html','LICENSE','pyproject.toml',
              'start_windows.bat','start_macos.command','get_real_example_windows.bat',
              '.env.example','.gitattributes','.gitignore'}
SOURCE_DIRS = {'src','scripts','tests','docs','examples','demo_output','.github'}
TEST_CERTIFICATES = {'tests/fixtures/connection_test_only.pem',
                     'tests/fixtures/encrypted_dns_test_only.pem'}
CHECKS = ('unit-tests', 'user-guide', 'offline-demo', 'source-doctor')


def source_files(root: Path) -> dict[str, Path]:
    """Select payload; a source symlink or obvious private file fails closed."""
    root = Path(root)
    if root.is_symlink():
        raise ValueError('source root is a symlink')
    result = {}
    for prefix in sorted(SOURCE_DIRS):
        base = root / prefix
        if base.is_symlink():
            raise ValueError('source directory is a symlink')
        if not base.exists():
            continue
        for path in base.rglob('*'):
            rel = path.relative_to(root)
            if '.radar-sessions' in rel.parts:
                raise ValueError('private browser state inside source payload')
            if '.radar-acceptance' in rel.parts:
                raise ValueError('private acceptance observations inside source payload')
            if '__pycache__' in rel.parts or path.suffix in {'.pyc','.pyo','.zip','.log'}:
                continue
            if path.is_symlink():
                raise ValueError('source file is a symlink')
            if path.is_file():
                if (path.name == '.env' or path.name.startswith('.env.') and path.name != '.env.example'
                        or path.suffix.lower() in {'.sqlite','.sqlite3','.db','.key','.p12','.pfx'}
                        or path.suffix.lower() == '.pem' and rel.as_posix() not in TEST_CERTIFICATES):
                    raise ValueError('private-looking file inside source payload')
                result[rel.as_posix()] = path
    for name in ROOT_FILES:
        path = root / name
        if path.is_symlink():
            raise ValueError('source file is a symlink')
        if path.is_file():
            result[name] = path
    return dict(sorted(result.items()))


def fingerprint(root: Path) -> dict:
    hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name,path in source_files(root).items()}
    encoded = json.dumps(hashes, sort_keys=True, separators=(',', ':')).encode()
    return {'algorithm': 'sha256-file-manifest-v1', 'sha256': hashlib.sha256(encoded).hexdigest(),
            'files': hashes, 'file_count': len(hashes)}


def check_source(root: Path) -> dict:
    count = 0
    for name, path in source_files(root).items():
        if path.suffix == '.py':
            ast.parse(path.read_text(encoding='utf-8-sig'), filename=name)
            count += 1
    version_text = (root / 'src/vibe_job_radar/_version.py').read_text(encoding='utf-8')
    package_text = (root / 'pyproject.toml').read_text(encoding='utf-8')
    version = re.search(r'^__version__\s*=\s*"([^"]+)"', version_text, re.M)
    package = re.search(r'^version\s*=\s*"([^"]+)"', package_text, re.M)
    dynamic = ('dynamic = ["version"]' in package_text
               and '[tool.setuptools.dynamic]' in package_text
               and 'version = {attr = "vibe_job_radar._version.__version__"}' in package_text)
    if (not version or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+(?:[a-zA-Z0-9.+-]*)', version.group(1))
            or package and version.group(1) != package.group(1) or not package and not dynamic):
        raise ValueError('package/report versions do not match or are unsafe')
    return {'python_files_parsed': count, 'version': version.group(1)}


def require_local_evidence(root: Path, evidence: dict) -> dict:
    if not isinstance(evidence, dict):
        raise ValueError('verification evidence must be an object')
    if (type(evidence.get('schema_version')) is not int or evidence['schema_version'] != 1
            or evidence.get('kind') != 'local-candidate-verification'):
        raise ValueError('not a candidate verification report')
    if evidence.get('success') is not True:
        raise ValueError('local checks did not pass')
    tests = evidence.get('tests', {})
    if (not isinstance(tests, dict) or type(tests.get('tests_run')) is not int or tests['tests_run'] <= 0
            or tests.get('success') is not True
            or any(type(tests.get(key)) is not int or tests[key] != 0 for key in ('failures','errors','skipped'))):
        raise ValueError('missing, failing, or skipped test evidence')
    steps = evidence.get('steps', [])
    if (not isinstance(steps,list) or any(not isinstance(s,dict) for s in steps)
            or tuple(s.get('name') for s in steps) != CHECKS
            or any(type(s.get('returncode')) is not int or s['returncode'] != 0 for s in steps)):
        raise ValueError('required local checks are incomplete')
    current = fingerprint(root)
    if evidence.get('source') != current or evidence.get('source_unchanged') is not True:
        raise ValueError('source changed after validation; rerun verify_candidate.py')
    check_source(root)
    return current


def output_directory(root: Path, out: Path) -> Path:
    """No generated evidence/candidate is written over the reviewed payload."""
    root, out = root.resolve(), out.resolve()
    if out == root or any(out == root/name or root/name in out.parents for name in SOURCE_DIRS | {'.git'}):
        raise ValueError('output must not be inside source directories or the source root')
    return out
