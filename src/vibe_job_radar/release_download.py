"""Persist untrusted ZIP prefixes; only a complete official digest can publish.

The name binds release, asset, size and full SHA256. Redirect URLs, credentials
and claims of partial-content authenticity are never persisted.
"""
from contextlib import contextmanager
import hashlib
import http.client
import os
import stat
import tempfile

from .collection import writer_lock
from .network import FetchError
from .loopback_proxy import LocalProxyError
from .release_updates import (DownloadInterrupted, UpdateError, check_stop,
                              file_digest, io_path, require)
from .team_store import remove_private_file
from .workspace import InputError


def partial_path(updates, selected):
    asset=selected['package']
    return updates/f".resume-{selected['release_id']}-{asset['id']}-{asset['size']}-{selected['sha256']}.partial"


def plain(path):
    info=path.lstat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink==1
            and not getattr(info,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0))
    return info


@contextmanager
def download_lock(updates):
    directory=io_path(updates).lstat()
    require(stat.S_ISDIR(directory.st_mode)
            and not getattr(directory,'st_file_attributes',0)&getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0))
    marker=io_path(updates/'.writer.lock')
    if marker.exists() or marker.is_symlink():plain(marker)
    lease=writer_lock(io_path(updates))
    try:lease.__enter__()
    except InputError:
        raise InputError('另一个工作台正在下载升级包；请等待完成后重新检查。') from None
    try:yield
    finally:lease.__exit__(None,None,None)


def open_prefix(path):
    flags=os.O_RDWR|getattr(os,'O_BINARY',0)|getattr(os,'O_NOFOLLOW',0)
    try:fd=os.open(path,flags|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        plain(path);fd=os.open(path,flags)
    try:
        info=os.fstat(fd);before=plain(path)
        require((info.st_dev,info.st_ino)==(before.st_dev,before.st_ino))
        return os.fdopen(fd,'r+b'),(info.st_dev,info.st_ino)
    except BaseException:
        os.close(fd);raise


class TransferTarget:
    """A full-response fallback cannot truncate the only persistent prefix."""
    def __init__(self, stream, path, identity):
        self.stream=stream;self.path=path;self.identity=identity;self.replacement=None

    def tell(self):return self.stream.tell()
    def write(self, block):return self.stream.write(block)

    def seek(self, offset):
        require(offset==0 and self.replacement is None)
        fd,name=tempfile.mkstemp(prefix='.full-response-',suffix='.partial',dir=self.path.parent)
        self.path=type(self.path)(name);info=os.fstat(fd);self.identity=(info.st_dev,info.st_ino)
        self.replacement=os.fdopen(fd,'w+b');self.stream=self.replacement
        return self.stream.seek(0)

    def truncate(self):
        require(self.replacement is not None)
        return self.stream.truncate()

    def close(self):
        if self.replacement is not None:
            try:self.replacement.close()
            finally:remove_private_file(self.path,self.identity)


def download_package(selected, updates, factory, cancelled, save):
    # The permanent OS lock marker is never removed. Hold it until after the
    # prefix handle closes and owned-file cleanup finishes, including failures.
    with download_lock(updates):
        path=io_path(partial_path(updates,selected));package=io_path(updates/selected['package']['name'])
        if package.exists():
            require(file_digest(package)==selected['sha256']);return
        size=selected['package']['size'];keep=False;identity=None;target=None
        try:
            out,identity=open_prefix(path)
            with out:
                offset=os.fstat(out.fileno()).st_size
                require(0<=offset<=size)
                save(downloaded_bytes=offset,total_bytes=size,resumed_bytes=offset,retained_bytes=0,
                     message='正在继续下载并核验完整软件包。' if offset else '正在下载并核验完整软件包。')
                out.seek(offset)
                target=TransferTarget(out,path,identity)
                reused=offset
                def progress(count):
                    nonlocal reused
                    reused=min(reused,count)
                    save(downloaded_bytes=count,total_bytes=size,resumed_bytes=reused)
                try:
                    if offset<size:
                        factory().stream(selected['package'],target,cancelled,progress)
                    active=target.stream
                    active.flush();os.fsync(active.fileno())
                    require(os.fstat(active.fileno()).st_size==size)
                    active.seek(0);hasher=hashlib.sha256()
                    while True:
                        check_stop(cancelled);block=active.read(1024*1024)
                        if not block:break
                        hasher.update(block)
                    require(hasher.hexdigest()==selected['sha256'])
                    check_stop(cancelled)
                    current=plain(target.path)
                    require((current.st_dev,current.st_ino)==target.identity and current.st_size==size)
                    os.link(target.path,package)  # Never overwrite a pre-existing package.
                except (OSError,http.client.HTTPException,FetchError,LocalProxyError,DownloadInterrupted):
                    # Bytes remain untrusted. The final complete hash rejects
                    # corruption, changed assets and any partial disk write.
                    out.flush();os.fsync(out.fileno());current=plain(path)
                    keep=(current.st_dev,current.st_ino)==identity and 0<current.st_size<=size
                    if keep:save(retained_bytes=current.st_size)
                    raise
                except UpdateError:
                    if cancelled.is_set() or target.replacement is not None:
                        out.flush();os.fsync(out.fileno());current=plain(path)
                        keep=(current.st_dev,current.st_ino)==identity and 0<current.st_size<=size
                        if keep:save(retained_bytes=current.st_size)
                    raise
        finally:
            try:
                if target is not None:target.close()
            finally:
                if identity is not None and not keep:remove_private_file(path,identity)
