"""Consented Windows portable handoff, data backup and durable launch selection.

Never overwrites an executable or restores user data automatically. A verified
new directory becomes current only after its own readiness acknowledgement.
"""
from __future__ import annotations
from contextlib import ExitStack, closing
import ctypes
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import stat
import subprocess
import sys
import threading
import time
import zipfile

from ._version import __version__
from .collection import writer_lock
from .release_updates import (UpdateError, require, version, io_path, file_digest,
                              stage_package, remove_owned_tree, check_stop)
from .runtime import is_portable
from .publisher_signing import verify_bundle, evidence as signing_evidence, is_signed
from .release_platform import current_platform, executable_name, POSIX_PLATFORMS
from .utils import atomic_json
from .workspace import InputError

CONSENT = 'windows-verified-upgrade-restart-v1'
POSIX_CONSENT = 'native-verified-upgrade-restart-v1'


def consent_for(platform):
    return POSIX_CONSENT if platform in POSIX_PLATFORMS else CONSENT
MAX_BACKUP = 5 * 1024**3
MAX_FILES = 50000
OWNER_DIRS = ('', 'collections', 'collections/owner', 'guided/owner',
              'public_tasks/owner', 'public_schedule/owner', 'public_queue/owner',
              'report_tasks/owner')


def supported():
    return is_portable() and current_platform() in {'windows-x64',*POSIX_PLATFORMS}


def plain(path, *, directory=False):
    path = io_path(path)
    info = path.lstat()
    require(not (info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)
            if hasattr(info, 'st_file_attributes') else not stat.S_ISLNK(info.st_mode))
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    return info


def read_json(path, limit=100000):
    require(plain(path).st_size <= limit)
    def unique(pairs):
        result = {}
        for key,value in pairs:
            require(key not in result); result[key] = value
        return result
    return json.loads(io_path(path).read_text(encoding='utf-8'), object_pairs_hook=unique)


def write_json(path, value):
    if io_path(path).exists():plain(path)
    atomic_json(io_path(path), value)


def safe_child(root, relative, *, directory=True):
    root = root.resolve()
    path = root / relative
    require(path.resolve().is_relative_to(root) and path.resolve() != root)
    cursor = root
    plain(cursor, directory=True)
    for part in path.relative_to(root).parts:
        cursor = cursor / part
        if io_path(cursor).exists():plain(cursor, directory=directory or cursor != path)
    return path


def inventory(root, *, exclude_updates=False):
    root = root.resolve(); plain(root, directory=True)
    result = {}; total = 0
    def walk(directory, relative):
        nonlocal total
        for item in io_path(directory).iterdir():
            name = item.name
            if exclude_updates and not relative and name == 'updates':continue
            target = directory / name
            rel = (relative + '/' + name).lstrip('/')
            locks={(part+'/.writer.lock').lstrip('/') for part in OWNER_DIRS}
            if exclude_updates and rel in locks:continue  # OS lock markers carry no user records.
            st = item.lstat()
            if stat.S_ISDIR(st.st_mode):
                plain(target, directory=True); walk(target, rel)
            else:
                plain(target)
                total += st.st_size
                require(len(result) < MAX_FILES and total <= MAX_BACKUP)
                result[rel] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    walk(root, '')
    return result


