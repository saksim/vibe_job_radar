"""Build and exercise a source-verified Windows x64 portable candidate, not a release."""
from __future__ import annotations
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.qualification import check_source,fingerprint,output_directory,require_local_evidence
from vibe_job_radar.utils import atomic_json

BUILD_VERSIONS={'pyinstaller':'6.22.3','playwright':'1.63.0','packaging':'26.0','truststore':'0.10.4'}


def file_hash(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    return digest.hexdigest()


def inventory(folder):
    entries={}
    for path in sorted(folder.rglob('*')):
        if path.is_symlink():raise ValueError('portable payload contains a symlink')
        if path.is_file():entries[path.relative_to(folder).as_posix()]=file_hash(path)
    return entries


def relocate_browsers(bundle):
    root=bundle.resolve()
    source=bundle/'_internal/playwright/driver/package/.local-browsers'
    target=bundle/'browsers'
    if (source.is_symlink() or target.exists() or target.is_symlink()
            or not source.resolve().is_relative_to(root) or not target.resolve().is_relative_to(root)):
        raise ValueError('invalid portable browser relocation')
    if not source.is_dir() or not any(source.rglob('chrome.exe')):
        raise ValueError('packaged Chromium was not found')
    # Both absolute paths were checked to remain inside this disposable bundle.
    shutil.move(str(source),str(target))


def check_payload_path_lengths(bundle):
    # A <=110 UTF-16-unit bundle root plus separator + <=140-unit relative
    # filenames stays below legacy Windows MAX_PATH, including its final NUL.
    for path in bundle.rglob('*'):
        if len(str(path.relative_to(bundle)).encode('utf-16-le'))//2>140:
            raise ValueError('portable payload path is too deeply nested')


def build(out,evidence_path,*,verify_login_startup=False):
    if verify_login_startup and os.environ.get('GITHUB_ACTIONS')!='true':
        raise ValueError('login startup registration acceptance is restricted to an ephemeral CI runner')
    out=output_directory(ROOT,out);out.mkdir(parents=True,exist_ok=True)
    # A failed rerun must not leave a previous success as this run's manifest.
    atomic_json(out/'manifest.json',{'status':'build_incomplete','runtime_verified':False})
    atomic_json(out/'portable-verification.json',{'success':False,'stage':'not_started'})
    try:
        return build_candidate(out,evidence_path,verify_login_startup=verify_login_startup)
    except Exception as exc:
        atomic_json(out/'manifest.json',{'status':'build_failed','runtime_verified':False,
                                       'error_type':type(exc).__name__})
        raise


def build_candidate(out,evidence_path,*,verify_login_startup=False):
    if sys.platform!='win32' or platform.machine().lower() not in {'amd64','x86_64'} or sys.maxsize<=2**32:
        raise ValueError('portable candidate builder requires Windows x64 Python')
    if evidence_path.is_symlink() or evidence_path.stat().st_size>10_000_000:raise ValueError('invalid source evidence')
    expected=require_local_evidence(ROOT,json.loads(evidence_path.read_text(encoding='utf-8')))
    versions={key:importlib.metadata.version(key) for key in BUILD_VERSIONS}
    if versions!=BUILD_VERSIONS:raise ValueError('portable build dependencies do not match pinned versions')
    import playwright
    browser_root=Path(playwright.__file__).parent/'driver'/'package'/'.local-browsers'
    if browser_root.is_symlink() or not any(browser_root.rglob('chrome.exe')):
        raise ValueError('install matching bundled Chromium with PLAYWRIGHT_BROWSERS_PATH=0 before building')
    version=check_source(ROOT)['version']
    dependencies={dist.metadata['Name']:dist.version for dist in importlib.metadata.distributions()}
    with tempfile.TemporaryDirectory(prefix='.portable-build-',dir=out) as temp:
        stage=Path(temp).resolve()
        if out.resolve() not in stage.parents:raise ValueError('invalid build staging location')
        args=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--console','--noupx',
              '--name','VibeJobRadar','--paths',str(ROOT/'src'),'--collect-data','vibe_job_radar',
              '--collect-submodules','vibe_job_radar']
        for package in ('playwright','packaging','truststore'):
            args.extend(['--collect-all',package,'--copy-metadata',package])
        args.extend(['--distpath',str(stage/'程序目录 with spaces'),'--workpath',str(stage/'build'),'--specpath',str(stage),
                     str(ROOT/'scripts'/'portable_entry.py')])
        # Collection hooks run before Analysis applies --paths. Make the source
        # package discoverable to that process as well, without installing it.
        subprocess.run(args,cwd=ROOT,env={**os.environ,'PLAYWRIGHT_BROWSERS_PATH':'0',
                       'PYTHONPATH':str(ROOT/'src')},check=True,timeout=600)
        bundle=stage/'程序目录 with spaces'/'VibeJobRadar'
        if not (bundle/'VibeJobRadar.exe').is_file():raise ValueError('executable was not built')
        relocate_browsers(bundle)
        for source in (ROOT/'src'/'vibe_job_radar').iterdir():
            if source.is_file() and source.suffix in {'.json','.html','.js'}:
                target=bundle/'_internal'/'vibe_job_radar'/source.name
                if not target.is_file() or file_hash(target)!=file_hash(source):
                    raise ValueError(f'packaged application resource absent or changed: {source.name}')
        licenses=bundle/'licenses';licenses.mkdir()
        shutil.copyfile(ROOT/'LICENSE',licenses/'VibeJobRadar.txt')
        python_license=Path(sys.base_prefix)/'LICENSE.txt'
        if not python_license.is_file():raise ValueError('Python runtime license was not found')
        shutil.copyfile(python_license,licenses/'Python.txt')
        collected=[]
        for package in ('pyinstaller','playwright','packaging','truststore','greenlet','pyee','typing_extensions'):
            dist=importlib.metadata.distribution(package)
            found=False
            for item in dist.files or []:
                if not any(word in str(item).lower() for word in ('license','copying','notice')):continue
                source=Path(dist.locate_file(item))
                if source.is_file():
                    target=licenses/package/str(item).replace('..','_').replace('\\','_').replace('/','_')
                    target.parent.mkdir(exist_ok=True);shutil.copyfile(source,target);collected.append(target.relative_to(bundle).as_posix())
                    found=True
            if not found:raise ValueError(f'dependency license missing: {package}')
        (bundle/'START_HERE.txt').write_text(
            'Vibe Job Radar Windows x64 便携候选包（尚非正式签名发行）\n\n'
            '完整解压整个目录后双击 VibeJobRadar.exe。不要只复制exe或从zip内部运行。\n'
            '建议VibeJobRadar程序文件夹完整路径不超过110字符，避免Windows深层目录限制。无需修改系统长路径设置。\n'
            '自带Python、Playwright和配套Chromium；无需改动原Anaconda环境。\n'
            '打开本机地址后可检查采集浏览器，也可明确选择已安装的Edge。组件更新请更换完整候选包。\n'
            '默认数据仍在用户目录 .vibe-job-radar；程序不会将工作区放进本包。\n'
            '升级/回退前停止任务与计划、关闭所有工作台，并备份整个工作区。\n'
            '可在首页明确选择登录Windows后启动；默认关闭。移动/升级包前关闭旧登记，再从新包启用。\n'
            '三站实站登录/完整JD仍未认证；包的启动成功不代表网站允许采集。\n'
            '不自动安装开机任务、更新软件、发布服务或修改系统证书。\n',encoding='utf-8')
        atomic_json(bundle/'PORTABLE.json',{'schema_version':1,'status':'portable_candidate_not_release',
            'version':version,'platform':'windows-x64','source':expected,'python_version':platform.python_version(),
            'components':versions,'build_environment_packages':dependencies,'browser_bundled':True,'live_sites_certified':False,
            'bundled_browser_directory':'browsers','recommended_max_bundle_path_units':110,
            'runtime_evidence':'portable-verification.json','dependency_licenses':collected})
        check_payload_path_lengths(bundle)
        # Outside disposable build staging so an executable failure remains
        # inspectable in CI. The verifier never writes session tokens/logs.
        runtime_evidence=out/'portable-verification.json'
        verification=[sys.executable,str(ROOT/'scripts'/'verify_windows_portable.py'),
            '--bundle',str(bundle),'--report',str(runtime_evidence)]
        if verify_login_startup:verification.append('--verify-login-startup')
        subprocess.run(verification,cwd=ROOT,check=True,timeout=240)
        report=json.loads(runtime_evidence.read_text(encoding='utf-8'))
        validate_runtime_evidence(bundle,report,require_login_startup=verify_login_startup)
        if fingerprint(ROOT)!=expected:raise ValueError('source changed during portable build')
        part=stage/'candidate.zip'
        with zipfile.ZipFile(part,'w',zipfile.ZIP_DEFLATED) as archive:
            for name in report['files']:archive.write(bundle/name,'VibeJobRadar/'+name)
            archive.write(runtime_evidence,'VibeJobRadar/portable-verification.json')
        validate_archive(part,{**report['files'],'portable-verification.json':file_hash(runtime_evidence)})
        digest=file_hash(part)
        dest=out/f'vibe-job-radar-{version}-{digest[:16]}-windows-x64.zip'
        if dest.exists() or dest.is_symlink():raise ValueError('candidate destination already exists')
        # Destination is a fixed filename inside the validated output directory.
        created=False
        try:
            with dest.open('xb') as target,part.open('rb') as source:
                created=True
                shutil.copyfileobj(source,target,1024*1024)
        except Exception:
            # Only this newly created fixed candidate file is removed.
            # Older candidates have different names and are left intact.
            if created and dest.is_file():dest.unlink()
            raise
        atomic_json(out/'manifest.json',{'file':dest.name,'sha256':digest,'source_sha256':expected['sha256'],
            'status':'portable_candidate_not_release','runtime_verified':True,'live_sites_certified':False,
            'components':versions,'platform':'windows-x64','size_bytes':dest.stat().st_size,
            'startup_registration_verified':report.get('startup_registration_verified') is True,
            'startup_worker_verified':report.get('startup_worker_verified') is True})
        return dest


