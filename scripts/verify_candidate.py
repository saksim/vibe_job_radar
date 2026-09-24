"""Run source-bound local checks; no install, site access or public deployment.

Based on PR29. Each run has an isolated unit-report path and overwrites its own
summary on failure, so stale evidence cannot stand in for an unexecuted check.
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from vibe_job_radar.qualification import check_source, fingerprint, output_directory, require_local_evidence
from vibe_job_radar.guided.browser_health import safe_text
from vibe_job_radar.utils import atomic_json


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
        before = fingerprint(ROOT)
        report['source'] = before
        report['source_checks'] = check_source(ROOT)
        with tempfile.TemporaryDirectory(prefix='radar-qualification-') as temp:
            run_root = Path(temp)
            unit_file = run_root/'unit-tests.json'
            tasks = [('unit-tests',['scripts/run_tests.py','--report',str(unit_file),'--progress']),
                     ('user-guide',['scripts/build_user_guide.py','--check']),
                     ('offline-demo',['scripts/run_demo.py','--out',str(run_root/'demo')]),
                     ('source-doctor',['scripts/start_workbench.py','--doctor','--workspace',str(run_root/'workspace')])]
            for name, args in tasks:
                print('Running:', name, flush=True)
                entry = {'name':name,'returncode':None}
                report['steps'].append(entry)
                try:
                    run = subprocess.run([sys.executable,*args], cwd=ROOT, stdin=subprocess.DEVNULL,
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                         timeout=600 if name == 'unit-tests' else 180, shell=False)
                    entry['returncode'] = run.returncode
                    entry['output_tail'] = safe_text(run.stdout, 3000)
                except subprocess.TimeoutExpired:
                    entry.update(returncode=-1, error='local_check_timeout')
                    break
                if run.returncode:
                    break
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
        require_local_evidence(ROOT, {**report, 'success': True})
        report['success'] = True
    except Exception as exc:
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
    print(json.dumps({k:result[k] for k in ('success','source_unchanged','remote_ci','live_sites')},ensure_ascii=True))
    print('Evidence:', (args.out/'result.json').resolve())
    return 0 if result['success'] else 1


if __name__=='__main__':
    raise SystemExit(main())
