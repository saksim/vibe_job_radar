"""Independent local worker for an already confirmed public query plan.

No HTTP server, browser, new consent, service registration or queue format.
The existing scheduler owns dispatch/recovery and PublicTasks owns acquisition.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import signal
import threading

from .local_public import LocalPublicDataClient
from .public_schedule import PublicSchedule
from .public_tasks import PublicTasks
from .workspace import Workspace


def print_status(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


class PublicWorker:
    def __init__(self, workspace, *, client=None):
        self.tasks = PublicTasks(workspace, hybrid_client=client if client is not None else LocalPublicDataClient(workspace))
        self.schedule = PublicSchedule(workspace, self.tasks)
        self.stop = threading.Event()
        self._signalled = False

    def request_stop(self):
        # Wake the scheduler's stop path before waiting for current reads.
        self.stop.set()
        self.schedule.request_stop()

    def run(self, *, emit=print_status):
        code = 0
        try:
            # Validate an existing record before claiming ownership or starting
            # a thread. A corrupt record is never replaced with a default plan.
            self.schedule.state()
            if self.stop.is_set() or self._signalled:return 0
            self.schedule.start()
            emit({'event':'worker_started', 'http_server':False, 'browser':False})
            last = None
            while not self.stop.is_set() and not self._signalled:
                state = self.schedule.state()
                value = {'event':'worker_state', 'plan_status':state['status'],
                         'code':state['code'], 'owns_schedule':state['worker_active'],
                         'retained_runs':len(state['history'])}
                if value != last:
                    # Only fixed status fields; no query, source response,
                    # credentials, environment, local URL/token or path.
                    emit(value); last = value
                if state['code']=='storage_error' or not self.schedule.is_running():
                    emit({'event':'worker_failed', 'code':'scheduler_unavailable'})
                    code = 2; break
                self.stop.wait(1)
        except (OSError, ValueError, RuntimeError) as error:
            emit({'event':'worker_failed', 'code':'local_worker_error', 'error_type':type(error).__name__})
            code = 2
        finally:
            self.request_stop()
            self.schedule.close()
            self.tasks.close()
            if self.schedule.is_running() or self.tasks.is_running():
                code = 2
                emit({'event':'worker_failed', 'code':'shutdown_incomplete'})
            else:
                emit({'event':'worker_stopped'})
        return code


def run_cli(workspace_path, *, emit=print_status):
    worker = None
    entered = False
    previous = {}
    try:
        worker = PublicWorker(Workspace(workspace_path))
        # Python handlers can interrupt code while an Event/Condition lock is
        # held. Only set a flag here; run() performs locking cleanup normally.
        def stop(signum, frame):worker._signalled = True
        for name in ('SIGINT','SIGTERM','SIGBREAK'):
            number = getattr(signal, name, None)
            if number is not None:
                previous[number] = signal.signal(number, stop)
        entered = True
        return worker.run(emit=emit)
    except (OSError, ValueError, RuntimeError) as error:
        emit({'event':'worker_failed', 'code':'startup_failed', 'error_type':type(error).__name__})
        return 2
    finally:
        for number, handler in previous.items():signal.signal(number, handler)
        if worker is not None and not entered:
            worker.request_stop();worker.schedule.close();worker.tasks.close()


def main(argv=None):
    parser=argparse.ArgumentParser(description='仅执行本工作区已明确确认的公开查询计划；不启动网页服务器或浏览器。')
    parser.add_argument('--workspace',required=True,type=Path)
    return run_cli(parser.parse_args(argv).workspace)


if __name__=='__main__':raise SystemExit(main())