def validate_runtime_evidence(bundle,report,*,require_login_startup=False):
    if (not isinstance(report,dict) or report.get('success') is not True
            or report.get('verified_browser')!='bundled'
            or (require_login_startup and (report.get('startup_registration_verified') is not True
                                          or report.get('startup_worker_verified') is not True))
            or not isinstance(report.get('files'),dict) or not report['files']
            or report['files']!=inventory(bundle)):
        raise ValueError('portable runtime evidence does not match payload')


def validate_archive(path,expected):
    with zipfile.ZipFile(path) as archive:
        names=archive.namelist()
        if len(names)!=len(set(names)) or set(names)!={'VibeJobRadar/'+name for name in expected}:
            raise ValueError('archive file list differs from verified payload')
        for name,digest in expected.items():
            actual=hashlib.sha256()
            with archive.open('VibeJobRadar/'+name) as source:
                for chunk in iter(lambda:source.read(1024*1024),b''):actual.update(chunk)
            if actual.hexdigest()!=digest:raise ValueError('archive bytes differ from verified payload')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,default=ROOT/'portable-candidate')
    parser.add_argument('--evidence',type=Path,default=ROOT/'release-verification'/'result.json')
    parser.add_argument('--verify-login-startup',action='store_true',
        help='Explicit ephemeral-CI-only register/read/remove test; never used by ordinary local builds.')
    args=parser.parse_args()
    print(build(args.out,args.evidence,verify_login_startup=args.verify_login_startup))