def backup_workspace(workspace, updates):
    """Copy a quiescent workspace, including consistent SQLite/WAL content."""
    before = inventory(workspace, exclude_updates=True)
    require(shutil.disk_usage(updates).free > sum(v[2] for v in before.values()) + 64*1024**2)
    backup = safe_child(updates, 'backup-' + secrets.token_hex(12))
    io_path(backup).mkdir(mode=0o700)
    info = plain(backup, directory=True); identity = (info.st_dev, info.st_ino)
    databases = set();deadline=time.monotonic()+120
    def progress(*_):
        if time.monotonic()>deadline:raise UpdateError('工作区备份超过2分钟，原数据保留。')
    try:
        io_path(backup/'workspace').mkdir(mode=0o700)
        for base, dirs, _ in os.walk(io_path(workspace),followlinks=False):
            if Path(base)==io_path(workspace):dirs[:]=[name for name in dirs if name!='updates']
            relative=Path(base).relative_to(io_path(workspace))
            for name in dirs:
                plain(Path(base)/name,directory=True)
                io_path(backup/'workspace'/relative/name).mkdir(parents=True,exist_ok=True,mode=0o700)
        for relative in before:
            source = workspace / relative
            with io_path(source).open('rb') as stream:
                if stream.read(16) == b'SQLite format 3\x00':databases.add(relative)
        for relative in before:
            progress()
            # WAL is data. Copy it with its DB; SHM is a rebuildable lock index.
            if any(relative == database + '-shm' for database in databases):continue
            source = workspace / relative; destination = backup / 'workspace' / relative
            io_path(destination.parent).mkdir(parents=True, exist_ok=True, mode=0o700)
            with io_path(source).open('rb') as src, io_path(destination).open('xb') as dest:
                for block in iter(lambda:src.read(1024*1024),b''):
                    progress();dest.write(block)
                dest.flush(); os.fsync(dest.fileno())
        require(inventory(workspace, exclude_updates=True) == before)
        # Recover/checkpoint only our private clone; never write to the original.
        for relative in databases:
            progress()
            with closing(sqlite3.connect(str(io_path(backup/'workspace'/relative)),timeout=5)) as clone:
                require(clone.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] == 0)
        write_json(backup/'BACKUP.json',dict(schema_version=1,complete=True,files=len(before),
            sqlite_databases=len(databases),excluded=['updates','owned runtime lock markers'],workspace=str(workspace)))
        return backup
    except BaseException:
        remove_owned_tree(backup,identity,updates)
        raise


def verify_directory(directory, package, selected):
    """Recheck all executable/dependency bytes immediately before a handoff."""
    if selected.get("platform") in POSIX_PLATFORMS:
        from .release_posix import verify_directory as verify_native
        return verify_native(directory,package,selected,threading.Event())
    require(file_digest(package) == selected['sha256'])
    actual = inventory(directory)
    with io_path(package).open('rb') as stream, zipfile.ZipFile(stream) as archive:
        expected = {item.filename.removeprefix('VibeJobRadar/'):item for item in archive.infolist()
                    if not item.is_dir()}
        require(set(actual) == set(expected))
        for relative,item in expected.items():
            target = directory / relative
            require(plain(target).st_size == item.file_size)
            left = hashlib.sha256(); right = hashlib.sha256()
            with archive.open(item) as src, io_path(target).open('rb') as dest:
                for data in iter(lambda:src.read(1024*1024),b''):left.update(data)
                for data in iter(lambda:dest.read(1024*1024),b''):right.update(data)
            require(left.digest() == right.digest())
    verify_bundle(io_path(directory),selected)
    meta = read_json(directory/'PORTABLE.json')
    require(meta.get('version') == selected['version'] and meta.get('upgrade_protocol') == 1
            and meta.get('runtime_source',{}).get('revision') == selected['revision'])


def launch_environment():
    env = dict(os.environ)
    inherited = env.pop('VIBE_RADAR_BUNDLED_BROWSER_PATH',None)
    if inherited and env.get('PLAYWRIGHT_BROWSERS_PATH') == inherited:
        env.pop('PLAYWRIGHT_BROWSERS_PATH',None)
    # Never select a previous PyInstaller payload in a newly launched program.
    env['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    if sys.platform=='linux':
        original=env.pop('LD_LIBRARY_PATH_ORIG',None)
        if original is None:env.pop('LD_LIBRARY_PATH',None)
        else:env['LD_LIBRARY_PATH']=original
    return env


def spawn(args, **kwargs):
    if os.name!='nt':kwargs.setdefault('start_new_session',True)
    return subprocess.Popen([str(arg) for arg in args],stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,env=launch_environment(),
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),**kwargs)


