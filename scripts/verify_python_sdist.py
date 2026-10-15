"""Rebuild the source distribution without checkout files or network access."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
from build_python_distribution import wheel_payload


def verify(candidate,out):
    candidate=Path(candidate).resolve();out=Path(out).resolve()
    if out.exists():raise ValueError('result directory already exists')
    out.mkdir(parents=True)
    result={'success':False}
    try:
        manifest=json.loads((candidate/'manifest.json').read_text(encoding='utf-8'))
        rows=[x for x in manifest['assets'] if x['name'].endswith('.tar.gz')]
        if len(rows)!=1 or manifest.get('success') is not True:raise ValueError('invalid source candidate')
        source=candidate/rows[0]['name']
        if (source.resolve().parent!=candidate or source.is_symlink() or source.stat().st_size!=rows[0]['size']
                or hashlib.sha256(source.read_bytes()).hexdigest()!=rows[0]['sha256']):
            raise ValueError('source archive differs from candidate')
        with tempfile.TemporaryDirectory(prefix='sdist-rebuild-',dir=out) as folder:
            work=Path(folder)
            with tarfile.open(source,'r:gz') as archive:
                entries=archive.getmembers()
                if len(entries)>2000 or sum(m.size for m in entries)>20_000_000:
                    raise ValueError('oversized source archive')
                roots=set()
                for item in entries:
                    path=PurePosixPath(item.name)
                    if (not path.parts or path.is_absolute() or '..' in path.parts or '\\' in item.name
                            or not (item.isfile() or item.isdir())):raise ValueError('unsafe source entry')
                    roots.add(path.parts[0]);dest=work.joinpath(*path.parts)
                    if not dest.resolve().is_relative_to(work.resolve()):raise ValueError('escaping source entry')
                    if item.isdir():dest.mkdir(parents=True,exist_ok=True)
                    else:
                        dest.parent.mkdir(parents=True,exist_ok=True)
                        with archive.extractfile(item) as stream:dest.write_bytes(stream.read())
                if len(roots)!=1:raise ValueError('ambiguous source root')
            stage=work/roots.pop()
            command=[sys.executable,'-I','-c','from setuptools.build_meta import build_wheel; build_wheel("rebuilt")']
            run=subprocess.run(command,cwd=stage,capture_output=True,text=True,encoding='utf-8',timeout=180)
            if run.returncode:
                (out/'failure.log').write_text(run.stdout+run.stderr,encoding='utf-8')
                raise ValueError('source rebuild failed')
            wheels=list((stage/'rebuilt').glob('*.whl'))
            if len(wheels)!=1 or wheel_payload(wheels[0])!=manifest['package_files']:
                raise ValueError('rebuilt product payload differs')
            result.update(success=True,version=manifest['version'],source_revision=manifest['source_revision'],
                source_sha256=manifest['source_sha256'],sdist_sha256=rows[0]['sha256'],
                product_payload_identical=True,build_network_required=False)
    except Exception as exc:
        result['error_type']=type(exc).__name__;raise
    finally:(out/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args();print(json.dumps(verify(args.candidate,args.out)))
