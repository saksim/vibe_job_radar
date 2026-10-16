"""Build native POSIX candidates and exercise their extracted executable once."""
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
import sysconfig
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from vibe_job_radar.qualification import fingerprint,check_source,output_directory
from vibe_job_radar.runtime_identity import source_stamp
from vibe_job_radar.utils import atomic_json
from posix_artifacts import pack,extract_verified,require

VERSIONS={'pyinstaller':'6.22.3','playwright':'1.63.0','packaging':'26.0',
          'quickjs-ng':'0.17.0.1','pyspnego':'0.12.4','cryptography':'50.0.2',
          'cffi':'2.1.1','pycparser':'3.11'}


def file_hash(path):
    value=hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda:source.read(1024*1024),b''):value.update(block)
    return value.hexdigest()


def platform_key():
    machine=platform.machine().lower()
    if sys.platform=='linux' and machine in {'x86_64','amd64'}:return 'linux-x64'
    if sys.platform=='darwin' and machine=='arm64':return 'macos-arm64'
    raise ValueError('Build this candidate on its supported native OS and architecture')


def collect_licenses(bundle):
    directory=bundle/'licenses';directory.mkdir()
    shutil.copyfile(ROOT/'LICENSE',directory/'VibeJobRadar.txt')
    locations=[Path(sys.base_prefix)/'LICENSE.txt',Path(sysconfig.get_path('stdlib'))/'LICENSE.txt']
    license_file=next((p for p in locations if p.is_file()),None)
    require(license_file is not None,'Python runtime license missing')
    shutil.copyfile(license_file,directory/'Python.txt')
    shutil.copyfile(ROOT/'docs/licenses/QUICKJS_NG_NOTICES.txt',directory/'QuickJS_NG_Notices.txt')
    packages=tuple(VERSIONS)+('greenlet','pyee','typing_extensions')
    for package in packages:
        distribution=importlib.metadata.distribution(package)
        found=False
        for entry in distribution.files or []:
            if not any(word in str(entry).lower() for word in ('license','copying','notice')):continue
            source=Path(distribution.locate_file(entry))
            if source.is_file():
                target=directory/package/str(entry).replace('..','_').replace('/','_').replace('\\','_')
                target.parent.mkdir(exist_ok=True)
                shutil.copyfile(source,target);found=True
        require(found,'Dependency license missing: '+package)