class WindowsProcess:
    """Retain an OS process handle; PID reuse cannot satisfy the exit wait."""
    def __init__(self,pid,*,terminate=False):
        from ctypes import wintypes
        self.api = ctypes.WinDLL('kernel32',use_last_error=True)
        self.api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
        self.api.OpenProcess.restype=wintypes.HANDLE
        self.api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
        self.api.WaitForSingleObject.restype=wintypes.DWORD
        self.api.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
        self.api.GetProcessTimes.restype=wintypes.BOOL
        self.api.CloseHandle.argtypes=[wintypes.HANDLE]
        self.handle=self.api.OpenProcess(0x100000|0x1000|(1 if terminate else 0),False,pid)
        if not self.handle:raise OSError('Cannot retain parent process')
        values=[wintypes.FILETIME() for _ in range(4)]
        if not self.api.GetProcessTimes(self.handle,*[ctypes.byref(value) for value in values]):
            self.close();raise OSError('Cannot identify parent process')
        self.created=(values[0].dwHighDateTime<<32)|values[0].dwLowDateTime

    def exited(self, milliseconds=0):
        result=self.api.WaitForSingleObject(self.handle,milliseconds)
        if result not in (0,258):raise OSError('Cannot observe parent exit')
        return result==0

    def terminate(self):
        from ctypes import wintypes
        self.api.TerminateProcess.argtypes=[wintypes.HANDLE,wintypes.UINT]
        self.api.TerminateProcess.restype=wintypes.BOOL
        if not self.api.TerminateProcess(self.handle,0):raise OSError('Cannot stop owned process')

    def close(self):
        if self.handle:self.api.CloseHandle(self.handle);self.handle=None


class ExitPipe:
    """The old app alone retains the writer; EOF cannot be faked by PID reuse."""
    def __init__(self, fd, identity):
        require(type(fd) is int and fd>=3)
        info=os.fstat(fd)
        require(stat.S_ISFIFO(info.st_mode) and info.st_ino==identity)
        os.set_inheritable(fd,False)
        self.fd=fd;self.created=identity

    def exited(self, milliseconds=0):
        import select
        ready,_,_=select.select([self.fd],[],[],milliseconds/1000)
        if not ready:return False
        require(os.read(self.fd,1)==b'')
        return True

    def close(self):
        if self.fd is not None:os.close(self.fd);self.fd=None


def request_path(workspace, path):
    workspace=workspace.resolve(); updates=safe_child(workspace,'updates')
    path=Path(path)
    require(path.is_absolute() and path.name=='request.json' and path.parent.parent==updates
            and path.parent.name.startswith('install-') and len(path.parent.name)==32)
    safe_child(updates,path.parent.name)
    return path


def load_request(path):
    value=read_json(path)
    fields={'schema_version','workspace','directory','package','selected','parent_pid','parent_created',
            'old_executable','old_version','nonce','no_browser'}
    native=isinstance(value,dict) and value.get('schema_version')==2
    require(isinstance(value,dict) and set(value)==fields and type(value['schema_version']) is int
            and value['schema_version'] in (1,2)
            and type(value['parent_pid']) is int and value['parent_pid']>0
            and type(value['parent_created']) is int and value['parent_created']>=(0 if native else 1)
            and type(value['no_browser']) is bool and isinstance(value['nonce'],str) and len(value['nonce'])==64)
    root=Path(value['workspace']); require(root.is_absolute() and root.resolve()==root)
    request_path(root,path)
    selected=value['selected']; version(selected['version']); version(value['old_version'])
    require(isinstance(selected,dict) and set(selected)==({'version','sha256','revision','source_sha256'}
            | ({'platform','automatic_update'} if native else ({'signed','signing'} if 'signing' in selected else set()))))
    if not native and 'signing' in selected:
        require(type(selected['signed']) is bool and selected['signed']==is_signed(signing_evidence(selected['signing'])))
    if native:require(selected.get('platform') in POSIX_PLATFORMS and selected.get('automatic_update') is True)
    import re
    require(all(re.fullmatch('[0-9a-f]{64}',selected[key]) for key in ('sha256','source_sha256'))
            and re.fullmatch('[0-9a-f]{40}',selected['revision']))
    directory=Path(value['directory']); package=Path(value['package'])
    require(directory.is_absolute() and directory.parent.parent==root/'updates' and directory.name=='VibeJobRadar'
            and package.is_absolute() and package.parent==root/'updates')
    safe_child(root,directory.relative_to(root));safe_child(root,package.relative_to(root),directory=False)
    old=Path(value['old_executable']);require(old.is_absolute() and (old.name==executable_name(selected) if native else old.name.lower()=='vibejobradar.exe'))
    return value


