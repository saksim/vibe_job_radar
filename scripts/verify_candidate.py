"""Run source-bound local checks; no install, site access or public deployment.

Based on PR29. Each run has an isolated unit-report path and overwrites its own
summary on failure, so stale evidence cannot stand in for an unexecuted check.
"""
from __future__ import annotations

import argparse
import json
import math
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UNIT_SNAPSHOT_SECONDS = 570
# Complete supported Windows suite measured 677s; see SOURCE_QUALIFICATION.md.
UNIT_TEST_TIMEOUT_SECONDS = 1200
MAX_FAILURE_EVIDENCE_BYTES = 10_000_000
sys.path.insert(0, str(ROOT/'src'))
from vibe_job_radar.qualification import check_source, fingerprint, output_directory, require_local_evidence
from vibe_job_radar.guided.browser_health import safe_text
from vibe_job_radar.utils import atomic_json, atomic_text


def _failure_record(record: object) -> bool:
    """Accept only the existing FailureResult code-location schema."""
    def text(value, limit=1024):
        return isinstance(value, str) and 0 < len(value) <= limit and value.isprintable()
    def integer(value):
        return type(value) is int and value >= 0
    if not isinstance(record, dict):
        return False
    common = {'event', 'test', 'elapsed_seconds'}
    if set(record) not in (common | {'capture_status'}, common | {'threads', 'threads_omitted'}):
        return False
    if record['event'] not in ('failure', 'error', 'subtest_failure', 'subtest_error'):
        return False
    elapsed = record['elapsed_seconds']
    if not text(record['test']) or type(elapsed) not in (int, float) or not math.isfinite(elapsed) or elapsed < 0:
        return False
    if 'capture_status' in record:
        return record['capture_status'] == 'unavailable'
    threads = record['threads']
    if not isinstance(threads, list) or len(threads) > 16 or not integer(record['threads_omitted']):
        return False
    for thread in threads:
        if not isinstance(thread, dict) or set(thread) != {'current', 'stack', 'frames_truncated'}:
            return False
        if type(thread['current']) is not bool or type(thread['frames_truncated']) is not bool:
            return False
        if not isinstance(thread['stack'], list) or len(thread['stack']) > 24:
            return False
        for frame in thread['stack']:
            if not isinstance(frame, dict) or set(frame) != {'file', 'line', 'function'}:
                return False
            if (not text(frame['file']) or any(char in frame['file'] for char in '/\\:')
                    or not text(frame['function']) or not integer(frame['line'])):
                return False
    return True


