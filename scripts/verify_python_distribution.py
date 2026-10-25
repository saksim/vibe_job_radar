"""Install one immutable Python wheel and exercise it in two independent processes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import venv

ROOT=Path(__file__).resolve().parents[1]


def run(args, *, cwd, timeout=90):
    return subprocess.run(args,cwd=cwd,check=True,capture_output=True,text=True,encoding='utf-8',
                          timeout=timeout,env={**os.environ,'PYTHONUTF8':'1'})


def verify(candidate,out):
    candidate=Path(candidate).resolve();out=Path(out).resolve()
    if out.exists():raise ValueError('result directory already exists; preserve the prior evidence')
    out.mkdir(parents=True)
    result={'success':False,'platform':sys.platform,'steps':[]}
    try:
        manifest=json.loads((candidate/'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('status')!='python_distribution_candidate' or manifest.get('success') is not True:
            raise ValueError('candidate is not complete')
        wheels=[x for x in manifest['assets'] if x['name'].endswith('-py3-none-any.whl')]
        if len(wheels)!=1:raise ValueError('ambiguous universal wheel')
        wheel=candidate/wheels[0]['name']
        if (wheel.resolve().parent!=candidate or wheel.is_symlink() or wheel.stat().st_size!=wheels[0]['size']
                or hashlib.sha256(wheel.read_bytes()).hexdigest()!=wheels[0]['sha256']):
            raise ValueError('wheel differs from candidate')
        with tempfile.TemporaryDirectory(prefix='installed-wheel-',dir=out) as folder:
            work=Path(folder);environment=work/'venv'
            venv.EnvBuilder(with_pip=True).create(environment)
            binary=environment/('Scripts' if os.name=='nt' else 'bin')
            python=binary/('python.exe' if os.name=='nt' else 'python')
            run([str(python),'-I','-m','pip','install','--no-index','--no-deps',str(wheel)],cwd=work)
            result['steps'].append('offline_install')
            for entry in ['vibe-radar','vibe-radar-ui','vibe-radar-collect','vibe-radar-service','vibe-radar-team']:
                run([str(binary/(entry+('.exe' if os.name=='nt' else ''))),'--help'],cwd=work)
            result['steps'].append('five_installed_entrypoints')
            run([str(binary/('vibe-radar-web.exe' if os.name=='nt' else 'vibe-radar-web')),
                 '--help'],cwd=work)
            run([str(python),'-I','-c',
                 'from importlib.resources import files; '
                 'assert all(files("vibe_job_radar").joinpath(n).read_bytes() for n in '
                 '("server_login.html","server_login.js","server_ui.js","report_tasks.js","evidence_draft.js"))'],cwd=work)
            result['server_entrypoint_and_assets_checked']=True
            run([str(binary/('vibe-radar-ui.exe' if os.name=='nt' else 'vibe-radar-ui')),
                 '--doctor','--workspace',str(work/'doctor')],cwd=work)
            config=work/'config.json'
            config.write_text(json.dumps({**manifest,'workspace':str(work/'workspace')}),encoding='utf-8')
            code=(ROOT/'scripts/python_distribution_probe.py').read_text(encoding='utf-8')
            observations=[]
            for stage in ['import_and_original_report','independent_process_reopen']:
                answer=run([str(python),'-I','-c',code,str(config)],cwd=work)
                row=json.loads(answer.stdout)
                if row.get('success') is not True or row.get('stage')!=stage:raise ValueError('installed check incomplete')
                observations.append(row)
            result.update(success=True,version=manifest['version'],source_revision=manifest['source_revision'],
                source_sha256=manifest['source_sha256'],wheel_sha256=wheels[0]['sha256'],
                observations=observations,python_required=True,browser_bundled=False,live_sites_certified=False)
    except Exception as exc:
        result['error_type']=type(exc).__name__
        if isinstance(exc,subprocess.CalledProcessError):
            (out/'failure.log').write_text((exc.stdout or '')+(exc.stderr or ''),encoding='utf-8')
        raise
    finally:(out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(verify(args.candidate,args.out),ensure_ascii=True))
