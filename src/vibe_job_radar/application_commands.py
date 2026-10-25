"""Explicit application use cases, independent of HTTP paths and response codes."""
from dataclasses import dataclass

from .workspace import InputError


class UnknownCommand(LookupError):
    pass


class OperationBusy(InputError):
    pass


@dataclass(frozen=True)
class Command:
    service: str
    method: str
    admission: str = 'exclusive'


# New entry points use these named operations; they cannot invoke arbitrary
# service attributes. Existing Workspace/CLI APIs remain compatibility paths.
_ACTIONS = {
    'queries': ('bootstrap',),
    'collection_workflow': ('create_and_start',),
    'workspace': ('add_job', 'import_file', 'plan', 'discover', 'analyze',
        'network_preferences', 'network_proxy_preferences', 'network_proxy_auth_preferences',
        'network_proxy_auth_clear', 'network_pac_preferences', 'network_domain_pac_preferences',
        'network_domain_pac_dns_preferences', 'network_url_pac_preferences',
        'network_url_pac_dns_preferences', 'network_system_pac_preferences',
        'network_wpad_discover', 'network_wpad_cancel', 'network_wpad_preferences', 'network_pac_check'),
    'windows_startup': ('enable', 'disable'),
    'releases': ('check', 'download', 'install'),
    'team': ('check', 'sync', 'protect'),
    'public_schedule': ('configure', 'disable'),
    'public_queue': ('enqueue', 'remove', 'pause', 'resume'),
    'public_tasks': ('start', 'search', 'cancel', 'resume'),
    'guided': ('create', 'action', 'install', 'check_browser', 'diagnose', 'export', 'diagnostics'),
    'quality': ('open', 'save', 'save_pair', 'import_annotations'),
    'evidence': ('state', 'catalogue', 'sources', 'source', 'review', 'upload', 'metric', 'save', 'remove', 'generate'),
    'handoff': ('handoff_preview', 'handoff_start'),
    'collection_runner': ('start', 'state', 'pause'),
    'guidance': ('preview',),
    'collector': ('start', 'step', 'status', 'list', 'register', 'category_next_preview',
        'category_next_start', 'category_page_preview', 'category_page_start',
        'category_recovery_preview', 'category_recovery_start',
        'category_page_retry_preview', 'category_page_retry_start'),
    'report_tasks': ('start', 'state'),
}
COMMANDS = {service + '.' + action: Command(service, action)
            for service, actions in _ACTIONS.items() for action in actions}
# These observations have their own bounded read/state contracts. In
# particular, not every POST or method named "open" is read-only.
for name in ('queries.bootstrap', 'evidence.state', 'collector.list', 'collector.status',
             'collection_runner.state', 'report_tasks.state'):
    command = COMMANDS[name]
    COMMANDS[name] = Command(command.service, command.method, 'observe')
# Report start transfers the same mutation gate to its worker. Collection
# pause is protected by the runner's own lock/ownership and must remain usable
# while a report is writing; it cannot start or replay collection work.
for name in ('report_tasks.start', 'collection_runner.pause'):
    command = COMMANDS[name]
    COMMANDS[name] = Command(command.service, command.method, 'managed')


class ApplicationCommands:
    def __init__(self, application):
        self.application = application

    def execute(self, name, data):
        if not isinstance(name, str) or name not in COMMANDS:
            raise UnknownCommand('Unknown application command')
        if not isinstance(data, dict):
            raise InputError('操作参数必须为对象。')
        command = COMMANDS[name]
        if command.admission != 'observe' and getattr(self.application, '_closed', False):
            raise InputError('本工作台已关闭，未执行新操作。')
        if command.admission == 'exclusive':
            if not self.application.mutation_lock.acquire(blocking=False):
                raise OperationBusy('已有操作执行中，请勿重复提交。')
            try:
                if getattr(self.application, '_closed', False):
                    raise InputError('本工作台已关闭，未执行新操作。')
                return self._invoke(command, data)
            finally:
                self.application.mutation_lock.release()
        return self._invoke(command, data)

    def _invoke(self, command, data):
        # Resolve at call time to preserve LocalServer's replaceable facade.
        target = getattr(self.application, command.service)
        return getattr(target, command.method)(data)
