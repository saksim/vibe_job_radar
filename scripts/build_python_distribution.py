"""Build source-bound Python wheel/sdist candidates without changing the checkout."""
from __future__ import annotations
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from vibe_job_radar.qualification import check_source, fingerprint, source_files, output_directory
from vibe_job_radar.utils import atomic_json


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def require(ok, message):
    if not ok:
        raise ValueError(message)


def wheel_payload(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), 'duplicate wheel entries')
        require(all(not PurePosixPath(n).is_absolute() and '..' not in PurePosixPath(n).parts
                    and '\\' not in n for n in names), 'unsafe wheel path')
        return {n:hashlib.sha256(archive.read(n)).hexdigest()
                for n in names if n.startswith('vibe_job_radar/') and not n.endswith('/')}


def build(out):
    out = output_directory(ROOT, Path(out))
    require(not out.exists(), 'output already exists; preserve and inspect the earlier result')
    before = fingerprint(ROOT)
    version = check_source(ROOT)['version']
    revision = subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
    tracked = subprocess.check_output(['git','-C',str(ROOT),'status','--porcelain'],text=True)
    require(not tracked.strip(), 'commit source changes before building a publishable candidate')
    sources = source_files(ROOT)
    payload = {name.removeprefix('src/'):before['files'][name] for name in sources
               if name.startswith('src/vibe_job_radar/')}
    require(payload and 'vibe_job_radar/_version.py' in payload, 'missing package source')
    out.mkdir(parents=True)
    atomic_json(out/'manifest.json', {'schema_version':1,'status':'incomplete','success':False})
    with tempfile.TemporaryDirectory(prefix='python-dist-',dir=out) as folder:
        stage = Path(folder)
        for name in ['pyproject.toml','README.md','LICENSE'] + ['src/'+n for n in payload]:
            source = ROOT/name
            require(sha(source) == before['files'][name], 'source changed during copy')
            dest = stage/name; dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,dest)
        program = 'from setuptools.build_meta import build_wheel,build_sdist; build_wheel("dist"); build_sdist("dist")'
        subprocess.run([sys.executable,'-I','-c',program],cwd=stage,check=True,timeout=180)
        wheels = list((stage/'dist').glob('*.whl')); sdists = list((stage/'dist').glob('*.tar.gz'))
        require(len(wheels)==len(sdists)==1, 'expected one wheel and source archive')
        require(wheels[0].name.endswith('-py3-none-any.whl'), 'wheel must be platform independent')
        require(wheel_payload(wheels[0]) == payload, 'wheel omitted or changed product files')
        with tarfile.open(sdists[0],'r:gz') as archive:
            members=archive.getmembers()
            require(len(members)==len({m.name for m in members}), 'duplicate source archive entries')
            require(all(not PurePosixPath(m.name).is_absolute() and '..' not in PurePosixPath(m.name).parts
                        and '\\' not in m.name and (m.isfile() or m.isdir()) for m in members),
                    'source archive contains unsafe entries')
        require(fingerprint(ROOT)==before, 'checkout changed during package build')
        assets=[]
        for src in wheels+sdists:
            dest=out/src.name; shutil.copyfile(src,dest)
            assets.append({'name':dest.name,'size':dest.stat().st_size,'sha256':sha(dest)})
    atomic_json(out/'manifest.json', {'schema_version':1,'status':'python_distribution_candidate',
        'success':True,'version':version,'source_revision':revision,'source_sha256':before['sha256'],
        'source_file_count':before['file_count'],'package_files':payload,'assets':assets,
        'python_required':'>=3.10','python_bundled':False,'browser_bundled':False,
        'signed':False,'live_sites_certified':False})
    return {'success':True,'version':version,'source_revision':revision,'assets':assets}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=ROOT/'.verify/python-distribution')
    args=parser.parse_args()
    print(json.dumps(build(args.out),ensure_ascii=True))
