"""Fair in-process admission to short cross-process metadata locks."""
from collections import deque
from contextlib import contextmanager,ExitStack
import os
import threading
import time
from weakref import WeakValueDictionary

from .collection import writer_lock
from .workspace import InputError


class _Gate:
    def __init__(self):self.condition=threading.Condition();self.tickets=deque()

    @contextmanager
    def turn(self,timeout):
        ticket=object();deadline=time.monotonic()+timeout
        with self.condition:
            self.tickets.append(ticket);self.condition.notify_all()
        try:
            with self.condition:
                while self.tickets[0] is not ticket:
                    remaining=deadline-time.monotonic()
                    if remaining<=0:raise InputError('公开任务状态正在更新，请稍后重试。')
                    self.condition.wait(remaining)
            yield
        finally:
            with self.condition:
                self.tickets.remove(ticket);self.condition.notify_all()


_gates=WeakValueDictionary()
_guard=threading.Lock()


def _gate(root):
    key=os.path.normcase(str(root.resolve()))
    with _guard:
        gate=_gates.get(key)
        if gate is None:gate=_Gate();_gates[key]=gate
        return gate


@contextmanager
def record_lock(root,*,timeout=5):
    if root.is_symlink():raise InputError('公开任务记录不能使用符号链接。')
    deadline=time.monotonic()+timeout
    # FIFO applies only to this process. The original OS lock still decides
    # cross-process exclusion and releases when a process exits. A returning
    # writer queues behind any already-waiting local observer.
    with _gate(root).turn(timeout),ExitStack() as stack:
        while True:
            try:stack.enter_context(writer_lock(root));break
            except InputError as exc:
                if not isinstance(exc.__cause__,OSError) or time.monotonic()>=deadline:raise
                time.sleep(min(.01,max(0,deadline-time.monotonic())))
        yield