def build(out):
    target=platform_key()
    out=output_directory(ROOT,out)
    require(not out.exists() and not out.is_symlink(),'Candidate output exists; preserve prior evidence')
    out.mkdir(parents=True)
    result=dict(success=False,status='posix_candidate_incomplete',platform=target)
    try:
        version=check_source(ROOT)['version']
        source=fingerprint(ROOT)
        observed={name:importlib.metadata.version(name) for name in VERSIONS}
        require(observed==VERSIONS,'Pinned native build components differ')
        import playwright
        from playwright.sync_api import sync_playwright
        browsers=Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH','')).resolve()
        require(browsers.is_relative_to((ROOT/'.verify').resolve()) and browsers.is_dir()
                and not browsers.is_symlink(),'Install matching Chromium in the project-local build cache')
        require(not (Path(playwright.__file__).parent/'driver/package/.local-browsers').exists(),
                'Keep browser app bundles outside PyInstaller binary rewriting')
        with sync_playwright() as runtime:
            chromium=Path(runtime.chromium.executable_path)
        require(chromium.is_file() and chromium.resolve().is_relative_to(browsers.resolve()),'Bundled Chromium missing')
        chromium_relative=chromium.relative_to(browsers)
        with tempfile.TemporaryDirectory(prefix='native-posix-',dir=out) as temporary:
            stage=Path(temporary).resolve()
            require(stage.is_relative_to(out.resolve()),'Invalid build staging')
            args=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--console','--noupx',
                '--name','VibeJobRadar','--paths',str(ROOT/'src'),'--collect-data','vibe_job_radar',
                '--collect-submodules','vibe_job_radar']
            for name in ('playwright','packaging','cryptography','cffi','pycparser'):
                args.extend(['--collect-all',name,'--copy-metadata',name])
            args.extend(['--collect-all','quickjs','--hidden-import','_quickjs','--copy-metadata','quickjs-ng',
                '--collect-all','spnego','--copy-metadata','pyspnego','--hidden-import','_cffi_backend',
                '--distpath',str(stage/'dist'),'--workpath',str(stage/'build'),'--specpath',str(stage),
                str(ROOT/'scripts/portable_entry.py')])
            subprocess.run(args,cwd=ROOT,check=True,timeout=600,
                env={**os.environ,'PYTHONPATH':str(ROOT/'src')})
            bundle=stage/'dist/VibeJobRadar'
            executable=bundle/'VibeJobRadar'
            require(executable.is_file() and os.access(executable,os.X_OK),'Native executable not built')
            relocated=bundle/'browsers'
            require(not relocated.exists() and not relocated.is_symlink(), 'Browser destination exists')
            # Preserve the vendor's complete app/framework layout and signatures.
            # PyInstaller processes Python/driver binaries, never Chromium itself.
            shutil.copytree(browsers,relocated,symlinks=True)
            require((relocated/chromium_relative).is_file(),'Relocated Chromium missing')
            for item in (ROOT/'src/vibe_job_radar').iterdir():
                if item.is_file() and item.suffix in {'.html','.js','.json'}:
                    packaged=bundle/'_internal/vibe_job_radar'/item.name
                    require(packaged.is_file() and file_hash(packaged)==file_hash(item),'Bundled page resource differs')
            bridge=Path('vibe_job_radar/guided/cdp_bridge.js')
            require(file_hash(bundle/'_internal'/bridge)==file_hash(ROOT/'src'/bridge),'Native bridge differs')
            collect_licenses(bundle)
            (bundle/'START_HERE.txt').write_text(
                'Vibe Job Radar '+target+'\n\n'
                '完整解压后，在该目录运行 ./VibeJobRadar；也可传 --workspace 选择原工作区。\n'
                '自带 Python、Playwright、Chromium、PAC 和 NTLMv2 组件。不要只复制主程序。\n'
                'Linux 需要兼容的系统浏览器库；macOS 包没有发布者签名或公证。\n'
                '首次操作与干净个人设备仍须实际验收，不用 CI 代替。\n'
                '升级/回退先停止任务和所有程序，备份整个工作区；把完整新包放到另一个目录。\n'
                '本平台不支持 Windows 自动替换、DPAPI 或 Windows 登录启动。\n'
                '原 Windows 发行和 Python 安装包继续保留，详见随发行提供的说明。\n',encoding='utf-8')
            stamp=source_stamp(ROOT)
            require(stamp['raw_sha256']==source['sha256'],'Source changed before metadata')
            stamp.update(kind='portable_build_manifest',executable_sha256=file_hash(executable))
            metadata=dict(schema_version=1,status='posix_portable_candidate',version=version,platform=target,
                source=source,runtime_source=stamp,python_version=platform.python_version(),components=observed,
                python_required=False,browser_bundled=True,bundled_browser_directory='browsers',
                signed=False,notarized=False,live_sites_certified=False,clean_machine_certified=False,
                automatic_update=False,os_credential_store=False)
            atomic_json(bundle/'PORTABLE.json',metadata)
            archive=stage/'candidate.tar.gz'
            expected=pack(bundle,archive)
            extracted=extract_verified(archive,stage/'extracted program with spaces',expected)
            proof=out/'runtime-verification.json'
            subprocess.run([sys.executable,str(ROOT/'scripts/verify_posix_portable.py'),
                            '--bundle',str(extracted),'--out',str(proof)],cwd=ROOT,check=True,timeout=240)
            verification=json.loads(proof.read_text(encoding='utf-8'))
            require(verification['success'] and verification['source_revision']==stamp['revision']
                    and verification['source_sha256']==source['sha256'] and len(verification['checks'])==8,
                    'Native runtime evidence incomplete')
            require(fingerprint(ROOT)==source,'Source changed during native build')
            sha=file_hash(archive)
            name=f'vibe-job-radar-{version}-{sha[:16]}-{target}.tar.gz'
            with (out/name).open('xb') as destination,archive.open('rb') as stream:
                shutil.copyfileobj(stream,destination,1024*1024)
            require(file_hash(out/name)==sha,'Published candidate copy changed')
            result=dict(success=True,status='posix_portable_candidate',version=version,platform=target,
                source_revision=stamp['revision'],source_sha256=source['sha256'],
                file=name,sha256=sha,size_bytes=(out/name).stat().st_size,
                runtime_evidence_sha256=file_hash(proof),runtime_checks=8,python_required=False,
                browser_bundled=True,signed=False,notarized=False,live_sites_certified=False,
                clean_machine_certified=False,automatic_update=False,os_credential_store=False,
                files=expected,build_os=platform.platform())
    except Exception as error:
        result['error_type']=type(error).__name__
        raise
    finally:
        atomic_json(out/'manifest.json',result)
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    args=parser.parse_args()
    result=build(args.out)
    print(json.dumps({key:result[key] for key in ('success','platform','file','sha256','runtime_checks')}))
