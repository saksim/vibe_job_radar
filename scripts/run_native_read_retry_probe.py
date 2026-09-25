"""CI-only passive evidence for a native retry fixture stuck in opening.

Keep the original 45-second assertion and production timeouts/ownership. Store
fixed method names, existing bounded counters and thread stacks without locals.
"""
from __future__ import annotations

from collections import Counter, deque
import faulthandler
from functools import wraps
import os
import sys
import threading
import time
from unittest.mock import patch

import run_native_read_retry as acceptance
import run_native_auth_probe as observations


class Stages:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.calls = Counter()
        self.pending = {}
        self.history = deque(maxlen=128)
        self.next_id = 0
        self.started = time.monotonic()
        self.result = {'success':False, 'scope':'Ephemeral CI artificial native retry fixture; passive method names/counters and stacks without local values. Original waits and traffic decisions unchanged.'}

    def backend(self):
        with self.lock:
            self.next_id += 1
            self.pending[self.next_id] = []
            return self.next_id

    def record(self, ident, name, phase):
        with self.lock:
            if phase == 'enter':
                self.calls[name] += 1
                self.pending[ident].append(name)
            elif self.pending[ident] and self.pending[ident][-1] == name:
                self.pending[ident].pop()
            self.history.append({'backend':ident, 'method':name, 'phase':phase,
                'elapsed_seconds':round(time.monotonic()-self.started, 3)})
            self.save()

    def save(self):
        # Use the existing atomic writer. Never serialize a backend, traceback
        # object, URL, method arguments, exception text or browser credential.
        from vibe_job_radar.utils import atomic_json
        with self.lock:
            atomic_json(self.path, {**self.result, 'calls':dict(self.calls),
                'pending':dict(self.pending), 'history':list(self.history),
                'native_counters':[{
                    name:dict(probe.get(name, {}))
                    for name in ('sent','acknowledged','events','attachments','auth')
                } for probe in tuple(observations.PROBES)]})


def observed_backend(stages):
    class Backend(observations.ObservedBackend):
        def __init__(self, *args, **kwargs):
            self.stage_id = stages.backend()
            stages.record(self.stage_id, 'initialize', 'enter')
            try:
                super().__init__(*args, **kwargs)
            finally:
                stages.record(self.stage_id, 'initialize', 'leave')

    def observe(name, function):
        @wraps(function)
        def recorded(self, *args, **kwargs):
            stages.record(self.stage_id, name, 'enter')
            try:
                return function(self, *args, **kwargs)
            finally:
                stages.record(self.stage_id, name, 'leave')
        return recorded

    for name in ('_configure_context', '_new_page', '_load_robots', 'open', 'snapshot', 'close'):
        setattr(Backend, name, observe(name, getattr(Backend, name)))
    return Backend


def error_chain(error):
    names = []; seen = set()
    while error is not None and id(error) not in seen and len(names) < 8:
        seen.add(id(error))
        names.append(type(error).__name__)
        error = error.__cause__ or error.__context__
    return names


def main():
    if '--controlled' not in sys.argv or os.environ.get('CI') != 'true' or os.environ.get('GITHUB_ACTIONS') != 'true':
        raise SystemExit('Native retry diagnostics require an explicitly enabled ephemeral GitHub runner.')
    out=acceptance.ROOT/'browser-acceptance'/'native-read-retry';out.mkdir(parents=True,exist_ok=True)
    stages=Stages(out/'progress.json');stages.save()
    with (out/'threads.log').open('w',encoding='utf-8') as stacks:
        faulthandler.dump_traceback_later(35,file=stacks)
        try:
            with patch.object(acceptance,'NativeBackend',observed_backend(stages)):
                acceptance.main()
            stages.result['success']=True
        except BaseException as error:
            stages.result['error_types']=error_chain(error)
            faulthandler.dump_traceback(file=stacks,all_threads=True)
            raise
        finally:
            faulthandler.cancel_dump_traceback_later()
            stages.save()


if __name__=='__main__':main()
