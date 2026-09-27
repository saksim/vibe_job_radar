"""OS ownership for a live collector; a lock file is never a PID/liveness claim."""
from contextlib import contextmanager,ExitStack
from functools import wraps
import time

from ..collection import writer_lock
from ..workspace import InputError

MESSAGE='另一个本机工作台正在操作此工作区的采集任务或保留浏览器会话；此处只查看进度，请回到原工作台暂停或停止并关闭会话。'


class GuidedTaskBusy(InputError):
    """No operation, login submission or checkpoint mutation was accepted."""


class Ownership:
    def __init__(self,root):
        self.root=root/'owner';self.lease=None

    def _claim(self):
        if self.root.parent.is_symlink() or self.root.is_symlink():
            raise InputError('采集所有权目录不能使用符号链接。')
        self.root.mkdir(exist_ok=True,mode=0o700)
        lease=writer_lock(self.root)
        try:lease.__enter__()
        except InputError as exc:
            if isinstance(exc.__cause__,OSError):raise GuidedTaskBusy(MESSAGE) from None
            raise
        return lease

    def acquire(self):
        if self.lease is None:self.lease=self._claim()

    def release(self):
        if self.lease is not None:
            lease,self.lease=self.lease,None
            lease.__exit__(None,None,None)

    def foreign(self):
        if self.lease is not None:return False
        try:lease=self._claim()
        except GuidedTaskBusy:return True
        else:lease.__exit__(None,None,None);return False


def owned_action(method=None,*,allow_shutdown=False):
    """Claim before reads/decisions/writes; keep ownership for queued/live work."""
    if method is None:return lambda method:owned_action(method,allow_shutdown=allow_shutdown)
    @wraps(method)
    def call(service,*args,**kwargs):
        with mutation(service,allow_shutdown=allow_shutdown):return method(service,*args,**kwargs)
    return call


@contextmanager
def mutation(service,*,allow_shutdown=False):
    with service._lock:
        if service._shutdown.is_set() and (not allow_shutdown or service._ownership.lease is None):
            raise InputError('此工作台已关闭，未提交新动作。')
        if service._closure_uncertain and not allow_shutdown:
            raise InputError('无法确认上次采集浏览器已关闭；请退出原工作台进程并核对浏览器后重新启动。')
        service._ownership.acquire()
        service._owner_depth+=1
        try:yield
        finally:
            service._owner_depth-=1
            service._release_owner_if_idle()


@contextmanager
def checkpoint_lock(root):
    """Short shared reader/writer lease, including Windows metadata handles."""
    deadline=time.monotonic()+5
    with ExitStack() as stack:
        while True:
            try:stack.enter_context(writer_lock(root));break
            except InputError as exc:
                if not isinstance(exc.__cause__,OSError) or time.monotonic()>=deadline:raise
                time.sleep(.01)
        yield
