"""Workbench services and lifecycle shared by local and server adapters."""
from __future__ import annotations

import threading

from .workspace import InputError, Workspace
from .application_commands import ApplicationCommands
from .application_queries import ApplicationQueries
from .collection_workflow import CollectionWorkflow
from .report_tasks import ReportTasks
from .collection import Collector
from .collection_runtime import CollectionRunner
from .collection_handoff import CollectionHandoff
from .guided.service import GuidedService
from .collection_guidance import CollectionGuidance
from .evidence_ui import EvidenceService
from .quality_ui import QualityService
from .public_tasks import PublicTasks
from .public_schedule import PublicSchedule
from .public_queue import PublicQueue
from .windows_startup import WindowsStartup

_DEFAULT_PUBLIC_CLIENT = object()
_UNINITIALIZED_EVIDENCE = object()

# LocalServer keeps these attributes as a compatibility facade. The service
# objects themselves belong to one application, never to an HTTP connection.
SERVICE_NAMES = frozenset({
    'workspace', 'collector', 'collection_runner', 'guidance', 'evidence',
    'quality', 'team', 'releases', 'guided', 'handoff', 'public_tasks',
    'public_schedule', 'public_queue', 'windows_startup', 'mutation_lock', 'report_tasks',
    'queries', 'collection_workflow',
})


class WorkbenchApplication:
    def __init__(self, workspace: Workspace, *, public_client=_DEFAULT_PUBLIC_CLIENT):
        self._lifecycle_lock = threading.RLock()
        self._closed = False
        self._evidence_lock = threading.Lock()
        self._evidence = _UNINITIALIZED_EVIDENCE
        self.workspace = workspace
        self.collector = Collector(workspace)
        self.collection_runner = CollectionRunner(self.collector)
        self.guidance = CollectionGuidance(workspace)
        self.quality = QualityService(workspace)
        from .team_client import TeamUI
        self.team = TeamUI(workspace)
        from .release_updates import ReleaseUpdates
        self.releases = ReleaseUpdates(workspace)
        self.guided = GuidedService(workspace)
        self.handoff = CollectionHandoff(self.collector, self.guided)
        if public_client is _DEFAULT_PUBLIC_CLIENT:
            from .team_client import public_client as configured_public_client
            public_client = configured_public_client(workspace)
        self.public_tasks = PublicTasks(workspace, hybrid_client=public_client)
        self.public_schedule = PublicSchedule(workspace, self.public_tasks)
        self.public_queue = PublicQueue(workspace, self.public_tasks)
        self.windows_startup = WindowsStartup(workspace)
        self.mutation_lock = threading.Lock()
        self.queries = ApplicationQueries(self)
        self.collection_workflow = CollectionWorkflow(self)
        self.commands = ApplicationCommands(self)
        self.report_tasks = ReportTasks(self)
        self._started = False
        self._closed_services = {}

    @property
    def evidence(self):
        # An optional damaged history must not prevent the listener or ordinary
        # reports from opening. Never take the lifecycle lock here: shutdown
        # joins report workers which can be waiting for this property. An
        # initialization begun before close may finish; later first access is
        # rejected. Failed initialization leaves the original bytes untouched.
        with self._evidence_lock:
            if self._evidence is _UNINITIALIZED_EVIDENCE:
                if self._closed:
                    raise InputError('本工作台已关闭，未初始化个人证据。')
                self._evidence = EvidenceService(self.workspace)
            return self._evidence

    @evidence.setter
    def evidence(self, service):
        with self._evidence_lock:
            self._evidence = service

    @evidence.deleter
    def evidence(self):
        with self._evidence_lock:
            del self._evidence

    def execute(self, command, data):
        """Run a named use case without a listener, page or HTTP response."""
        return self.commands.execute(command, data)

    def _start_background(self):
        for name in ('public_schedule', 'public_queue'):
            service = getattr(self, name)
            service.start()
            self._closed_services.pop(name, None)

    def start(self):
        with self._lifecycle_lock:
            if self._closed:
                raise InputError('本工作台已关闭，未启动后台任务。')
            if self._started:
                return
            self._start_background()
            self._started = True

    def _close_service(self, name):
        service = getattr(self, name)
        if self._closed_services.get(name) is service:
            return
        # Record before invoking close so nested or repeated shutdown cannot
        # re-enter the same service. A resumed scheduler is recorded anew.
        self._closed_services[name] = service
        if name == 'workspace':
            service.close_network()
        else:
            service.close()

    def close(self):
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            error = None
            # Stop dispatch first, matching the local serve_forever shutdown.
            # All owners are released even if another service fails to close.
            for name in ('public_queue', 'collection_runner', 'public_schedule', 'report_tasks',
                         'team', 'releases', 'workspace', 'guided', 'public_tasks'):
                try:
                    self._close_service(name)
                except BaseException as exc:
                    if error is None:
                        error = exc
            if error is not None:
                raise error

    def upgrade_preflight(self):
        if self.report_tasks.is_running():
            raise InputError('报告仍在生成，请等待完成后再自动升级；报告不会被强制中断。')
        with self.guided._lock:
            if (self.guided._busy or self.guided._backends or self.guided._closing_backends
                    or self.guided._closure_uncertain or self.guided._ownership.foreign()):
                raise InputError('请先停止采集并关闭采集浏览器；自动升级不会中断该会话。')
        if self.public_tasks.busy() or self.collection_runner.is_running():
            raise InputError('请先等待或停止当前公开/后台采集任务，再自动升级。')
        if self.team._thread and self.team._thread.is_alive():
            raise InputError('团队同步正在执行，请完成后再自动升级。')

    def upgrade_quiesce(self):
        if not self.mutation_lock.acquire(blocking=False):
            raise InputError('另一个操作正在执行，请完成后再自动升级。')
        try:
            with self._lifecycle_lock:
                self.upgrade_preflight()
                self._close_service('public_queue')
                self._close_service('public_schedule')
                self.upgrade_preflight()
                if self.public_queue.is_running() or self.public_schedule.is_running():
                    raise InputError('后台调度尚未退出，未切换版本。请等待任务完成后重启当前工作台；原计划和待办保留。')
        except BaseException:
            self.mutation_lock.release()
            with self._lifecycle_lock:
                if not self._closed:
                    self._start_background()
            raise
        # Keep mutations closed until this process exits and releases OS owners.
