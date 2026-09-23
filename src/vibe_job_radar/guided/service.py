"""Local guided task service. All browser calls stay on one owning thread.

UI requests enqueue actions and return immediately. Jobs and quotas survive process
restarts; cookie sessions are opt-in and local; passwords are never accepted. Dependency injection enables offline tests.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import queue
import re
import subprocess
import sys
import tempfile
import threading
import uuid
from dataclasses import asdict
from pathlib import Path
from urllib.parse import urlsplit

from ..collection import writer_lock
from ..models import JobRecord
from ..store import Store
from ..utils import atomic_json, utc_now
from ..workspace import InputError, text_field
from .adapters import Registry, builtins
from .browser import PlaywrightBackend
from .native_browser import NativeBackend
from .native_policy import capability as native_capability, contract_for
from ..network_policy import current_policy
from .contracts import CrawlError
from .login_return import LoginReturnManager
from .session_reuse import reuse_current_session
from .saved_session import SavedSession
from .rate import RateLedger, RateLimit
from .transport import diagnose_host
from .diagnostic_trace import DiagnosticTrace, traced, observe, notify
from .browser_health import (BrowserStartupError, HEALTH_MESSAGES, environment_report,
                             failed_report, probe_browser, safe_text, package_version)
from .browser_install import install_commands, run_command
from .browser_choice import BrowserChoice, CHOICES, validate_choice
from .. import tls_context

MESSAGES = {
    'saved_session_invalid': '保存的会话格式无效。未启动新的采集；请清除该平台保存的会话后正常登录。',
    'saved_session_incompatible': '保存会话的工作区、平台版本、后端、浏览器或网络设置不匹配。未自动换身份重试；请明确清除该平台保存的会话。',
    'saved_session_unreadable': '保存的会话无法读取或解密。请使用原 Windows 用户，或清除后重新正常登录。',
    'saved_session_unsafe': '会话目录或文件权限不安全，或存在链接。未读取登录资料；请检查本机 .radar-sessions 目录。',
    'saved_session_busy': '另一个程序正在使用此平台保存的会话，或无法取得文件锁。不会并发复用身份。',
    'saved_session_cleared': '该平台保存的会话已清除，当前会话已关闭。岗位、报告、个人证据与配额保留。',
    'session_reuse_unavailable': '当前采集会话不是可复用的空闲会话。请先完成原任务的登录/等待，或停止该会话；不会自动另开浏览器重试。',
    'session_reuse_incompatible': '当前会话的平台、后端、浏览器或网络设置不匹配。请保留原任务，或明确停止旧会话后再开始；不会串用身份。',
    'login_rate_limited': '打开登录的频次已达到限制：至少间隔5分钟，滚动24小时最多3次。请使用已经打开的登录窗口或等待，不要反复新建任务。',
    'list_page_limit': '本任务列表页数已达到上限。本任务仍受站点共享配额限制。',
    'operation_error': '操作未完成，已有结果保留。请检查环境与页面。',
    'new': '任务已保存。正在准备采集浏览器。',
    'browser_missing': '浏览器组件未就绪。请先点击“安装/修复采集浏览器”，完成后再打开任务。',
    'opening': '正在打开站内搜索；浏览器可能弹出。无需复制职位链接。',
    'reading': '正在读取当前列表并识别具体岗位链接。',
    'ready': '岗位已列出。勾选要研究的岗位，再点击“采集所选并生成报告”。',
    'empty_list': '没有识别到岗位链接。请在采集浏览器完成搜索/登录，再点“读取当前列表”。',
    'manual_required': '需要你操作采集浏览器：完成登录/验证或打开搜索结果，然后回这里读取当前列表。',
    'manual_browser_open': '已打开平台登录页面。请在采集浏览器正常登录；完成后点击“登录完成，继续原任务”，无需重新填写检索条件。',
    'collecting': '正在按强制频次依次打开选中岗位，真实详情链接会自动保留。',
    'completed': '本批次已结束。请查看每条结果和报告；有报告不等于所有岗位均采集成功。',
    'paused': '已请求暂停；当前网络调用结束后停止。没有完成的岗位保留，可继续。',
    'stopped': '任务已停止，采集浏览器已关闭。已保存结果保留。',
    'interrupted': '程序曾退出，浏览器已关闭。明确保留的本站 Cookie 可尝试恢复；其他情况请正常重新登录。已完成岗位不会重复采集。',
    'non_public_address': '域名解析到了非公网地址（可能为 198.18.* Fake-IP）。点击“网络检查”查看地址；先修正代理/DNS，不要重复增加采集预算。',
    'dns_error': '域名解析失败。检查本机 DNS/网络后重试；此错误与账号密码无关。',
    'robots_denied': '当前站点 robots 规则不允许此自动访问路径；本程序已停止。可使用获准接口或回基础页粘贴有权处理的正文。',
    'robots_unavailable': '无法确认 robots 规则；已停止自动采集。不是填写错误。',
    'publisher_delay_exceeds_policy': '站点要求比当前策略更长的抓取间隔，已停止；需适配站点延时策略，不能忽略其限制。',
    'http_401': '站点拒绝此请求（401）；任务已暂停，请检查登录或权限。冷却期内不会反复请求。',
    'http_403': '站点拒绝此请求（403）；不代表单纯缺少密码。请核对访问条件，程序不会换身份继续访问。',
    'http_429': '站点限流（429），已停止并记录至少5分钟冷却。服务要求更久时遵循其 Retry-After。',
    'cooldown': '该站点仍处于冷却期。新建任务也不能绕过，请稍后重新操作。',
    'hourly_limit': '本工作区该站点的小时配额已用完。请稍后再操作，不要新建任务试图提速。',
    'daily_limit': '本工作区该站点的每日配额已用完。已有数据仍可分析。',
    'publisher_wait': '该来源要求降低访问频率；正在等待，已有进度保留，可以暂停或停止。',
    'rate_storage_error': '限频记录无法安全读取或写入，已停止联网；不会重置配额后继续。',
    'publisher_policy_invalid': '发布方限频规则无法安全处理，已保留进度并停止自动访问。',
    'rate_wait': '正在等待安全间隔；不是卡死。可以暂停或停止。',
    'clock_rollback': '系统时钟回拨，限频保护暂停了请求。请先校准本机时间。',
    'structure_changed': '页面没有可确认的完整职位容器；未把整页/推荐职位冒充正文。可在基础页粘贴获准正文。',
    'page_not_ready': '页面没有在限定时间内就绪。可到采集浏览器手动操作，然后读取当前列表。',
    'browser_closed': '采集浏览器已被关闭。点击重新搜索或打开登录以新建会话。',
    'network_error': '网络连接未完成。已有结果保留；检查网络后手动继续，不做无限重试。',
    'resource_domain_blocked': '页面需要适配器尚未允许的资源域名，已阻止；需要审核站点适配，不能任意放行。',
    'write_not_allowed': '该页面需要未开放的写入请求。采集模式只读，不代投简历或发消息。',
    'redirect_requires_attention': '目标重定向无法安全处理，请到采集浏览器确认页面。',
    'job_identity_mismatch': '详情并非所选岗位或页面身份存在冲突，未保存该正文。',
    'jd_incomplete': '当前职位介绍尚未完整展开，未将摘要或登录提示保存为完整正文。',
    'login_origin_changed': '页面离开了该平台允许的登录域名，未填入账号密码。请人工确认。',
    'not_job_list': '当前是登录页、首页或职位详情，不是本次搜索列表；没有把推荐岗位当成搜索结果。登录完成后点“继续原任务”返回原搜索，已选岗位和已取得正文保留。',
    'invalid_job_data': '此条正文或字段不符合岗位数据格式，未截断或冒充成功；本批其他岗位继续处理。',
    'not_job_url': '当前页面不是适配器识别的职位详情；没有保存为完整 JD。',
    'wrong_platform': '页面不属于所选平台，请在同一平台重新搜索。',
    'credential_url': '链接含疑似凭据参数，未保存。请使用无登录凭据的稳定职位链接。',
    'dependency_install_failed': '浏览器组件安装失败。检查网络/磁盘权限后重试，原本地分析仍可使用。',
}

from ..network_settings import DNS_MESSAGES
MESSAGES.update(DNS_MESSAGES)
MESSAGES.update(HEALTH_MESSAGES)
MESSAGES.update({
    'local_proxy_configuration_conflict': 'HTTP与SOCKS专用覆盖同时存在。只保留一种；任务不会猜测路线。',
    'local_socks_configuration_invalid': 'SOCKS5配置无效。仅支持无userinfo的本机socks5入口，凭据需单独设置；不会将socks5h或SOCKS4降级。',
    'local_socks_auth_unsupported': '所选SOCKS代理要求未支持的认证。已停止，不向网站发送请求或绕过代理。',
    'local_socks_protocol_error': 'SOCKS代理协议响应无效。保留任务，请检查所选入口提供的协议。',
    'local_socks_truncated_reply': 'SOCKS代理握手中断。未直连、未重放网站请求。',
    'local_socks_timeout': 'SOCKS代理握手超时。保留任务，待网络恢复后继续。',
    'local_socks_connection_failed': '无法连接所选SOCKS代理。任务已停止，不会偷偷直连。',
    'local_socks_request_rejected': 'SOCKS代理拒绝连接目标。已停止，不更换出口或身份绕过拒绝。',
    'local_proxy_configuration_invalid': '本机HTTP代理配置不符合要求。仅接受明确的 http://127.0.0.1:端口 或 http://[::1]:端口；账号密码需使用独立代理凭据设置，不能写在地址里；不支持远程代理。',
    'local_proxy_credentials_invalid': '代理用户名和密码必须同时提供，并符合受支持的字符和长度；未发送凭据或目标请求。',
    'local_proxy_credentials_require_explicit': '已设置代理凭据，但没有明确的本程序HTTP或SOCKS5代理入口。未把凭据转交系统自动发现的其他代理。',
    'local_proxy_auth_failed': '所选本机代理拒绝认证或选择了不同认证方式。请核对代理凭据；没有降为匿名、重复认证或改走直连。',
    'local_proxy_connection_failed': '已选择本机代理，但代理连接或CONNECT隧道失败。程序没有改走直连；请核对实际HTTP代理端口及代理是否运行。',
    'tls_verification_failed': 'TLS证书验证失败，已停止。检查系统时间、证书与网络环境，不要关闭TLS校验。',
    'tls_handshake_failed': 'TLS握手失败，已停止；这不是缺少招聘账号或浏览器安装问题。',
})


MESSAGES.update({
    'native_administrator_blocked': '浏览器管理策略阻止了本次访问，未改变系统保护或切换路线。',
    'native_contract_unavailable': '该平台的原生采集契约尚未建立；未自动改用其他模式。',
    'native_contract_invalid': '原生站点契约无效，未开始联网。',
    'native_operation_unreviewed': '页面需要尚未核实的业务请求或依赖。请预览脱敏诊断；不是搜索结果为空，需继续维护站点适配。',
    'native_surface_unsupported': '当前原生实验暂不支持此框架或后台执行面，已停止该请求。',
    'native_protocol_error': '浏览器原生拦截协议未完成，已停止；未回退至其他后端。',
    'native_proxy_auth_failed': '应用专用本机连接校验失败；没有使用账号密码或绕过所选代理。',
    'native_policy_changed': '网络偏好已变更，原生会话已停止。请明确继续以建立使用新偏好的会话；配额保持。',
    'native_observation_limit': '原生业务响应或观察队列超过上限，已保留已有结果并停止。',
    'native_unaccounted_response': '出现无法关联到已计量请求的响应，已停止原生会话。',
    'native_business_response_invalid': '已收到业务响应，但内容不是有效的结构化岗位数据。',
})


class GuidedService:
    def __init__(self, workspace, *, registry: Registry | None = None, backend_factory=PlaywrightBackend,
                 ledger: RateLedger | None = None, health_probe=probe_browser, installer=run_command,
                 native_backend_factory=NativeBackend):
        self.workspace = workspace
        self.root = workspace.root / 'guided'
        self.root.mkdir(exist_ok=True, mode=0o700)
        if self.root.is_symlink():
            raise InputError('向导目录不能使用符号链接。')
        self.registry, self.factory = registry or builtins(), backend_factory
        self.native_factory = native_backend_factory
        self.ledger = ledger or RateLedger(self.root / 'rates.sqlite')
        self._lock = threading.RLock()
        self._queue = queue.Queue(maxsize=1)
        self._cancel = threading.Event()
        self._shutdown = threading.Event()
        self._busy = False
        self._active = None
        self._stop_ident = None
        self._backends = {}
        self._session_leases = {}
        self._login_return = LoginReturnManager()
        self._traces = {}  # Bounded in-memory metadata; no automatic disk export.
        self._thread = None
        self._last_install = 'not_started'
        self._restart_required = False
        self._tls_restart_required = False
        self._health_probe, self._installer = health_probe, installer
        self._choice = BrowserChoice(workspace.root)
        self._choice_error = ''
        try:
            self._choice_data = self._choice.read()
            self._selected_browser = self._choice_data['selected']
        except InputError as exc:
            self._choice_data = {'selected': None, 'last_check': None}
            self._selected_browser = None
            self._choice_error = str(exc)
        self._browser_health = {**environment_report(), 'browser_channel': self._selected_browser}
        self._setup = {'stage': 'idle', 'message': '请先检查浏览器；DNS检查与浏览器检查是两回事。', 'steps': []}

    def _path(self, ident):
        if not isinstance(ident, str) or not re.fullmatch(r'[a-f0-9]{32}', ident):
            raise InputError('无效任务编号。')
        p = self.root / f'{ident}.json'
        if p.is_symlink():
            raise InputError('任务文件不能使用符号链接。')
        return p

    def _load(self, ident):
        try:
            return json.loads(self._path(ident).read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise InputError('任务不存在或文件损坏。') from exc

    def _save(self, state, code=None, **changes):
        with self._lock:
            if code:
                state['code'] = code
            state.update(changes)
            state['message'] = MESSAGES.get(state.get('code'), '请查看当前状态或回基础工作台处理数据。')
            state['updated_at'] = utc_now()
            with writer_lock(self.workspace.root):
                atomic_json(self._path(state['id']), state)

    def _spawn(self):
        if not self._thread:
            self._thread = threading.Thread(target=self._worker, name='radar-guided-browser', daemon=True)
            self._thread.start()

    def _submit(self, action, ident=None, secret=None):
        with self._lock:
            if self._busy:
                raise InputError('已有浏览器动作正在运行；请等待或暂停它。')
            self._busy, self._active = True, ident
            self._cancel.clear()
            self._spawn()
            self._queue.put_nowait((action, ident, secret))

    def state(self, data=None):
        with self._lock:
            jobs = []
            for path in sorted(self.root.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[:30]:
                if path.is_symlink():
                    continue
                try:
                    item = self._load(path.stem)
                except InputError:
                    continue
                if item['status'] in {'queued', 'running'} and item['id'] != self._active:
                    item.update(status='interrupted', code='interrupted', message=MESSAGES['interrupted'])
                item['browser_open'] = item['id'] in self._backends
                item['automatic_resume_available'] = (item.get('auto_resume', False)
                                                      and item['browser_open']
                                                      and item['status'] == 'waiting_rate')
                item['backend'] = item.get('backend', 'bridge')
                backend = self._backends.get(item['id'])
                if item['backend'] == 'native' and backend is not None:
                    item['native_requests'] = dict(getattr(backend, 'native_counts', {}))
                jobs.append(item)
            return {'jobs': jobs, 'busy': self._busy, 'active': self._active,
                    'sites': [{**site, 'native': native_capability(self.registry.get(site['key']))}
                              for site in self.registry.describe()], 'limits': asdict(self.ledger.limits),
                    'browser_package': self._package(), 'installation': self._last_install,
                    'installation_scope': 'current_process_actions_only_not_component_readiness',
                    'tls_environment': {**tls_context.status(), 'restart_required': self._tls_restart_required},
                    'browser_choice': {'selected': self._selected_browser, 'options': CHOICES,
                                       'error': self._choice_error,
                                       'last_check': self._choice.historical_view(self._choice_data, self._package())},
                    'browser_health': copy.deepcopy(self._browser_health), 'setup': copy.deepcopy(self._setup),
                    'python': sys.executable, 'roles': {k: v['label'] for k,v in self.workspace.config['roles'].items()},
                    'sessions_persisted': any(j.get('saved_session_status') == 'saved_unverified' for j in jobs),
                    'session_storage_scope': 'opt_in_cookies_only_not_account_certification',
                    'external_site_certification': False}

    @staticmethod
    def _package():
        try:
            return importlib.metadata.version('playwright')
        except importlib.metadata.PackageNotFoundError:
            return None

    def create(self, data):
        adapter = self.registry.get(data.get('platform'))
        if type(data.get('persist_session', False)) is not bool:
            raise InputError('在本机保留会话必须为明确的布尔选项。')
        if type(data.get('reuse_current_session', False)) is not bool:
            raise InputError('复用当前采集会话必须为明确的布尔选项。')
        if type(data.get('diagnostics', False)) is not bool:
            raise InputError('诊断选项必须为布尔值。')
        mode = data.get('backend', 'bridge')
        if not isinstance(mode, str) or mode not in {'bridge', 'native'}:
            raise InputError('请选择已有桥接模式或原生实验模式。')
        if mode == 'native':
            if data.get('native_consent') is not True:
                raise InputError('启用原生实验模式前请阅读并确认范围；不会自动换路线。')
            try:
                contract_for(adapter)
            except CrawlError:
                raise InputError('该平台尚无原生访问契约；不能将其他平台配置当作已支持。') from None
        keyword = text_field(data, 'keyword', required=True, limit=100).strip()
        roles = data.get('roles', ['time_series'])
        if not isinstance(roles, list) or not roles or any(not isinstance(r,str) or r not in self.workspace.config['roles'] for r in roles):
            raise InputError('请选择已配置的目标岗位。')
        max_pages, max_jobs = data.get('max_pages', 1), data.get('max_jobs', 5)
        if type(max_pages) is not int or not 1 <= max_pages <= 5 or type(max_jobs) is not int or not 1 <= max_jobs <= 20:
            raise InputError('首次建议1页/5个岗位；最多5页/20个岗位。')
        if data.get('consent') is not True:
            raise InputError('请确认本次正常访问与数据使用范围。')
        rights = text_field(data, 'rights_note', required=True, limit=2000)
        seed = text_field(data, 'list_url', limit=2048).strip()
        if seed:
            seed = adapter.accept_url(seed)
        state = {'id': uuid.uuid4().hex, 'schema_version': 1, 'platform': adapter.key,
                 'keyword': keyword, 'roles': roles, 'max_pages': max_pages, 'max_jobs': max_jobs,
                 'rights_note': rights, 'search_url': seed or adapter.search_url(keyword),
                 'status': 'queued', 'code': 'new', 'cards': [], 'pages_seen': [], 'report_id': '',
                 'phase': 'search', 'selection': [], 'created_at': utc_now(), 'updated_at': utc_now(),
                 'authentication': 'not_checked', 'certification': 'not_live_verified',
                 'diagnostics_enabled': data.get('diagnostics', False), 'backend': mode,
                 'reuse_current_session': data.get('reuse_current_session', False), 'session_reused': False,
                 'persist_session': data.get('persist_session', False), 'saved_session_status': 'off'}
        with self._lock:
            if self._busy:
                raise InputError('已有任务运行，请先暂停。')
            self._save(state)
            self._submit('search', state['id'])
        return {'id': state['id'], 'queued': True}

    def action(self, data):
        ident, action = data.get('id'), data.get('action')
        state = self._load(ident)
        if any(k in data for k in ('username','password','cookie','credential_consent')):
            raise InputError('本向导不接收账号密码；请在平台原生浏览器页面登录。')
        if any(k in data for k in ('backend', 'native_consent', 'native_contract', 'persist_session', 'storage_state')):
            raise InputError('任务后端不可中途更换；新任务仍共享原配额。')
        permitted = {'search', 'login', 'capture', 'more', 'collect', 'pause', 'stop', 'resume', 'forget_session'}
        if action not in permitted:
            raise InputError('未知操作。')
        if action == 'forget_session' and data.get('confirm') is not True:
            raise InputError('清除该平台保存的会话需要明确确认。')
        if 'auto_continue' in data and (action != 'login' or type(data['auto_continue']) is not bool):
            raise InputError('自动接续只用于本次登录，必须明确勾选。')
        if action in {'pause','stop'}:
            with self._lock:
                self._login_return.disarm(ident)
                if self._busy and self._active is None:
                    raise InputError('浏览器安装正在运行；此任务按钮不能取消安装。')
                if self._active and self._active != ident:
                    raise InputError('另一个任务正在运行。')
                self._cancel.set()
                if action == 'stop' and self._busy:
                    self._stop_ident = ident
                elif self._busy:
                    return {'id': ident, 'message': MESSAGES['paused']}
                else:
                    self._submit('close' if action == 'stop' else 'pause_idle', ident)
            return {'id': ident, 'message': MESSAGES['paused' if action=='pause' else 'stopped']}
        if action == 'resume' and state['status'] in {'completed', 'stopped'}:
            return {'id': ident, 'message': '该任务已结束；已有报告保留。重新打开搜索请使用搜索按钮。'}
        secret = None
        if action == 'collect':
            ids = data.get('selected')
            known = {r['id'] for r in state['cards']}
            if (not isinstance(ids,list) or not ids or len(ids)>state['max_jobs']
                    or any(not isinstance(i,str) or i not in known for i in ids) or len(set(ids))!=len(ids)):
                raise InputError('请选择列表中的岗位，不能超过本批数量上限。')
            state['selection'] = ids
            state['report_id'] = ''
            state.pop('outcome', None)
            state['phase'] = 'collect'
        with self._lock:
            if self._busy:
                raise InputError('当前动作尚未结束，请先暂停或等待。')
            self._login_return.disarm(ident)
            if action == 'login':
                state['auto_continue_after_login'] = data.get('auto_continue', False)
            state['login_continuation'] = 'off'
            self._save(state, 'opening' if action in {'search','login'} else state['code'],
                       status='queued', auto_resume=False, next_allowed_at=None)
            self._submit(action, ident, secret)
        return {'id': ident, 'queued': True}

    def install(self, data):
        if (not isinstance(data, dict) or set(data) - {'consent', 'mode'}
                or data.get('consent') is not True
                or not isinstance(data.get('mode', 'ensure'), str)
                or data.get('mode', 'ensure') not in {'ensure', 'reinstall', 'upgrade', 'tls'}):
            raise InputError('请明确确认安装/重下载/更新模式；不接受命令、路径或版本参数。')
        if data.get('mode') == 'tls':
            environment = tls_context.status()
            if not environment['windows'] or environment['explicit_ca_environment']:
                raise InputError('此操作仅用于 Windows 默认信任策略；已有自定义CA配置须由管理员核对，不会覆盖。')
        self._submit_setup('install', mode=data.get('mode', 'ensure'))
        return {'queued': True}

    def _submit_setup(self, action, *, mode='ensure'):
        with self._lock:
            if self._busy:
                raise InputError('已有动作正在运行，请结束后再检查或安装。')
            if self._backends:
                raise InputError('请先点“停止并关闭登录会话”，再检查或安装。不会擅自关闭你的登录浏览器。')
            self._browser_health = environment_report()
            self._setup = {'stage': 'queued', 'message': '已排队准备浏览器组件检查。', 'steps': []}
            self._submit(action, secret=mode)

    def check_browser(self, data):
        if data:
            if (not isinstance(data, dict) or set(data) != {'channel', 'consent'}
                    or data.get('consent') is not True):
                raise InputError('更换浏览器需明确确认；不接受网址、命令或路径。')
            channel = validate_choice(data['channel'])
            self._submit_setup('choose_browser', mode=channel)
        else:
            self._submit_setup('check_browser')
        return {'queued': True, 'network_scope': 'blank local page only; no job requests'}

    def _restart_report(self):
        return {**environment_report(), 'stage': 'restart', 'code': 'browser_restart_required',
                'ready': False, 'restart_required': True,
                'message': HEALTH_MESSAGES['browser_restart_required']}

    def _remember_check(self, report, channel, *, select=False):
        try:
            data = self._choice.record(report, channel, select=select)
            self._choice_data = data
            self._choice_error = ''
            if select:
                self._selected_browser = channel
        except (OSError, ValueError, KeyError, TypeError) as exc:
            # Optional history must not hide the actual probe result. A requested
            # choice, however, cannot claim it was saved when the write failed.
            self._choice_error = '本机检查历史或浏览器选择未能保存；原选择未更改。'
            if select:
                raise InputError(self._choice_error) from exc

    def _check_browser(self, channel=None, *, select=False):
        channel = channel or self._selected_browser
        if self._restart_required:
            report = self._restart_report()
            with self._lock:
                self._browser_health = report
                self._setup.update(stage='restart_required', message=report['message'])
            return report
        if channel not in CHOICES:
            report = failed_report(environment_report(), ValueError('invalid saved browser selection'),
                                   code='browser_choice_invalid')
        else:
            with self._lock:
                self._setup.update(stage='launch_check', message='正在实际打开并关闭所选浏览器空白页，不访问招聘网站。')
            # No automatic fallback on failure, and no in-use context is replaced.
            report = self._health_probe(**({'channel': channel} if channel != 'bundled' else {}))
            report = {**report, 'browser_channel': channel}
            with self._lock:
                try:
                    self._remember_check(report, channel, select=select and report['ready'] is True)
                    report['selection_applied'] = select and report['ready'] is True
                except InputError as exc:
                    report = failed_report(report, exc, code='browser_choice_invalid')
        with self._lock:
            self._browser_health = report
            self._setup.update(stage='ready' if report['ready'] else 'failed', message=report['message'])
        return report

    def _install_browser(self, mode='ensure'):
        if mode == 'tls':
            return self._install_tls_component()
        self._last_install = 'installing'
        before = package_version('playwright')
        labels = {'package_install': '正在安装/检查 Playwright Python 包。',
                  'browser_download': '正在通过 Playwright 下载配套 Chromium，不是 pip install Chromium。'}
        if mode == 'upgrade':
            labels['package_install'] = '正在按明确同意更新当前解释器的 Playwright；完成后需重新启动工作台。'
        if mode != 'ensure':
            labels['browser_download'] = '正在重新下载匹配的 Chromium，已有缓存不会被直接当作修复成功。'
        for stage, command in install_commands(mode):
            step = {'stage': stage, 'log': '', 'returncode': None}
            with self._lock:
                self._setup.update(stage=stage, message=labels[stage])
                self._setup['steps'].append(step)
            def progress(text):
                with self._lock:
                    step['log'] = safe_text(text, 12000)
            if mode == 'upgrade' and stage == 'package_install':
                # Even a failed/partial pip update may invalidate an imported SDK.
                self._restart_required = True
            result = self._installer(command, cancel=self._shutdown, progress=progress)
            if stage == 'package_install' and package_version('playwright') != before:
                if any(n == 'playwright' or n.startswith('playwright.') for n in sys.modules):
                    self._restart_required = True
            with self._lock:
                step.update(returncode=result.returncode, log=safe_text(result.output, 12000),
                            timed_out=result.timed_out, cancelled=result.cancelled)
            if result.returncode or result.timed_out or result.cancelled:
                code = 'dependency_install_timeout' if result.timed_out else 'dependency_install_failed'
                self._last_install = code
                with self._lock:
                    self._browser_health = failed_report({**environment_report(), 'stage': stage}, RuntimeError(result.output), code=code)
                    self._setup.update(stage='failed', message=HEALTH_MESSAGES[code])
                return
        # Zero exit codes are not proof of a usable browser. Verify the real
        # headed backend on the same owner thread before reporting installed.
        report = self._check_browser(channel='bundled')
        self._last_install = ('restart_required' if self._restart_required else
                              'installed' if report['ready'] else 'installed_not_ready')

    def _install_tls_component(self):
        # A fixed, user-confirmed optional package. Never install a CA or browser.
        self._last_install = 'installing'
        stage, command = install_commands('tls')[0]
        step = {'stage': stage, 'log': '', 'returncode': None}
        with self._lock:
            self._setup.update(stage=stage, message='正在安装 Windows 原生证书验证组件；不修改证书信任列表或浏览器。')
            self._setup['steps'].append(step)
            # Partial updates also require a clean process before further use.
            self._tls_restart_required = self._restart_required = True
        def progress(value):
            with self._lock:
                step['log'] = safe_text(value, 12000)
        result = self._installer(command, cancel=self._shutdown, progress=progress)
        with self._lock:
            step.update(returncode=result.returncode, log=safe_text(result.output, 12000),
                        timed_out=result.timed_out, cancelled=result.cancelled)
            succeeded = not (result.returncode or result.timed_out or result.cancelled)
            self._last_install = 'restart_required' if succeeded else 'dependency_install_failed'
            message = ('证书验证组件安装命令已完成；请重新启动工作台，再检查当前网络策略。安装成功不代表 TLS 已通过。'
                       if succeeded else '证书验证组件安装未完成；请保留错误并重新启动工作台。未安装证书或更换浏览器。')
            self._setup.update(stage='restart_required', message=message)
            self._browser_health = {**self._restart_report(), 'message': message,
                                    'browser_channel': self._selected_browser}

    def diagnose(self, data):
        if self._tls_restart_required:
            return {'passed': False, 'code': 'tls_component_restart_required',
                    'message': '证书验证组件操作后请先重新启动工作台；本次未发起 DNS 或网络请求。',
                    'target_connection_tested': False, 'browser_tested': False}
        adapter = self.registry.get(data.get('platform'))
        from .network_diagnostic import diagnose_workspace
        with self._lock:
            has_sessions = bool(self._backends)
        return diagnose_workspace(self.workspace, urlsplit(adapter.search_url('test')).hostname,
                                  raw_probe=diagnose_host, cancelled=self._shutdown,
                                  has_sessions=has_sessions)

    def export(self, data):
        state = self._load(data.get('id'))
        return {'id': state['id'], 'urls': [c.get('resolved_url') or c['url'] for c in state['cards']],
                'source': 'observed links; not a proof of complete market coverage'}

    def _trace_for(self, state):
        # Called on the owning worker, and by the authenticated local preview.
        # Instrumentation availability must not affect collection decisions.
        try:
            with self._lock:
                if state.get('diagnostics_enabled') is not True:
                    return None
                ident = state['id']
                if ident not in self._traces:
                    while len(self._traces) >= 30:
                        self._traces.pop(next(iter(self._traces))).disable()
                    adapter = self.registry.get(state['platform'])
                    self._traces[ident] = DiagnosticTrace(ident, adapter.key,
                        adapter_version=getattr(adapter, 'version', 'unknown'), browser=self._selected_browser)
                return self._traces[ident]
        except Exception:
            return None

    def diagnostics(self, data):
        """Authenticated local metadata preview; never a new site request."""
        if set(data) - {'id', 'enabled'}:
            raise InputError('诊断只接受任务编号和启用选项。')
        with self._lock:
            state = self._load(data.get('id'))
            if 'enabled' in data:
                if type(data['enabled']) is not bool:
                    raise InputError('诊断选项必须为布尔值。')
                if self._busy:
                    raise InputError('请先暂停或等待当前动作结束，再更改诊断选项。')
                if not data['enabled']:
                    trace = self._traces.pop(state['id'], None)
                    if trace:
                        trace.disable()
                self._save(state, diagnostics_enabled=data['enabled'])
            trace = self._trace_for(state)
            return trace.snapshot() if trace is not None else {
                'schema_version': 1, 'trace_id': state['id'], 'enabled': False,
                'events': [], 'scope': '诊断未启用或不可用；没有读取浏览器、联网或导出任务正文。'}

    def _new_session_lease(self, state):
        return SavedSession(self.workspace.root, self.registry.get(state['platform']),
                            backend=state.get('backend', 'bridge'), browser=self._selected_browser,
                            network=current_policy().fingerprint)

    def _close_backend(self, ident):
        backend = self._backends.pop(ident, None)
        try:
            if backend:
                backend.close()
        finally:
            lease = self._session_leases.pop(ident, None)
            if lease:
                lease.close()

    def _checkpoint_session(self, state):
        lease = self._session_leases.get(state['id'])
        backend = self._backends.get(state['id'])
        if (not lease or not backend or not state.get('persist_session')
                or state.get('status') not in {'ready', 'completed'} or not state.get('cards')
                or getattr(backend, 'auth_mode', False) or getattr(backend, 'error', None)
                or self._cancel.is_set()):
            return
        try:
            lease.save(backend.export_session_cookies())
            self._save(state, saved_session_status=lease.status, saved_session_error='')
        except Exception as exc:
            # Persistence failure must not discard already collected JDs/reports.
            code = exc.code if isinstance(exc, CrawlError) else 'saved_session_unreadable'
            self._save(state, saved_session_status='save_failed', saved_session_error=code)

    def _forget_session(self, state):
        platform = state['platform']
        for ident, backend in list(self._backends.items()):
            if backend.adapter.key == platform:
                self._login_return.disarm(ident)
                self._close_backend(ident)
        lease = self._new_session_lease(state)
        try:
            lease.forget()
        finally:
            lease.close()
        # Revocation applies to this platform in this workspace, including old
        # tasks. Their later resume must not silently re-enable persistence.
        for path in self.root.glob('*.json'):
            try:
                task = self._load(path.stem)
            except InputError:
                continue
            if task.get('platform') == platform:
                self._save(task, persist_session=False, saved_session_status='cleared',
                           saved_session_error='', login_continuation='off')
        self._save(state, 'saved_session_cleared', status='paused', persist_session=False,
                   saved_session_status='cleared', saved_session_error='', login_continuation='off')

    @traced('browser_session', 'service', state_index=0)
    def _backend(self, state):
        if self._restart_required:
            raise BrowserStartupError(self._restart_report())
        if self._selected_browser not in CHOICES:
            raise BrowserStartupError(failed_report(environment_report(),
                ValueError('invalid saved browser selection'), code='browser_choice_invalid'))
        ident = state['id']
        if reuse_current_session(self, state):
            old = state['session_source_task']
            if old in self._session_leases:
                self._session_leases[ident] = self._session_leases.pop(old)
        previous = self._backends.get(ident)
        native = state.get('backend', 'bridge') == 'native'
        if native and previous and previous.wire.network_policy.fingerprint != current_policy().fingerprint:
            self._close_backend(ident)
            raise CrawlError('native_policy_changed')
        if previous and hasattr(previous, 'alive') and not previous.alive():
            self._close_backend(ident)
        if ident not in self._backends:
            for old in list(self._backends):
                self._close_backend(old)
            def progress(code, seconds):
                self._save(state, code, wait_seconds=seconds)
            factory = self.native_factory if native else self.factory
            options = {'channel': self._selected_browser} if self._selected_browser != 'bundled' else {}
            lease = None
            try:
                if state.get('persist_session'):
                    lease = self._new_session_lease(state)
                    restored = lease.restore()
                    if restored is not None:
                        options['storage_state'] = restored
                    self._save(state, saved_session_status=lease.status,
                               authentication='restored_session_unverified' if restored is not None else 'not_checked')
                self._backends[ident] = factory(self.registry.get(state['platform']), self.ledger,
                                               self._cancel, progress, **options)
                if lease:
                    self._session_leases[ident] = lease
            except Exception:
                if lease:
                    lease.close()
                raise
            health = getattr(self._backends[ident], 'startup_report', None)
            if health:
                with self._lock:
                    self._browser_health = dict(health)
                    self._remember_check(health, self._selected_browser)
        backend = self._backends[ident]
        if native:
            backend.policy_check = lambda: self.workspace.network_policy().fingerprint == backend.wire.network_policy.fingerprint
        bind = getattr(backend, 'bind_diagnostics', None)
        if callable(bind):
            try:
                bind(self._trace_for(state))
            except Exception:
                pass  # Diagnostics must not affect a third-party backend.
        return backend

    @traced('listing', 'service', state_index=0)
    def _gather(self, state, backend, adapter, *, navigate=False, more=False):
        if navigate:
            backend.open(state['search_url'])
        if hasattr(backend, 'collection_mode'):
            backend.collection_mode()
        if more and len(state['pages_seen']) >= state['max_pages']:
            raise CrawlError('list_page_limit')
        if more and not backend.next_page():
            self._save(state, 'ready' if state['cards'] else 'empty_list', status='ready',
                       list_end='no_next_button')
            return
        # Even at the page budget, inspect the current surface. Previously the
        # loop was skipped and a login/detail/empty page was reported as ready
        # merely because an earlier page had supplied cards.
        while True:
            if self._cancel.is_set():
                raise CrawlError('paused')
            page = backend.snapshot()
            if hasattr(backend, 'wire'):
                backend.wire.ensure_robots(page.url)
            with observe(self._trace_for(state), 'list_parse', url=page.url):
                cards = adapter.cards(page)
                if not cards:
                    notify(self._trace_for(state), 'note', code='no_cards')
            if not cards:
                self._save(state, 'empty_list', status='waiting_manual', phase='select',
                           last_list_url=page.url)
                return
            signature = hashlib.sha256('\n'.join(c.id for c in cards).encode()).hexdigest()
            if signature in state['pages_seen']:
                self._save(state, last_list_url=page.url)
                break
            if len(state['pages_seen']) >= state['max_pages']:
                raise CrawlError('list_page_limit')
            state['pages_seen'].append(signature)
            existing = {r['id'] for r in state['cards']}
            for card in cards:
                if card.id not in existing and len(state['cards']) < 100:
                    state['cards'].append({**asdict(card), 'status': 'discovered', 'record_id': '', 'resolved_url': ''})
                    existing.add(card.id)
            self._save(state, 'reading', status='running', last_list_url=page.url)
            if len(state['pages_seen']) >= state['max_pages'] or not backend.next_page():
                break
        self._save(state, 'ready' if state['cards'] else 'empty_list', status='ready' if state['cards'] else 'waiting_manual', phase='select')

    @traced('collection', 'service', state_index=0)
    def _collect(self, state, backend, adapter):
        self._save(state, 'collecting', status='running', phase='collect')
        selected = set(state['selection'])
        try:
            for row in state['cards']:
                if row['id'] not in selected or row['status'] == 'ok':
                    continue
                if self._cancel.is_set():
                    raise CrawlError('paused')
                row['status'] = 'opening'
                self._save(state)
                try:
                    trace = self._trace_for(state)
                    with observe(trace, 'detail_navigation', url=row['url'], entity=row['id']):
                        page = backend.open(row['url'])
                    with observe(trace, 'detail_identity', url=page.url, entity=row['id']):
                        final_url = adapter.accept_url(page.url, detail=True)
                        validate_identity = getattr(adapter, 'validate_detail_identity', None)
                        if callable(validate_identity):
                            validate_identity(row['url'], page)
                    with observe(trace, 'detail_parse', url=page.url, entity=row['id']):
                        parsed = adapter.detail(page)
                    with observe(trace, 'persist', entity=row['id']):
                        try:
                            record = JobRecord(**parsed, url=final_url, platform=adapter.key,
                                source_mode='browser_fetch', rights_note=state['rights_note'],
                                source_ref=f"guided:{state['id']}:{row['id']}",
                                raw_sha256=hashlib.sha256(page.html.encode()).hexdigest())
                        except (TypeError, ValueError) as exc:
                            raise CrawlError('invalid_job_data') from exc
                        with writer_lock(self.workspace.root), Store(self.workspace.db) as store:
                            store.add(record)
                    row.update(status='ok', record_id=record.record_id, resolved_url=final_url, title=record.title,
                               parser=record.parser, body_sha256=hashlib.sha256(record.text.encode('utf-8')).hexdigest(),
                               adapter_version=getattr(adapter, 'version', 'custom'))
                    identity = getattr(adapter, 'job_identity', None)
                    if callable(identity):
                        row['platform_job_id'] = identity(final_url)
                except CrawlError as exc:
                    row['status'] = exc.code
                    self._save(state)
                    if exc.code not in {'structure_changed','not_job_url','invalid_job_data','job_identity_mismatch','jd_incomplete'}:
                        raise
                self._save(state)
        finally:
            # Keep a usable batch report even if a later selected job blocks.
            self._finalize_report(state, adapter)
        self._save(state, 'completed', status='completed', phase='report')

    @staticmethod
    def _outcome(state, manifest=None):
        rows = [c for c in state['cards'] if c['id'] in set(state['selection'])]
        saved = sum(c['status'] == 'ok' for c in rows)
        waiting = {'discovered', 'opening', 'manual_required', 'paused', 'rate_wait',
                   'publisher_wait', 'cooldown', 'http_429', 'hourly_limit', 'daily_limit'}
        pending = sum(c['status'] in waiting for c in rows)
        failed = len(rows) - saved - pending
        stats = (manifest or {}).get('stats', {})
        target_jobs = stats.get('full_text_job_groups', 0)
        ai_jobs = stats.get('vibe_evidence_job_groups', 0)
        if not saved:
            status, message = 'no_data', '本批尚未取得可用正文；任务结束不等于岗位研究完成。'
        elif not manifest or manifest.get('status') != 'completed':
            status, message = 'analysis_incomplete', '已有正文，但分析未完整完成，请先核对错误。'
        elif not target_jobs:
            status, message = 'no_target', '正文已保存，但没有纳入本次目标岗位；请核对岗位方向和原文，不用其他岗位冒充结果。'
        elif not ai_jobs:
            status, message = 'no_ai_evidence', '已取得目标岗位正文，未提取到已接收的正向 AI 编程证据；可复核原文，不表示市场没有需求。'
        elif failed or pending:
            status, message = 'partial', '部分目标岗位已形成研究结果；仍有未完成条目，报告不会把它们隐去。'
        else:
            status, message = 'ready', '本批目标岗位正文与研究结果已就绪，可查看原文并继续本人证据。'
        return {'schema_version': 1, 'status': status, 'message': message,
                'selected': len(rows), 'saved': saved, 'failed': failed, 'pending': pending,
                'target_jobs': target_jobs, 'ai_jobs': ai_jobs,
                'scope': 'selected batch only; saved is not target match or live-site certification'}

    @traced('report', 'service', state_index=0)
    def _finalize_report(self, state, adapter):
        selected = set(state['selection'])
        if not any(c['status']=='ok' and c['id'] in selected for c in state['cards']):
            notify(self._trace_for(state), 'note', code='no_records')
            self._save(state, outcome=self._outcome(state), report_id='')
            return
        from ..pipeline import analyze
        with writer_lock(self.workspace.root):
            report_id = uuid.uuid4().hex
            report_root = self.workspace.root/'reports'/report_id
            selected_records = {c['record_id'] for c in state['cards'] if c['id'] in selected and c['status']=='ok'}
            with Store(self.workspace.db) as store:
                records = [r for r in store.records(latest_only=False) if r.record_id in selected_records]
            with tempfile.TemporaryDirectory(prefix='.batch-',dir=self.root) as tmp:
                batch_db = Path(tmp)/'batch.sqlite'
                with Store(batch_db) as batch:
                    for record in records:
                        batch.add(record)
                config = copy.deepcopy(self.workspace.config)
                config['platforms'].setdefault(adapter.key,
                    {'label': adapter.label, 'domains': list(adapter.domains)})
                manifest = analyze(batch_db, report_root, config=config,
                    role_filter=state['roles'], platform_filter=[adapter.key])
            outcome = self._outcome(state, manifest)
            audit = {'schema_version': 1, 'task_id': state['id'],
                     'adapter': {'key': adapter.key, 'version': getattr(adapter, 'version', 'custom')},
                     'outcome': outcome, 'items': [
                         {k: c.get(k, '') for k in ('id', 'url', 'resolved_url', 'status', 'record_id', 'platform_job_id', 'parser', 'body_sha256', 'adapter_version')}
                         for c in state['cards'] if c['id'] in selected]}
            audit_path = report_root/'guided_acquisition.json'
            atomic_json(audit_path, audit)
            manifest['acquisition_outcome'] = outcome
            manifest['output_files_sha256'][audit_path.name] = hashlib.sha256(audit_path.read_bytes()).hexdigest()
            atomic_json(report_root/'run_manifest.json', manifest)
        self._save(state, report_id=report_id, outcome=outcome,
                   report_scope='exact successful selected records in this batch')

    @traced('task', 'service', state_index=1)
    def _run(self, action, state, secret):
        if action == 'forget_session':
            self._forget_session(state)
            return
        if action == 'close':
            self._close_backend(state['id'])
            self._save(state,'stopped',status='stopped'); return
        if action == 'pause_idle':
            self._cancel.set()
            self._save(state,'paused',status='paused'); return
        adapter = self.registry.get(state['platform'])
        if action == 'login':
            try:
                self.ledger.reserve(adapter.key, 'login')
            except RateLimit as exc:
                self._save(state, wait_seconds=round(exc.wait, 1))
                raise CrawlError('login_rate_limited') from exc
        backend = self._backend(state)
        self._save(state, 'opening', status='running')
        pending_login = state.get('authentication') == 'manual_pending'
        if action == 'login':
            # Save intent before open(): the native page itself can ask for
            # manual assistance and raise, but the original task must survive.
            watching = self._login_return.arm(state, backend)
            self._save(state, authentication='manual_pending',
                       login_continuation='watching' if watching else 'off')
            backend.open(adapter.login_url, authentication=True)
            self._save(state, 'manual_browser_open', status='waiting_manual', authentication='manual_pending')
        elif action in {'capture','more','search'}:
            self._gather(state,backend,adapter,navigate=action=='search',more=action=='more')
        elif action in {'collect','resume'}:
            if state['phase'] == 'collect':
                self._collect(state,backend,adapter)
            else:
                self._gather(state,backend,adapter,navigate=action=='resume')
        if (pending_login and action in {'capture', 'search', 'resume', 'collect'}
                and state['status'] in {'ready', 'completed'}):
            # This is task progress after a user action, not login certification.
            self._save(state, authentication='user_resumed')

    def _resume_due(self):
        """Resume only safe read actions in a still-owned browser session.

        Restarted processes preserve the due time and selection, but require one
        user resume/login because browser credentials intentionally aren't saved.
        Never replay login POSTs or repeat a pagination click automatically.
        """
        with self._lock:
            if self._busy or self._shutdown.is_set():
                return
            for ident in list(self._backends):
                try:
                    state = self._load(ident)
                    due = state.get('next_allowed_at')
                    action = state.get('retry_action')
                    if (state['status'] == 'waiting_rate' and state.get('auto_resume')
                            and action in {'search', 'capture', 'collect', 'resume'}
                            and isinstance(due, (int, float)) and math.isfinite(due)
                            and due <= self.ledger.clock()):
                        self._save(state, status='queued', auto_resume=False, next_allowed_at=None)
                        self._submit(action, ident)
                        return
                except (InputError, OSError, ValueError):
                    continue

    def _worker(self):
        while not self._shutdown.is_set():
            try:
                action, ident, secret = self._queue.get(timeout=0.1)
            except queue.Empty:
                self._resume_due()
                for backend in list(self._backends.values()):
                    try: backend.pump()
                    except Exception: pass
                try:
                    self._login_return.tick(self)
                except Exception:
                    pass  # Optional continuation must not terminate the worker.
                continue
            try:
                if action == 'install':
                    self._install_browser(secret or 'ensure')
                    continue
                if action == 'check_browser':
                    self._check_browser()
                    continue
                if action == 'choose_browser':
                    self._check_browser(secret, select=True)
                    continue
                state = self._load(ident)
                from ..network_policy import use_policy
                with observe(self._trace_for(state), 'network_policy'):
                    policy = self.workspace.network_policy()
                with use_policy(policy):
                    self._run(action,state,secret)
                    self._checkpoint_session(state)
                if state['status'] in {'completed','stopped','paused','ready'}:
                    self._cancel.set()
            except Exception as exc:
                code = exc.code if isinstance(exc,CrawlError) else 'operation_error'
                if action in {'install', 'check_browser', 'choose_browser'}:
                    code = 'dependency_install_failed' if action == 'install' else 'browser_check_failed'
                    if action == 'install':
                        self._last_install = code
                    with self._lock:
                        self._browser_health = failed_report(environment_report(), exc, code=code)
                        self._setup.update(stage='failed', message=self._browser_health['message'])
                else:
                    try:
                        current = self._load(ident)
                        stopping = self._stop_ident == ident
                        if stopping:
                            self._close_backend(ident)
                            self._save(state,'stopped',status='stopped')
                        else:
                            if isinstance(exc, BrowserStartupError):
                                self._browser_health = exc.report
                                self._remember_check(exc.report, self._selected_browser)
                                state['startup_diagnostic'] = exc.report
                            if isinstance(exc, RateLimit) and exc.next_allowed_at is not None:
                                backend = self._backends.get(ident)
                                safe = (action in {'search', 'capture', 'collect', 'resume'}
                                        and not getattr(backend, 'auth_mode', False)
                                        and code != 'clock_rollback')
                                self._save(state, code, status='waiting_rate',
                                           wait_seconds=round(exc.wait, 1),
                                           next_allowed_at=exc.next_allowed_at,
                                           retry_action=action, auto_resume=safe)
                                self._cancel.set()  # No background browser requests during deferral.
                            else:
                                self._save(state,code,status='paused' if code=='paused' else 'waiting_manual',
                                           auto_resume=False, next_allowed_at=None)
                    except Exception:
                        pass
            finally:
                secret = None
                if ident and self._stop_ident == ident:
                    self._stop_ident = None
                    self._close_backend(ident)
                    self._save(self._load(ident), 'stopped', status='stopped')
                with self._lock:
                    self._busy, self._active = False, None
                self._queue.task_done()
        for ident in list(self._backends):
            try: self._close_backend(ident)
            except Exception: pass

    def close(self):
        self._cancel.set(); self._shutdown.set()
        if self._thread:
            self._thread.join(timeout=5)