def wait_record(path, nonce, status, *, process=None, timeout=30):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if process is not None and process.poll() is not None:
            raise UpdateError('新版或升级助手未启动；原程序、工作区和已完成备份保留。')
        if io_path(path).exists():
            record=read_json(path)
            require(record.get('nonce')==nonce)
            if record.get('status')==status:return record
            if record.get('status')=='failed':raise UpdateError('自动升级未完成；请查看升级记录并从原程序继续。')
        time.sleep(.1)
    raise UpdateError('等待新版就绪超时；保留原程序与工作区备份。')


class AutomaticInstaller:
    def __init__(self,workspace,*,shutdown,quiesce,preflight,no_browser=False):
        self.workspace=workspace;self.shutdown=shutdown;self.quiesce=quiesce;self.no_browser=no_browser
        self.preflight=preflight

    def install(self,package,selected,*,cancelled=None):
        cancelled=cancelled or threading.Event();check_stop(cancelled)
        if not supported():raise InputError('自动切换需要受支持的完整程序包。')
        native=selected.get('platform') in POSIX_PLATFORMS
        if native:require(selected['platform']==current_platform() and selected.get('automatic_update') is True)
        self.preflight()
        old=Path(sys.executable).resolve();plain(old)
        updates=safe_child(self.workspace.root,'updates')
        # Re-extract the retained, verified ZIP instead of trusting an editable preview.
        directory=stage_package(package,selected,updates,cancelled)
        verify_directory(directory,package,selected)
        check_stop(cancelled)
        process=spawn([directory/executable_name(selected),'--doctor','--workspace',updates/('probe-'+secrets.token_hex(12))])
        try:
            if process.wait(timeout=30)!=0:raise UpdateError('新版离线启动检查失败；当前工作台继续运行。')
        except subprocess.TimeoutExpired:
            process.kill();process.wait(5);raise UpdateError('新版离线启动检查超时；当前工作台继续运行。') from None
        journal=safe_child(updates,'install-'+secrets.token_hex(12));io_path(journal).mkdir(mode=0o700)
        reader=writer=None
        if native:
            reader,writer=os.pipe()
            created=os.fstat(reader).st_ino
        else:
            parent=WindowsProcess(os.getpid())
            try:created=parent.created
            finally:parent.close()
        request=dict(schema_version=2 if native else 1,workspace=str(self.workspace.root),directory=str(directory),package=str(package),
            selected={key:selected[key] for key in ('version','sha256','revision','source_sha256')},
            parent_pid=os.getpid(),parent_created=created,old_executable=str(old),old_version=__version__,
            nonce=secrets.token_hex(32),no_browser=self.no_browser)
        if native:request['selected'].update(platform=selected['platform'],automatic_update=True)
        else:request['selected'].update(signed=selected.get('signed',False),signing=signing_evidence(selected.get('signing')))
        helper=None
        try:
            write_json(journal/'request.json',request)
            check_stop(cancelled)
            latest=updates/'latest-install.json'
            if latest.exists():
                prior=read_json(latest);require(set(prior)=={'request'})
                load_request(request_path(self.workspace.root,prior['request']))
            write_json(latest,dict(request=str(journal/'request.json')))
            args=[old,'--apply-update',journal/'request.json']
            if native:
                args+=['--update-parent-fd',str(reader)]
                helper=spawn(args,pass_fds=(reader,))
            else:helper=spawn(args)
            wait_record(journal/'status.json',request['nonce'],'armed',process=helper,timeout=15)
            # Blocks new UI mutations and stops only this instance's idle dispatchers.
            check_stop(cancelled)
            self.quiesce()
        except BaseException:
            if helper is not None and helper.poll() is None:helper.terminate();helper.wait(5)
            if writer is not None:os.close(writer)
            raise
        finally:
            if reader is not None:os.close(reader)
        # An integer fd stays open until process exit, after server/workers close.
        # It is non-inheritable and is never passed to the update helper.
        self._exit_writer=writer
        threading.Timer(.5,self.shutdown).start()
        return dict(status='restarting',directory=str(directory),install_record=str(journal),
                    message='正在退出当前工作台。升级助手将备份工作区、核对新版就绪并自动打开；原程序保留。')


