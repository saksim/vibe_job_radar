"""Explicit stable-release checking and verified, side-by-side Windows staging.

Manual preparation never executes programs. Optional consented Windows handoff
uses the separate installer and preserves prior programs and workspace backups.
GitHub HTTPS/digests identify repository content, not a publisher signature.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import zipfile
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin, urlsplit

from ._version import __version__
from .network import SafeHTTP, PinnedHTTPSConnection, validate_public_url, USER_AGENT, FetchError
from .workspace import InputError
from .workspace_proxy_auth import MESSAGES as WORKSPACE_PROXY_AUTH_MESSAGES

REPOSITORY = 'saksim/vibe_job_radar'
API = 'https://api.github.com/repos/'+REPOSITORY+'/releases/latest'
WEB = 'https://github.com/'+REPOSITORY
HOSTS = {'api.github.com','github.com','release-assets.githubusercontent.com','objects.githubusercontent.com'}
VERSION = re.compile(r'(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})')
HEX = re.compile('[0-9a-f]{64}')
SMALL_ASSETS = {'manifest.json','provenance.json','portable-verification.json','source-verification.json',
                'SHA256SUMS.txt','CHANGES.md','UPGRADE_AND_LIMITATIONS.md'}
WORKFLOWS = {'.github/workflows/'+name for name in ('tests.yml','browser.yml','browser-choice.yml',
             'browser-health.yml','native-browser.yml','vm-proxy.yml','windows-native-tls.yml','windows-portable.yml')}
MAX_ZIP = 1024**3
MAX_EXPANDED = 3*1024**3
UPDATE_NETWORK_MESSAGES = {
    **WORKSPACE_PROXY_AUTH_MESSAGES,
    'non_public_address': '升级域名未解析为允许的公网地址，可能涉及VPN映射或私网DNS。请在“网络自动适配”核对DNS设置并阅读修复模式说明；未连接该目标，也未自动改换网络。',
    'dns_error': '升级域名的系统DNS解析失败，请检查当前网络或在“网络自动适配”核对DNS设置后重试。',
    'encrypted_dns_unavailable': '加密DNS服务连接失败，请核对当前网络后重新检查。',
    'encrypted_dns_expired_answer': '加密DNS解析结果已过期，请稍后重新检查。',
    'pac_failed': 'PAC脚本进程执行或清理失败，请检查网络设置后重试。',
    'pac_timeout': 'PAC脚本选路超时，请检查脚本和当前网络后重试。',
    'pac_dns_failed': 'PAC所用系统DNS暂不可用，请核对当前网络后重试。',
    'pac_dns_limit': 'PAC脚本超过单次DNS名称上限，请检查脚本后重试。',
}


class UpdateError(ValueError):
    pass


class DownloadInterrupted(UpdateError):
    """A bounded transfer may be resumed only after a new explicit check."""


def require(condition):
    if not condition:raise UpdateError('版本或文件核验失败；已有程序和工作区未替换。')


def version(value):
    require(isinstance(value,str) and VERSION.fullmatch(value))
    return tuple(int(x) for x in value.split('.'))


def digest(data):return hashlib.sha256(data).hexdigest()


def check_stop(cancelled):
    if cancelled.is_set():raise UpdateError('升级准备已停止；已有程序和工作区保留。')


def safe_url(url):
    parsed=urlsplit(url)
    require(parsed.scheme=='https' and parsed.hostname in HOSTS and parsed.port in (None,443)
            and not parsed.username and not parsed.password and not parsed.fragment and len(url)<=8192)
    return url


class ReleaseTransport:
    def __init__(self, policy):
        self.policy=policy
        self.client=SafeHTTP(HOSTS,timeout=10,max_bytes=1_000_000,interval=0,network_policy=policy)

    def latest(self):return self.client.json(API)

    def release(self, tag):
        require(re.fullmatch(r"desktop-v[0-9]+[.][0-9]+[.][0-9]+",tag))
        return self.client.json(API.rsplit("/",1)[0]+"/tags/"+tag)

    def small(self, asset, cancelled):
        current=asset['browser_download_url']
        for attempt in range(4):
            check_stop(cancelled);safe_url(current)
            response=self.client.public_get(current)
            if response.status in (301,302,303,307,308):
                current=urljoin(current,response.headers.get('location',''))
                continue
            require(response.status==200)
            return response.body
        raise UpdateError('下载重定向次数超限，未继续请求。')

    def stream(self, asset, destination, cancelled, progress):
        current=asset['browser_download_url'];started=time.monotonic()
        offset=destination.tell()
        require(type(offset) is int and 0<=offset<asset['size'])
        for attempt in range(4):
            check_stop(cancelled);safe_url(current)
            policy=self.policy.for_request(current)
            host,addresses,target=validate_public_url(current,HOSTS,all_addresses=True,
                                                     network_policy=policy,resolver=policy.resolver)
            conn=PinnedHTTPSConnection(host,addresses,10,network_policy=policy)
            try:
                headers={'User-Agent':USER_AGENT,'Accept-Encoding':'identity'}
                if offset:headers['Range']=f'bytes={offset}-'
                conn.request('GET',target,headers=headers)
                response=conn.getresponse()
                if response.status in (301,302,303,307,308):
                    current=urljoin(current,response.getheader('Location') or '')
                    continue
                require(response.getheader('Content-Encoding','identity').lower()=='identity'
                        and not response.getheader('Content-Type','').lower().startswith('multipart/'))
                if response.status==206:
                    require(offset>0 and response.getheader('Content-Range')==f"bytes {offset}-{asset['size']-1}/{asset['size']}")
                else:
                    require(response.status==200 and response.getheader('Content-Range') is None)
                length=response.getheader('Content-Length')
                expected=asset['size']-offset if response.status==206 else asset['size']
                require(length is None or length==str(expected))
                if offset and response.status==200:
                    # Range may be ignored. Consume this same full response from
                    # zero, never append it or issue an automatic second request.
                    destination.seek(0);destination.truncate();offset=0
                received=offset;progress(received)
                while True:
                    check_stop(cancelled)
                    if time.monotonic()-started>900:raise DownloadInterrupted('本次下载已到15分钟期限。')
                    block=response.read(min(262144,asset['size']-received+1))
                    if not block:break
                    received+=len(block)
                    require(received<=asset['size'])
                    destination.write(block);progress(received)
                if received!=asset['size']:raise DownloadInterrupted('下载连接在完整接收前结束。')
                return
            finally:conn.close()
        raise UpdateError('下载重定向次数超限，未继续请求。')


def inspect_release(release, transport, cancelled, *, metadata_cache=None):
    require(isinstance(release,dict) and release.get('draft') is False and release.get('prerelease') is False
            and type(release.get('id')) is int and release['id']>0)
    tag=release.get('tag_name','')
    require(isinstance(tag,str) and tag.startswith('v'))
    selected=tag[1:];version(selected)
    require(release.get('html_url')==WEB+'/releases/tag/'+tag)
    assets=release.get('assets')
    require(isinstance(assets,list) and len(assets)==8)
    by_name={}
    for asset in assets:
        require(isinstance(asset,dict) and isinstance(asset.get('name'),str))
        name=asset['name']
        require(name not in by_name and '/' not in name and '\\' not in name
                and asset.get('state')=='uploaded' and type(asset.get('size')) is int
                and 0<asset['size']<=MAX_ZIP and type(asset.get('id')) is int and asset['id']>0
                and asset.get('browser_download_url')==WEB+'/releases/download/'+tag+'/'+name
                and isinstance(asset.get('digest'),str) and re.fullmatch('sha256:[0-9a-f]{64}',asset['digest']))
        by_name[name]=asset
    packages=set(by_name)-SMALL_ASSETS
    require(SMALL_ASSETS<=set(by_name) and len(packages)==1)
    package=by_name[packages.pop()]
    require(re.fullmatch('vibe-job-radar-'+re.escape(selected)+r'-[0-9a-f]{16}-windows-x64\.zip',package['name']))
    data={}
    for name in sorted(SMALL_ASSETS):
        check_stop(cancelled)
        asset=by_name[name];require(asset['size']<=1_000_000)
        # Latest is always fetched first. Cache only immutable public bytes whose
        # digest and length still match this fresh official inventory. A partial
        # failure must not turn cached metadata into a qualified release.
        key=(asset['digest'],asset['size'])
        raw=metadata_cache.pop(key,None) if metadata_cache is not None else None
        if not (isinstance(raw,bytes) and len(raw)==asset['size'] and 'sha256:'+digest(raw)==asset['digest']):
            raw=transport.small(asset,cancelled)
        require(isinstance(raw,bytes) and len(raw)==asset['size'] and 'sha256:'+digest(raw)==asset['digest'])
        if metadata_cache is not None:
            metadata_cache[key]=raw
            while len(metadata_cache)>len(SMALL_ASSETS):metadata_cache.popitem(last=False)
        data[name]=raw
    sums={}
    for line in data['SHA256SUMS.txt'].decode('utf-8').splitlines():
        match=re.fullmatch(r'([0-9a-f]{64})  ([^/\\]+)',line)
        require(match is not None and match[2] not in sums)
        sums[match[2]]=match[1]
    require(set(sums)==set(by_name)-{'SHA256SUMS.txt'})
    require(all(by_name[name]['digest']=='sha256:'+sha for name,sha in sums.items()))
    manifest=json.loads(data['manifest.json']);provenance=json.loads(data['provenance.json'])
    source=json.loads(data['source-verification.json']);portable=json.loads(data['portable-verification.json'])
    require(manifest.get('file')==package['name'] and manifest.get('sha256')==sums[package['name']]
            and manifest.get('size_bytes')==package['size'] and manifest.get('runtime_verified') is True
            and provenance.get('repository')==REPOSITORY and provenance.get('tag')==tag
            and provenance.get('release_channel')=='stable' and provenance.get('signed') is False
            and provenance.get('rebuilt_for_release') is False
            and provenance.get('candidate_sha256')==manifest['sha256']
            and provenance.get('change_notes_sha256')==sums['CHANGES.md']
            and re.fullmatch('[0-9a-f]{40}',str(provenance.get('source_revision','')))
            and isinstance(provenance.get('ci'),dict) and set(provenance['ci'])==WORKFLOWS
            and all(isinstance(r,dict) and type(r.get('id')) is int and r['id']>0
                    and type(r.get('attempt')) is int and r['attempt']>0 for r in provenance['ci'].values())
            and isinstance(manifest.get('source_sha256'),str) and HEX.fullmatch(manifest['source_sha256'])
            and source.get('success') is True and source.get('source_unchanged') is True
            and portable.get('success') is True and source.get('source',{}).get('sha256')==manifest.get('source_sha256')
            and manifest.get('source_sha256')==provenance.get('source_sha256'))
    require(package['name']==f"vibe-job-radar-{selected}-{manifest['sha256'][:16]}-windows-x64.zip")
    notes=data['CHANGES.md'].decode('utf-8')
    for heading in ('解决了什么','提高了什么','验证结果','剩余问题'):require('## '+heading in notes)
    return {'release_id':release['id'],'version':selected,'tag':tag,'release_url':release['html_url'],
            'package':package,'sha256':manifest['sha256'],'revision':provenance['source_revision'],
            'source_sha256':provenance['source_sha256'],'notes':notes[:30000],'signed':False}


def io_path(path):
    """Use native Windows long paths for I/O; validate logical paths first."""
    if os.name != 'nt':return path
    # abspath normalizes dot components without following a final symlink.
    name=os.path.abspath(path)
    if name.startswith('\\\\?\\'):return Path(name)
    if name.startswith('\\\\'):return Path('\\\\?\\UNC\\'+name[2:])
    return Path('\\\\?\\'+name)


def file_digest(path):
    path=io_path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size<=MAX_ZIP)
    result=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):result.update(block)
    return result.hexdigest()


def remove_owned_tree(path, identity, parent):
    # Never recursively remove a replacement, junction or path outside updates.
    if path.is_symlink() or path.resolve().parent!=parent.resolve():return
    try:
        current=path.stat()
        if (current.st_dev,current.st_ino)==identity:shutil.rmtree(io_path(path))
    except FileNotFoundError:pass


def stage_package(package, info, updates, cancelled):
    if info.get("platform") in {"linux-x64","macos-arm64"}:
        from .release_posix import stage_package as stage_native
        return stage_native(package,info,updates,cancelled)
    require(package.parent.resolve()==updates.resolve() and not package.is_symlink())
    created=None
    with io_path(package).open('rb') as stream:
        # Hash and extract the same open file, not a path that could be replaced.
        hasher=hashlib.sha256()
        for block in iter(lambda:stream.read(1024*1024),b''):
            check_stop(cancelled);hasher.update(block)
        require(hasher.hexdigest()==info['sha256']);stream.seek(0)
        with zipfile.ZipFile(stream) as archive:
            members=archive.infolist();require(0<len(members)<=20000)
            seen=set();total=0;files=set()
            for member in members:
                name=member.filename;parts=PurePosixPath(name).parts
                require(name and not name.startswith('/') and '\\' not in name and ':' not in name
                        and parts and parts[0]=='VibeJobRadar' and all(p not in {'.','..',''} for p in parts)
                        and all(p==p.rstrip(' .') and not re.fullmatch(r'(?i:CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])',p.split('.')[0]) for p in parts)
                        and not member.flag_bits&1 and member.compress_type in (zipfile.ZIP_STORED,zipfile.ZIP_DEFLATED)
                        and stat.S_IFMT(member.external_attr>>16) in (0,stat.S_IFREG,stat.S_IFDIR))
                normalized='/'.join(parts).casefold();require(normalized not in seen);seen.add(normalized)
                total+=member.file_size
                require(0<=member.file_size<=MAX_ZIP and total<=MAX_EXPANDED)
                if not member.is_dir():files.add(name)
            require({'VibeJobRadar/VibeJobRadar.exe','VibeJobRadar/PORTABLE.json'}<=files)
            meta_info=archive.getinfo('VibeJobRadar/PORTABLE.json');require(meta_info.file_size<=100000)
            meta=json.loads(archive.read(meta_info))
            require(meta.get('version')==info['version'] and meta.get('platform')=='windows-x64'
                    and meta.get('runtime_source',{}).get('revision')==info['revision']
                    and meta.get('source',{}).get('sha256')==info['source_sha256'])
            staged=Path(tempfile.mkdtemp(prefix='v'+info['version']+'-',dir=io_path(updates)))
            created=updates/staged.name
            st=staged.stat();identity=(st.st_dev,st.st_ino)
            try:
                for member in members:
                    check_stop(cancelled)
                    destination=created.joinpath(*PurePosixPath(member.filename).parts)
                    require(destination.resolve().is_relative_to(created.resolve()))
                    destination=io_path(destination)
                    if member.is_dir():destination.mkdir(parents=True,exist_ok=True,mode=0o700);continue
                    destination.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                    with archive.open(member) as source, destination.open('xb') as out:
                        remaining=member.file_size
                        while True:
                            check_stop(cancelled);block=source.read(min(262144,remaining+1))
                            if not block:break
                            remaining-=len(block);require(remaining>=0);out.write(block)
                        require(remaining==0)
                        out.flush();os.fsync(out.fileno())
                (io_path(created)/'READY.json').write_text(json.dumps({'version':info['version'],'sha256':info['sha256'],
                    'revision':info['revision'],'signed':False,'workspace_migrated':False}),encoding='utf-8')
                return created/'VibeJobRadar'
            except BaseException:
                remove_owned_tree(created,identity,updates)
                raise


class ReleaseUpdates:
    def __init__(self, workspace, *, transport_factory=None, current_version=__version__, installer=None, target_platform=None):
        from .release_platform import current_platform
        self.platform=target_platform or current_platform()
        self.workspace=workspace;self.current_version=current_version;self.installer=installer
        self.factory=transport_factory or (lambda:ReleaseTransport(workspace.network_policy()))
        self._lock=threading.Lock();self._thread=None;self._stop=threading.Event();self._selected=None
        self._metadata_cache=OrderedDict()  # At most seven <=1MB blobs; no disk or cross-instance sharing.
        self._state={'status':'idle','current_version':current_version,'message':'检查前不会联网；只读取官方仓库稳定版本。'}

    def state(self):
        with self._lock:
            result=copy.deepcopy(self._state)
        from .release_install import last_install
        result['last_install']=last_install(self.workspace.root)
        result['automatic_supported']=self.installer is not None
        from .release_install import consent_for
        result['install_consent_version']=consent_for(self.platform)
        result['platform']=self.platform
        return result

    def _save(self,**value):
        with self._lock:self._state.update(value)

    def _start(self, fn):
        with self._lock:
            if self._stop.is_set():raise InputError('工作台正在退出。')
            if self._thread and self._thread.is_alive():raise InputError('版本操作正在进行，请等待完成。')
            self._state={'status':'running','current_version':self.current_version,'message':'正在核对官方版本及文件。'}
            self._thread=threading.Thread(target=self._run,args=(fn,),name='radar-release-update',daemon=True)
            self._thread.start()
            return dict(self._state)

    def check(self,data):
        if not isinstance(data,dict) or set(data)!={'consent'} or data['consent'] is not True:
            raise InputError('请明确确认连接GitHub检查稳定版本。')
        return self._start(self._check)

    def _check(self):
        self._selected=None
        transport=self.factory()
        if self.platform not in {"windows-x64","linux-x64","macos-arm64"}:
            raise InputError("当前系统或架构尚无已验证的完整程序包，请查看发行说明；未下载其他平台文件。")
        if self.platform=="windows-x64":
            selected=inspect_release(transport.latest(),transport,self._stop,metadata_cache=self._metadata_cache)
        else:
            from .release_posix import inspect_release as inspect_native
            selected=inspect_native(transport.latest(),transport,self._stop,self.platform,metadata_cache=self._metadata_cache)
        self._selected=selected
        newer=version(selected['version'])>version(self.current_version)
        self._save(status='available' if newer else 'current',version=selected['version'],
                   release_id=selected['release_id'],release_url=selected['release_url'],notes=selected['notes'],sha256=selected['sha256'],
                   size_bytes=selected['package']['size'],signed=False,
                   message='发现较新的稳定版本，可下载并准备到独立目录。' if newer else '当前版本不低于最新稳定版。')

    def download(self,data):
        with self._lock:
            selected=copy.deepcopy(self._selected)
            valid=(self._state.get('status')=='available' and selected is not None
                   and isinstance(data,dict) and data.get('consent') is True and type(data.get('release_id')) is int
                   and data=={'consent':True,'release_id':selected['release_id'],'sha256':selected['sha256']})
        if not valid:raise InputError('请先检查并确认当前显示的稳定版本；旧页面不能更换下载目标。')
        return self._start(lambda:self._download(selected))

    def _download(self,selected,*,prepare=True):
        self._save(message='正在下载、校验并解压新版；旧程序和工作区保留。')
        updates=self.workspace.root/'updates'
        require(not updates.is_symlink() and updates.resolve().parent==self.workspace.root.resolve())
        io_path(updates).mkdir(exist_ok=True,mode=0o700)
        package=updates/selected['package']['name']
        if io_path(package).exists():require(file_digest(package)==selected['sha256'])
        else:
            from .release_download import download_package
            download_package(selected,updates,self.factory,self._stop,self._save)
        if not prepare:return package
        directory=stage_package(package,selected,updates,self._stop)
        from .release_platform import executable_name
        name=executable_name(selected)
        self._save(status='ready',release_id=selected['release_id'],version=selected['version'],release_url=selected['release_url'],
                   directory=str(directory),archive=str(package),sha256=selected['sha256'],signed=False,
                   message=f'已校验并解压到独立目录。停止任务和计划、关闭旧程序并备份工作区后，启动新目录的{name}，继续选择原工作区。旧程序和数据保留。')

    def install(self,data):
        from .release_install import consent_for
        CONSENT=consent_for(self.platform)
        with self._lock:
            selected=copy.deepcopy(self._selected)
            valid=(self._state.get('status') in {'available','ready'} and selected is not None
                   and type(data) is dict and data=={'consent':True,'consent_version':CONSENT,
                       'release_id':selected['release_id'],'sha256':selected['sha256']}
                   and data.get('consent') is True and type(data.get('release_id')) is int)
        if not valid:raise InputError('请核对当前稳定版，并明确同意关闭其他实例、备份和自动重启。')
        if self.installer is None:raise InputError('自动切换适用于受支持的完整程序包；源码版可下载并手动准备。')
        if self.platform!='windows-x64' and selected.get('automatic_update') is not True:
            raise InputError('此原生发行没有自动交接证明，请使用手动准备。')
        self.installer.preflight()
        return self._start(lambda:self._install(selected))

    def _install(self,selected):
        package=self._download(selected,prepare=False)
        self._save(message='正在复核新版并准备自动重启；备份完成前不会修改原数据。')
        result=self.installer.install(package,selected,cancelled=self._stop)
        self._save(**result,version=selected['version'],release_url=selected['release_url'])

    def _run(self,fn):
        try:fn()
        except Exception as error:
            message=str(error) if isinstance(error,(UpdateError,InputError)) else '版本操作未完成，请检查网络、磁盘和官方发行状态；已有程序和工作区保留。'
            code=error.code if isinstance(error,FetchError) and isinstance(error.code,str) and error.code in UPDATE_NETWORK_MESSAGES else None
            if code is not None:
                message=UPDATE_NETWORK_MESSAGES[code]+' 本次会话已校验的小文件会保留；原程序和工作区保留。'
            if self._state.get('retained_bytes'):
                message+=' 已保留未完成下载；请重新检查稳定版，再确认继续下载。完整校验前不会解压或安装。'
            self._save(status='failed',message=message,error_code=code)

    def close(self):
        self._stop.set()
        if self._thread:self._thread.join(timeout=12)