def retain_failure_evidence(source: Path, target: Path) -> str:
    """Keep bounded current diagnostics without changing the original test result."""
    def unique_fields(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError('duplicate fields')
        return value
    try:
        if source.is_symlink():
            return 'invalid'
        try:
            stream = source.open('rb')
        except FileNotFoundError:
            return 'missing'
        with stream:
            raw = stream.read(MAX_FAILURE_EVIDENCE_BYTES + 1)
        if len(raw) > MAX_FAILURE_EVIDENCE_BYTES:
            return 'too_large'
        content = raw.decode('utf-8')
        lines = content.splitlines()
        if len(lines) > 10_000:
            return 'too_large'
        if any(not _failure_record(json.loads(line, object_pairs_hook=unique_fields)) for line in lines):
            return 'invalid'
        atomic_text(target, content)
        return 'retained' if lines else 'empty'
    except OSError:
        return 'unavailable'
    except (ValueError, TypeError, RecursionError, OverflowError):
        return 'invalid'


def verify(out: Path) -> dict:
    out = output_directory(ROOT, out)
    out.mkdir(parents=True, exist_ok=True)
    report = {'schema_version':1, 'kind':'local-candidate-verification',
              'created_at':datetime.now(timezone.utc).isoformat(), 'python':sys.version,
              'platform':platform.platform(), 'success':False, 'source_unchanged':False,
              'remote_ci':'not_verified_by_this_command', 'live_sites':'not_tested',
              'steps':[], 'tests':{}, 'scope':'Local source checks only, not signed attestation or release approval.'}
    try:
        (out/'unit-tests.json').unlink(missing_ok=True)
        (out/'unit-tests.progress.log').unlink(missing_ok=True)
        (out/'unit-tests.failures.jsonl').unlink(missing_ok=True)
        before = fingerprint(ROOT)
        report['source'] = before
        report['source_checks'] = check_source(ROOT)
        with tempfile.TemporaryDirectory(prefix='radar-qualification-') as temp:
            run_root = Path(temp)
            unit_file = run_root/'unit-tests.json'
            tasks = [('unit-tests',['scripts/run_tests.py','--report',str(unit_file),'--progress',
                                    '--suite-snapshot-after',str(UNIT_SNAPSHOT_SECONDS)]),
                     ('user-guide',['scripts/build_user_guide.py','--check']),
                     ('offline-demo',['scripts/run_demo.py','--out',str(run_root/'demo')]),
                     ('source-doctor',['scripts/start_workbench.py','--doctor','--workspace',str(run_root/'workspace')])]
            for name, args in tasks:
                print('Running:', name, flush=True)
                entry = {'name':name,'returncode':None}
                if name == 'unit-tests':
                    entry['suite_snapshot_after_seconds'] = UNIT_SNAPSHOT_SECONDS
                report['steps'].append(entry)
                timeout = UNIT_TEST_TIMEOUT_SECONDS if name == 'unit-tests' else 180
                try:
                    run = subprocess.run([sys.executable,*args], cwd=ROOT, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         timeout=timeout, shell=False)
                    entry['returncode'] = run.returncode
                    entry['output_tail'] = safe_text(run.stdout, 3000)
                except subprocess.TimeoutExpired:
                    entry.update(returncode=-1, error='local_check_timeout', timeout_seconds=timeout)
                    report.update(error_type='TimeoutExpired', error='local_check_timeout',
                                  failed_check=name, timeout_seconds=timeout)
                    break
                if run.returncode:
                    break
            # FailureResult flushes this before tearDown, even if the final
            # summary is absent. Retain it before deleting the isolated run.
            report['unit_failure_evidence'] = retain_failure_evidence(
                unit_file.with_suffix('.failures.jsonl'), out/'unit-tests.failures.jsonl')
            progress=unit_file.with_suffix('.progress.log')
            if progress.is_file() and progress.stat().st_size<=10_000_000:
                shutil.copyfile(progress,out/'unit-tests.progress.log')
            if unit_file.is_file():
                if unit_file.stat().st_size > 10_000_000:
                    raise ValueError('unit report is too large')
                report['tests'] = json.loads(unit_file.read_text(encoding='utf-8'))
                atomic_json(out/'unit-tests.json', report['tests'])
        report['source_unchanged'] = fingerprint(ROOT) == before
        # One validator for both the producer and builder, including skipped
        # tests, strict integer counts, exact step order and matching bytes.
        # A stopped check is already an explicit failure. Keep its stage and
        # deadline instead of replacing it with a secondary missing-report error.
        if report.get('error') != 'local_check_timeout':
            require_local_evidence(ROOT, {**report, 'success': True})
            report['success'] = True
    except Exception as exc:
        if report.get('error') == 'local_check_timeout':
            # Preserve the earlier timeout if retaining its evidence also fails.
            # Export fixed types, never a custom class name or exception text.
            kinds = (json.JSONDecodeError, UnicodeError, OSError, ValueError)
            report['post_timeout_error_type'] = next(
                (kind.__name__ for kind in kinds if isinstance(exc, kind)), 'Exception')
        else:
            report['error_type'] = type(exc).__name__
            report['error'] = safe_text(exc)
    finally:
        atomic_json(out/'result.json', report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=ROOT/'release-verification')
    args = parser.parse_args()
    try:
        result = verify(args.out)
    except (OSError, ValueError) as exc:
        print('Verification refused:', safe_text(exc), file=sys.stderr)
        return 2
    summary = {k:result[k] for k in ('success','source_unchanged','remote_ci','live_sites')}
    if not result['success']:
        summary.update({k:result[k] for k in ('error_type','error','failed_check','timeout_seconds','post_timeout_error_type') if k in result})
    print(json.dumps(summary,ensure_ascii=True))
    print('Evidence:', (args.out/'result.json').resolve())
    return 0 if result['success'] else 1


if __name__=='__main__':
    raise SystemExit(main())