def apply_update(path, *, parent_fd=None):
    """Old verified runtime, explicitly invoked by its own UI after consent."""
    path=Path(path); request=load_request(path); journal=path.parent
    require(supported() and Path(sys.executable).resolve()==Path(request['old_executable'])
            and __version__==request['old_version'])
    native=request['schema_version']==2
    require((native and request['selected']['platform']==current_platform() and parent_fd is not None)
            or (not native and parent_fd is None))
    nonce=request['nonce']; status=journal/'status.json'; process=None; parent=None
    committed=False;current=None;previous_current=None;backup=None
    try:
        parent=ExitPipe(parent_fd,request['parent_created']) if native else WindowsProcess(request['parent_pid'])
        require(parent.created==request['parent_created'])
        write_json(status,dict(status='armed',nonce=nonce))
        if not parent.exited(120000):raise UpdateError('原工作台未完全退出，未切换程序。')
        parent.close();parent=None
        root=Path(request['workspace']);updates=root/'updates';directory=Path(request['directory'])
        with ExitStack() as locks:
            for relative in OWNER_DIRS:
                target=root if not relative else safe_child(root,relative)
                io_path(target).mkdir(parents=True,exist_ok=True,mode=0o700)
                locks.enter_context(writer_lock(target))
            if (updates/'current.json').exists():
                previous_current=read_json(updates/'current.json')
                validate_current(root,previous_current)
            backup=backup_workspace(root,updates)
            verify_directory(directory,Path(request['package']),request['selected'])
            write_json(status,dict(status='starting',nonce=nonce,backup=str(backup)))
            args=[directory/executable_name(request['selected']),'--workspace',root,'--upgrade-handoff',path]
            if request['no_browser']:args.append('--no-browser')
            process=spawn(args)
            ready=wait_record(journal/'ready.json',nonce,'ready',process=process,timeout=45)
            require(ready.get('version')==request['selected']['version'] and ready.get('pid')==process.pid)
            # Keep the existing OS registration. Its original portable entry
            # follows current.json only after the handoff is committed.
            current=dict(schema_version=1,request=str(path),version=request['selected']['version'],
                executable=str(directory/executable_name(request['selected'])),executable_sha256=file_digest(directory/executable_name(request['selected'])),
                old_executable=request['old_executable'],backup=str(backup),nonce=nonce)
            write_json(updates/'current.json',current)
        write_json(status,dict(status='committed',nonce=nonce,backup=str(backup),startup='existing_registration_preserved'))
        committed=True
        return 0
    except Exception as error:
        if not committed and current is not None:
            try:
                if read_json(updates/'current.json')==current:
                    if previous_current is not None:write_json(updates/'current.json',previous_current)
                    else:io_path(updates/'current.json').unlink()
            except Exception:pass
        if process is not None and not committed and process.poll() is None:
            process.terminate();process.wait(10)
        # A failed/new workspace may contain migrations: do not overwrite data
        # or blindly restart an older binary against uncertain newer data.
        try:write_json(status,dict(status='failed',nonce=nonce,error_type=type(error).__name__,
                                  message='自动切换未完成；原程序、工作区及已完成备份保留，请核对升级记录。',
                                  backup=str(backup) if backup else ''))
        except Exception:pass
        return 2
    finally:
        if parent is not None:parent.close()


def acknowledge_ready(path, workspace, port):
    request=load_request(path)
    require(Path(request['workspace'])==workspace.root and is_portable()
            and Path(sys.executable).resolve()==Path(request['directory'])/executable_name(request['selected'])
            and __version__==request['selected']['version'])
    write_json(Path(path).parent/'ready.json',dict(status='ready',nonce=request['nonce'],
               version=__version__,pid=os.getpid(),port=port))
    wait_record(Path(path).parent/'status.json',request['nonce'],'committed',timeout=45)


def installed_command(workspace, argv):
    """An old portable entry follows only a committed, verified installation."""
    root=Path(workspace).expanduser().resolve();path=root/'updates'/'current.json'
    if not (supported() and path.exists()):return None
    safe_child(root,'updates');record=read_json(path)
    validate_current(root,record)
    target=Path(record['executable'])
    if target==Path(sys.executable).resolve() or version(record['version'])<=version(__version__):return None
    request=load_request(Path(record['request']))
    status=read_json(Path(record['request']).parent/'status.json')
    if status.get('status')!='committed':return None
    require(status.get('nonce')==record.get('nonce')==request['nonce']
            and target==Path(request['directory'])/executable_name(request['selected'])
            and file_digest(target)==record['executable_sha256'])
    return [str(target),*argv]


def retained_launcher(workspace, previous, current):
    """An owned startup entry can keep forwarding without rewriting the registry."""
    try:
        record=read_json(workspace/'updates'/'current.json')
        validate_current(workspace,record)
        plain(previous)
        request=load_request(Path(record['request']))
        state=read_json(Path(record['request']).parent/'status.json')
        return (state.get('status')=='committed' and state.get('nonce')==record.get('nonce')==request['nonce']
                and Path(record['executable'])==current==Path(request['directory'])/executable_name(request['selected'])
                and read_json(previous.parent/'PORTABLE.json').get('upgrade_protocol')==1
                and file_digest(current)==record['executable_sha256'])
    except (OSError,ValueError,KeyError,TypeError):
        return False


def last_install(workspace):
    """Read only a bounded local handoff record; never starts work or networking."""
    try:
        latest=Path(workspace)/'updates'/'latest-install.json'
        if not latest.exists():return None
        pointer=read_json(latest);require(set(pointer)=={'request'})
        path=request_path(Path(workspace),pointer['request']);request=load_request(path)
        status=read_json(path.parent/'status.json')
        require(status.get('nonce')==request['nonce'])
        state=status.get('status');require(state in {'armed','starting','committed','failed'})
        return dict(status=state,version=request['selected']['version'],record=str(path.parent),
            backup=status.get('backup',''),message={
                'armed':'上次升级已准备交接；若未切换，请核对记录。',
                'starting':'上次升级已备份并尝试启动；请核对是否完成。',
                'committed':'上次自动升级已完成，新版就绪，原程序和备份保留。',
                'failed':'上次自动升级未完成；原程序和备份保留，未自动回写用户数据。'}[state])
    except (OSError,ValueError,KeyError,TypeError):
        return dict(status='unreadable',message='上次升级记录不可读；原文件保留，请核对工作区 updates。')


def validate_current(workspace,record):
    require(isinstance(record,dict) and set(record)=={'schema_version','request','version','executable',
        'executable_sha256','old_executable','backup','nonce'} and type(record['schema_version']) is int
        and record['schema_version']==1)
    request=load_request(request_path(Path(workspace),record['request']))
    require(record['version']==request['selected']['version'] and record['nonce']==request['nonce']
        and record['executable']==str(Path(request['directory'])/executable_name(request['selected']))
        and record['old_executable']==request['old_executable'])
    import re
    require(isinstance(record['executable_sha256'],str) and re.fullmatch('[0-9a-f]{64}',record['executable_sha256']))
    backup=Path(record['backup'])
    require(backup.parent==Path(workspace)/'updates' and re.fullmatch('backup-[0-9a-f]{24}',backup.name))
