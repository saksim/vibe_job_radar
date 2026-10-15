"""Workspace DNS consent and explicit anonymous loopback proxy preferences."""
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
from dataclasses import replace

from .network_policy import NetworkPolicy
from .loopback_proxy import LoopbackProxy, LocalProxyError
from .loopback_socks import LoopbackSocks5
from .digest_proxy import LoopbackDigestProxy
from .vm_proxy import VmHTTPProxy, VmSocks5Proxy, ERROR_MESSAGES as VM_MESSAGES
from .proxy_credentials import configured as credentials_configured, ERROR_MESSAGES as AUTH_MESSAGES
from . import workspace_proxy_auth
from .html_parser import _unique_object
from .utils import atomic_json, atomic_text, utc_now
from .workspace import InputError
from .pac import PacSnapshot, CONSENT as PAC_CONSENT, MESSAGES as PAC_MESSAGES, validate_script
from . import pac_native, pac_url, pac_url_worker, pac_domain
from . import system_pac, wpad, wpad_native

CONSENT = 'cloudflare-doh-v1'
PUBLIC_DNS_CONSENT = 'cloudflare-public-doh-v1'
MODES = {'system', 'fake_ip_doh', 'public_doh'}
PROXY_CONSENT = 'workspace-anonymous-loopback-v1'
VM_CONSENT = 'workspace-anonymous-vm-host-v1'
PROXY_PARSERS = {'http': LoopbackProxy, 'socks5': LoopbackSocks5,
                 'vm_http': VmHTTPProxy, 'vm_socks5': VmSocks5Proxy}
PROXY_MODES = {'auto', *PROXY_PARSERS}
PROXY_DEFAULT = {'proxy_mode': 'auto', 'proxy_endpoint': '', 'proxy_consent_version': ''}
PAC_FIELDS = {'pac_name', 'pac_sha256', 'pac_id'}
SYSTEM_PAC_FIELDS = {'system_pac_config_id', 'pac_id'}
WPAD_FIELDS = {'wpad_config_id', 'pac_id'}
ALL_PAC_FIELDS = PAC_FIELDS | SYSTEM_PAC_FIELDS | WPAD_FIELDS
ALL_ROUTE_FIELDS = ALL_PAC_FIELDS | {workspace_proxy_auth.FIELD}
PAC_SCOPES = {3: ('pac', PAC_CONSENT), 6: ('pac_url', pac_url.CONSENT),
              7: ('pac_url_dns', pac_url.DNS_CONSENT),
              9: ('pac_domain', pac_domain.CONSENT), 10: ('pac_domain_dns', pac_domain.DNS_CONSENT)}
PAC_DISCLOSURE = ('只导入你信任的本地PAC文件（UTF-8，最多64KiB）。Windows会执行脚本，原生DNS函数可能解析域名。'
    '脚本只收到HTTPS根地址和域名，不含职位路径、关键词、Cookie或密码。仅接受DIRECT、匿名本机PROXY和明确SOCKS5；'
    '完整检查返回项，只采用第一项，失败不自动直连。不会自动读取系统PAC、发现WPAD或修改系统代理。')
PROXY_CONFLICT = '已有应用专用代理或代理凭据环境设置；请先清除冲突或选择自动模式。不会把这些凭据交给工作区的新入口。'
DISCLOSURE = ('仅当系统DNS为映射地址时，使用Cloudflare加密解析目标域名；解析服务可看到域名和网络出口，'
              '不发送职位正文、查询参数、Cookie、密码或个人材料。不修改系统DNS，不要求关闭VPN/TUN。'
              '不开启时维持系统解析；不是所有VPN或企业网络的兼容保证。')


PUBLIC_DNS_DISCLOSURE = ('所访问的公共域名直接交给Cloudflare加密解析，不先使用本机目标DNS；解析服务可看到域名和网络出口。'
    '请求沿岗位原选代理或系统路线，不发送职位正文、URL路径与查询参数、Cookie、密码或个人材料。'
    '不适用于企业内部域名或分区DNS；不保证兼容企业策略。失败时停止，不改用系统DNS或其他解析商。'
    '这是单独授权的范围，不沿用仅修复映射地址的许可；不修改系统DNS或VPN。')


def _dns_consent(mode):
    return PUBLIC_DNS_CONSENT if mode == 'public_doh' else CONSENT if mode == 'fake_ip_doh' else ''


def _proxy(mode, endpoint):
    try:
        if not isinstance(mode, str) or mode not in PROXY_MODES | workspace_proxy_auth.MODES.keys() or not isinstance(endpoint, str):
            raise ValueError()
        if mode == 'auto':
            if endpoint != '':
                raise ValueError()
            return None
        if mode == 'http_digest_auth':
            return LoopbackDigestProxy.from_url(endpoint)
        return PROXY_PARSERS[workspace_proxy_auth.MODES.get(mode, mode)].from_url(endpoint)
    except (ValueError, TypeError, LocalProxyError):
        raise InputError('请按所选模式填写匿名HTTP或SOCKS5代理及端口：本机模式只接受loopback，宿主机模式只接受RFC1918 IPv4。不能包含账号密码、路径、其他远程地址或代理DNS。') from None


def _endpoint(mode, proxy):
    if proxy is None:
        return ''
    host = '[' + proxy.host + ']' if ':' in proxy.host else proxy.host
    return f'{workspace_proxy_auth.MODES.get(mode, mode).removeprefix("vm_")}://{host}:{proxy.port}'


def _proxy_consent(mode):
    if mode in workspace_proxy_auth.MODES:
        return workspace_proxy_auth.CONSENT
    return '' if mode == 'auto' else VM_CONSENT if mode.startswith('vm_') else PROXY_CONSENT


def _environment_conflict():
    return bool(os.environ.get('VIBE_RADAR_HTTP_PROXY', '').strip()
                or os.environ.get('VIBE_RADAR_SOCKS_PROXY', '').strip() or credentials_configured())


def read_settings(workspace):
    path = workspace.root / 'network-preferences.json'
    if path.is_symlink():
        raise InputError('网络偏好文件不能使用符号链接。')
    default = {'schema_version': 1, 'mode': 'system', 'revision': 0, 'consent_version': '', 'updated_at': ''}
    if not path.exists():
        return default
    try:
        if path.stat().st_size > 4096:
            raise ValueError
        value = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] not in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10):
            raise ValueError
        expected = set(default) | (set(PROXY_DEFAULT) if value['schema_version'] >= 2 else set())
        if value['schema_version'] in PAC_SCOPES:
            expected |= PAC_FIELDS
        if value['schema_version'] == 4:
            expected |= SYSTEM_PAC_FIELDS
        if value['schema_version'] == 5:
            expected |= WPAD_FIELDS
        if value['schema_version'] == 8:
            expected.add(workspace_proxy_auth.FIELD)
        if (set(value) != expected
                or value['mode'] not in MODES or type(value['revision']) is not int
                or not 1 <= value['revision'] < 2**31 or not isinstance(value['updated_at'], str)
                or value['consent_version'] != _dns_consent(value['mode'])):
            raise ValueError
        if value['schema_version'] == 2:
            if value['proxy_mode'] not in PROXY_MODES:
                raise ValueError
            proxy = _proxy(value['proxy_mode'], value['proxy_endpoint'])
            if (value['proxy_endpoint'] != _endpoint(value['proxy_mode'], proxy)
                    or value['proxy_consent_version'] != _proxy_consent(value['proxy_mode'])):
                raise ValueError
        elif value['schema_version'] == 8:
            if value['proxy_mode'] not in workspace_proxy_auth.MODES:
                raise ValueError
            proxy = _proxy(value['proxy_mode'], value['proxy_endpoint'])
            if (value['proxy_endpoint'] != _endpoint(value['proxy_mode'], proxy)
                    or value['proxy_consent_version'] != workspace_proxy_auth.CONSENT):
                raise ValueError
            workspace_proxy_auth.validate_envelope(value[workspace_proxy_auth.FIELD])
        elif value['schema_version'] in PAC_SCOPES:
            pac_mode, pac_consent = PAC_SCOPES[value['schema_version']]
            if (value['proxy_mode'] != pac_mode or value['proxy_endpoint'] != ''
                    or value['proxy_consent_version'] != pac_consent
                    or not _pac_name(value['pac_name'])
                    or not isinstance(value['pac_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['pac_sha256'])
                    or not isinstance(value['pac_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['pac_id'])):
                raise ValueError
        elif value['schema_version'] == 4:
            if (value['proxy_mode'] != 'system_pac' or value['proxy_endpoint'] != ''
                    or value['proxy_consent_version'] != system_pac.CONSENT
                    or not isinstance(value['system_pac_config_id'], str)
                    or not re.fullmatch('[0-9a-f]{64}', value['system_pac_config_id'])
                    or not isinstance(value['pac_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['pac_id'])):
                raise ValueError
        elif value['schema_version'] == 5:
            if (value['proxy_mode'] != 'wpad' or value['proxy_endpoint'] != ''
                    or value['proxy_consent_version'] != wpad.CONSENT
                    or not isinstance(value['wpad_config_id'], str)
                    or not re.fullmatch('[0-9a-f]{64}', value['wpad_config_id'])
                    or not isinstance(value['pac_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['pac_id'])):
                raise ValueError
        return value
    except (ValueError, TypeError, OSError, InputError):
        raise InputError('网络偏好文件无效或版本不兼容；已停止联网，离线数据不受影响。') from None


def capture_policy(workspace, *, settings=None):
    if settings is None:
        settings = read_settings(workspace)
    mode = settings.get('proxy_mode', 'auto')
    if mode == 'wpad':
        def permitted():
            current = read_settings(workspace)
            return (not workspace._network_closed.is_set()
                    and current.get('pac_id') == settings['pac_id']
                    and current.get('wpad_config_id') == settings['wpad_config_id']
                    and current.get('proxy_mode') == 'wpad' and not _environment_conflict()
                    and workspace.wpad_discovery.matches(settings['wpad_config_id']))
        policy = NetworkPolicy('explicit_workspace',
            pac=wpad.WpadSnapshot(settings['wpad_config_id'], permitted), pac_id=settings['pac_id'],
            error='workspace_proxy_environment_conflict' if _environment_conflict() else
                'wpad_unavailable' if not wpad_native.available() else
                None if pac_native.available() else 'pac_unavailable')
    elif mode == 'system_pac':
        def permitted():
            current = read_settings(workspace)
            return (current.get('pac_id') == settings['pac_id']
                    and current.get('system_pac_config_id') == settings['system_pac_config_id']
                    and current.get('proxy_mode') == 'system_pac' and not _environment_conflict())
        try:
            source = system_pac.current_source()
            if source.config_id != settings['system_pac_config_id']:
                raise LocalProxyError('system_pac_changed')
            policy = NetworkPolicy('explicit_workspace',
                pac=system_pac.SystemPacSnapshot(source, permitted), pac_id=settings['pac_id'],
                error='workspace_proxy_environment_conflict' if _environment_conflict() else
                    None if pac_native.available() else 'pac_unavailable')
        except LocalProxyError as exc:
            policy = NetworkPolicy('explicit_workspace', error=exc.code, pac_id=settings['pac_id'])
    elif mode in {'pac', 'pac_url', 'pac_url_dns', 'pac_domain', 'pac_domain_dns'}:
        domain_scope = mode in {'pac_domain', 'pac_domain_dns'}
        url_scope = mode in {'pac_url', 'pac_url_dns'}
        try:
            source = _pac_source(workspace, settings)
        except InputError:
            return NetworkPolicy('explicit_workspace', error='pac_file_invalid', pac_id=settings['pac_id'],
                                 pac_url_mode=url_scope, pac_domain_mode=domain_scope)
        def permitted():
            current = read_settings(workspace)
            return (current.get('pac_id') == settings['pac_id']
                    and current.get('pac_sha256') == settings['pac_sha256']
                    and current.get('proxy_mode') == mode
                    and (mode == 'pac' or not workspace._network_closed.is_set())
                    and not _environment_conflict()
                    and _pac_source(workspace, current) == source)
        available = pac_url_worker.available() if url_scope or domain_scope else pac_native.available()
        snapshot = (pac_domain.DomainPacSnapshot(source, permitted, dns_helpers=mode == 'pac_domain_dns')
                    if domain_scope else pac_url.UrlPacSnapshot(source, permitted, dns_helpers=mode == 'pac_url_dns')
                    if url_scope else PacSnapshot(source, permitted))
        policy = NetworkPolicy('explicit_workspace', pac=snapshot, pac_id=settings['pac_id'], pac_url_mode=url_scope,
            pac_domain_mode=domain_scope,
            error='workspace_proxy_environment_conflict' if _environment_conflict() else
                  None if available else 'pac_domain_unavailable' if domain_scope else
                  'pac_url_unavailable' if url_scope else 'pac_unavailable')
    elif mode in workspace_proxy_auth.MODES:
        try:
            if _environment_conflict():
                raise LocalProxyError('workspace_proxy_environment_conflict')
            proxy = _proxy(mode, settings['proxy_endpoint'])
            credentials = workspace_proxy_auth.restore(workspace.root, mode, settings['proxy_endpoint'],
                                                       settings[workspace_proxy_auth.FIELD])
            # Hash the randomized protected envelope, never a plaintext secret
            # or stable password verifier. Restore validated it before binding.
            envelope_id = hashlib.sha256(json.dumps(settings[workspace_proxy_auth.FIELD],
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            policy = NetworkPolicy('explicit_workspace', replace(proxy, credentials=credentials),
                                   credential_scope_id=envelope_id)
        except LocalProxyError as exc:
            policy = NetworkPolicy('explicit_workspace', error=exc.code)
    elif (proxy := _proxy(mode, settings.get('proxy_endpoint', ''))) is None:
        policy = NetworkPolicy.capture()
    elif _environment_conflict():
        policy = NetworkPolicy('explicit_workspace', error='workspace_proxy_environment_conflict')
    else:
        policy = NetworkPolicy('explicit_workspace', proxy)
    return replace(policy, encrypted_dns=settings['mode'] != 'system',
                   public_dns=settings['mode'] == 'public_doh', resolver=workspace.dns_resolver)


def state(workspace):
    settings = read_settings(workspace)
    public = {k: v for k, v in settings.items() if k != workspace_proxy_auth.FIELD}
    policy = capture_policy(workspace, settings=settings)
    return {**PROXY_DEFAULT, **public, 'disclosure': DISCLOSURE, 'provider': 'Cloudflare',
            'proxy_auth_available': workspace_proxy_auth.available(),
            'proxy_auth_saved': settings.get(workspace_proxy_auth.FIELD) is not None,
            'proxy_auth_disclosure': workspace_proxy_auth.DISCLOSURE,
            'proxy_auth_message': workspace_proxy_auth.MESSAGES.get(policy.error, ''),
            'dns_disclosures': {'fake_ip_doh': DISCLOSURE, 'public_doh': PUBLIC_DNS_DISCLOSURE},
            'pac_available': pac_native.available(), 'pac_disclosure': PAC_DISCLOSURE,
            'pac_url_available': pac_url_worker.available(), 'pac_url_disclosure': pac_url.DISCLOSURE,
            'pac_url_dns_disclosure': pac_url.DNS_DISCLOSURE,
            'pac_domain_available': pac_url_worker.available(), 'pac_domain_disclosure': pac_domain.DISCLOSURE,
            'pac_domain_dns_disclosure': pac_domain.DNS_DISCLOSURE,
            'system_pac': system_pac.preview(), 'system_pac_disclosure': system_pac.DISCLOSURE,
            'wpad': workspace.wpad_discovery.state(settings['revision']), 'wpad_disclosure': wpad.DISCLOSURE,
            'network_tested': False, 'policy': policy.describe()}


def save(workspace, data):
    if (not isinstance(data, dict) or set(data) != {'mode','revision','consent'}
            or not isinstance(data['mode'], str) or data['mode'] not in MODES or type(data['revision']) is not int
            or type(data['consent']) is not bool
            or (data['mode'] != 'system' and data['consent'] is not True)):
        raise InputError('请明确同意加密解析说明；不接受地址、凭据或额外参数。')
    from .collection import writer_lock
    with writer_lock(workspace.root):
        previous = read_settings(workspace)
        if previous['revision'] >= 2**31-1:
            raise InputError('网络偏好修订号已达到上限；未覆盖原设置。')
        if previous['revision'] != data['revision']:
            raise InputError('网络偏好已被其他页面更新，请刷新后重试。')
        value = {**previous, 'mode': data['mode'], 'revision': previous['revision']+1,
                 'consent_version': _dns_consent(data['mode']), 'updated_at': utc_now()}
        atomic_json(workspace.root/'network-preferences.json', value)
    workspace.dns_resolver.clear()
    return {**state(workspace), 'message': '已保存。新公开查询、高级采集的下一步和新浏览器会话采用此设置；已有浏览器会话不偷偷更换网络。关闭后不再发起新的加密DNS请求。'}


def save_proxy(workspace, data):
    if (not isinstance(data, dict) or set(data) != {'mode', 'endpoint', 'revision', 'consent'}
            or not isinstance(data['mode'], str) or data['mode'] not in PROXY_MODES
            or type(data['revision']) is not int or type(data['consent']) is not bool
            or (data['mode'] != 'auto' and data['consent'] is not True)):
        raise InputError('请确认本工作区匿名代理及修订号；不接受凭据或额外参数。')
    proxy = _proxy(data['mode'], data['endpoint'])
    if proxy is not None and _environment_conflict():
        raise InputError(PROXY_CONFLICT)
    from .collection import writer_lock
    with writer_lock(workspace.root):
        previous = read_settings(workspace)
        if previous['revision'] >= 2**31-1 or previous['revision'] != data['revision']:
            raise InputError('网络偏好已更新或修订号达到上限，请刷新后重试；原设置保留。')
        value = {**{k:v for k,v in previous.items() if k not in ALL_ROUTE_FIELDS},
                 'schema_version': 2, 'revision': previous['revision'] + 1,
                 'updated_at': utc_now(), 'proxy_mode': data['mode'],
                 'proxy_endpoint': _endpoint(data['mode'], proxy),
                 'proxy_consent_version': _proxy_consent(data['mode'])}
        atomic_json(workspace.root/'network-preferences.json', value)
    workspace.dns_resolver.clear()
    return {**state(workspace), 'message': '已保存本工作区代理，未进行联网测试。请停止并重开已有采集会话以采用新设置；其他工作区和系统设置未修改。'}


def save_proxy_auth(workspace, data):
    if (not isinstance(data, dict) or set(data) != {'mode', 'endpoint', 'username', 'password', 'revision', 'consent'}
            or type(data['revision']) is not int or data['consent'] is not True
            or not isinstance(data['mode'], str) or data['mode'] not in workspace_proxy_auth.MODES):
        raise InputError('请明确选择本机认证代理并同意Windows加密保存；不接受额外参数。')
    proxy = _proxy(data['mode'], data['endpoint'])
    endpoint = _endpoint(data['mode'], proxy)
    if _environment_conflict():
        raise InputError(PROXY_CONFLICT)
    from .collection import writer_lock
    with writer_lock(workspace.root):
        previous = read_settings(workspace)
        if previous['revision'] >= 2**31-1 or previous['revision'] != data['revision']:
            raise InputError('网络偏好已更新或修订号达到上限，请刷新后重新输入；原设置保留。')
        try:
            envelope = workspace_proxy_auth.seal(workspace.root, data['mode'], endpoint,
                                                data['username'], data['password'])
        except LocalProxyError as exc:
            raise InputError({**AUTH_MESSAGES, **workspace_proxy_auth.MESSAGES}[exc.code]) from None
        value = {**{k: v for k, v in previous.items() if k not in ALL_ROUTE_FIELDS},
                 'schema_version': 8, 'revision': previous['revision']+1, 'updated_at': utc_now(),
                 'proxy_mode': data['mode'], 'proxy_endpoint': endpoint,
                 'proxy_consent_version': workspace_proxy_auth.CONSENT, workspace_proxy_auth.FIELD: envelope}
        atomic_json(workspace.root/'network-preferences.json', value)
    workspace.dns_resolver.clear()
    return {**state(workspace), 'message': '已加密保存本工作区代理凭据；未联网测试。请停止并重开已有采集会话以采用新凭据，原任务和数据保留。'}


def clear_proxy_auth(workspace, data):
    if (not isinstance(data, dict) or set(data) != {'revision', 'consent'}
            or type(data['revision']) is not int or data['consent'] is not True):
        raise InputError('请明确确认清除当前工作区代理凭据及修订号。')
    from .collection import writer_lock
    with writer_lock(workspace.root):
        previous = read_settings(workspace)
        if (previous.get('proxy_mode') not in workspace_proxy_auth.MODES
                or previous['revision'] >= 2**31-1 or previous['revision'] != data['revision']):
            raise InputError('网络偏好已更新或没有工作区代理凭据，请刷新后重试；原设置保留。')
        value = {**previous, 'revision': previous['revision']+1, 'updated_at': utc_now(),
                 workspace_proxy_auth.FIELD: None}
        atomic_json(workspace.root/'network-preferences.json', value)
    workspace.dns_resolver.clear()
    return {**state(workspace), 'message': '已清除当前保存的代理凭据，保持所选入口并停止新连接；不会匿名连接或直连。请关闭已有会话；工作区备份中的旧加密副本需按原备份管理。'}


def _pac_name(name):
    return (isinstance(name, str) and 1 <= len(name) <= 128 and name.lower().endswith(('.pac', '.js'))
            and not any(ord(c) < 32 or ord(c) == 127 or c in '/\\:*?<>|"' for c in name))


def _pac_path(workspace, sha):
    parent = workspace.root/'network-pac'
    path = parent/(sha + '.js')
    if parent.is_symlink() or parent.resolve() != parent or path.is_symlink():
        raise InputError('PAC副本不能使用符号链接或重定向目录；未修改网络设置。')
    return path


def _pac_source(workspace, settings):
    try:
        path = _pac_path(workspace, settings['pac_sha256'])
        with path.open('rb') as stream:
            raw = stream.read(pac_native.MAX_SCRIPT + 1)
        source = raw.decode('utf-8')
        validate_script(source)
        if hashlib.sha256(raw).hexdigest() != settings['pac_sha256']: raise ValueError()
        return source
    except (OSError, ValueError, UnicodeError):
        raise InputError('PAC副本缺失或校验失败，已停止联网；可重新导入或选择自动模式。') from None


def save_pac(workspace, data, *, full_url=False, dns_helpers=False, portable_domain=False):
    if (any(type(flag) is not bool for flag in (full_url, dns_helpers, portable_domain))
            or (full_url and portable_domain) or (dns_helpers and not (full_url or portable_domain))):
        raise InputError('请单独选择PAC执行范围及DNS助手许可。')
    if (not isinstance(data, dict) or set(data) != {'name', 'script', 'revision', 'consent'}
            or type(data['revision']) is not int or data['consent'] is not True
            or not _pac_name(data['name'])):
        raise InputError('请选择本地PAC文件并明确同意执行说明；不接受远程URL或额外参数。')
    available = pac_url_worker.available() if full_url or portable_domain else pac_native.available()
    if not available:
        raise InputError(pac_domain.MESSAGES['pac_domain_unavailable'] if portable_domain else
                         pac_url.MESSAGES['pac_url_unavailable'] if full_url else PAC_MESSAGES['pac_unavailable'])
    if _environment_conflict(): raise InputError(PROXY_CONFLICT)
    try: raw = validate_script(data['script'])
    except ValueError: raise InputError('PAC必须为非空UTF-8文本且不超过64KiB，不能包含空字符。') from None
    sha = hashlib.sha256(raw).hexdigest()
    from .collection import writer_lock
    with writer_lock(workspace.root):
        previous = read_settings(workspace)
        if previous['revision'] >= 2**31-1 or previous['revision'] != data['revision']:
            raise InputError('网络偏好已更新或修订号达到上限，请刷新后重试；原设置保留。')
        path = _pac_path(workspace, sha)
        # Content-addressed, frozen copy first. A failed preference write leaves
        # at most an unused copy, never settings pointing to a partial script.
        if path.exists():
            if _pac_source(workspace, {'pac_sha256': sha}) != data['script']:
                raise InputError('PAC副本冲突；原设置保留。')
        else:
            atomic_text(path, data['script'])
        schema = (10 if dns_helpers else 9) if portable_domain else 7 if dns_helpers else 6 if full_url else 3
        pac_mode, pac_consent = PAC_SCOPES[schema]
        value = {**{k:v for k,v in previous.items() if k not in ALL_ROUTE_FIELDS},
            'schema_version': schema, 'revision': previous['revision']+1,
            'updated_at': utc_now(), 'proxy_mode': pac_mode, 'proxy_endpoint': '',
            'proxy_consent_version': pac_consent, 'pac_name': data['name'],
            'pac_sha256': sha, 'pac_id': secrets.token_hex(16)}
        atomic_json(workspace.root/'network-preferences.json', value)
    workspace.dns_resolver.clear()
    return {**state(workspace), 'message': '已保存PAC副本及许可；尚未执行或联网测试。新会话采用此脚本，已有会话请停止并重开。'}


def save_system_pac(workspace, data):
    if (not isinstance(data, dict) or set(data) != {'config_id', 'revision', 'consent'}
            or type(data['revision']) is not int or data['consent'] is not True
            or not isinstance(data['config_id'], str) or not re.fullmatch('[0-9a-f]{64}', data['config_id'])):
        raise InputError('请核对当前系统PAC来源并单独同意；不接受地址、凭据或额外参数。')
    if not pac_native.available(): raise InputError(PAC_MESSAGES['pac_unavailable'])
    if _environment_conflict(): raise InputError(PROXY_CONFLICT)
    from .collection import writer_lock
    with writer_lock(workspace.root):
        previous = read_settings(workspace)
        if previous['revision'] >= 2**31-1 or previous['revision'] != data['revision']:
            raise InputError('网络偏好已更新或修订号达到上限，请刷新后重试；原设置保留。')
        try: source = system_pac.current_source()
        except LocalProxyError as exc: raise InputError(system_pac.MESSAGES[exc.code]) from None
        if source.config_id != data['config_id']:
            raise InputError(system_pac.MESSAGES['system_pac_changed'])
        value = {**{k:v for k,v in previous.items() if k not in ALL_ROUTE_FIELDS},
            'schema_version': 4, 'revision': previous['revision']+1, 'updated_at': utc_now(),
            'proxy_mode': 'system_pac', 'proxy_endpoint': '', 'proxy_consent_version': system_pac.CONSENT,
            'system_pac_config_id': source.config_id, 'pac_id': secrets.token_hex(16)}
        atomic_json(workspace.root/'network-preferences.json', value)
    workspace.dns_resolver.clear()
    return {**state(workspace), 'message': '已许可当前系统PAC来源；未下载或执行。实际新会话会读取一次。撤销时选择自动发现并保存。'}


def discover_wpad(workspace, data):
    if (not isinstance(data, dict) or set(data) != {'revision', 'consent'}
            or type(data['revision']) is not int or data['consent'] is not True):
        raise InputError('请单独同意向当前网络发现PAC地址；不接受地址或额外参数。')
    if not wpad_native.available(): raise InputError(wpad.MESSAGES['wpad_unavailable'])
    if not pac_native.available(): raise InputError(PAC_MESSAGES['pac_unavailable'])
    if _environment_conflict(): raise InputError(PROXY_CONFLICT)
    settings = read_settings(workspace)
    if settings['revision'] != data['revision'] or settings['revision'] >= 2**31-1:
        raise InputError('网络偏好已更新或修订号达到上限，请刷新后重试；原设置保留。')
    def permitted():
        return (read_settings(workspace)['revision'] == data['revision']
                and not workspace._network_closed.is_set() and not _environment_conflict())
    try:
        workspace.wpad_discovery.start(data['revision'], permitted)
    except LocalProxyError as exc:
        raise InputError({**PAC_MESSAGES, **wpad.MESSAGES}.get(exc.code, wpad.MESSAGES['wpad_failed'])) from None
    return state(workspace)


def cancel_wpad(workspace, data):
    if not isinstance(data, dict) or data:
        raise InputError('取消发现不接受额外参数。')
    workspace.wpad_discovery.cancel()
    return state(workspace)


def save_wpad(workspace, data):
    if (not isinstance(data, dict) or set(data) != {'candidate', 'revision', 'consent'}
            or type(data['revision']) is not int or data['consent'] is not True
            or not isinstance(data['candidate'], str) or not re.fullmatch('[0-9a-f]{32}', data['candidate'])):
        raise InputError('请核对来源并单独同意信任；不接受地址、凭据或额外参数。')
    if not wpad_native.available(): raise InputError(wpad.MESSAGES['wpad_unavailable'])
    if not pac_native.available(): raise InputError(PAC_MESSAGES['pac_unavailable'])
    if _environment_conflict(): raise InputError(PROXY_CONFLICT)
    from .collection import writer_lock
    with writer_lock(workspace.root):
        previous = read_settings(workspace)
        if previous['revision'] >= 2**31-1 or previous['revision'] != data['revision']:
            raise InputError('网络偏好已更新或修订号达到上限，请刷新后重试；原设置保留。')
        def commit(config_id):
            value = {**{k:v for k,v in previous.items() if k not in ALL_ROUTE_FIELDS},
                'schema_version': 5, 'revision': previous['revision']+1, 'updated_at': utc_now(),
                'proxy_mode': 'wpad', 'proxy_endpoint': '', 'proxy_consent_version': wpad.CONSENT,
                'wpad_config_id': config_id, 'pac_id': secrets.token_hex(16)}
            atomic_json(workspace.root/'network-preferences.json', value)
        try:
            workspace.wpad_discovery.confirm(data['revision'], data['candidate'], commit)
        except LocalProxyError as exc:
            raise InputError(wpad.MESSAGES.get(exc.code, PAC_MESSAGES['pac_revoked'])) from None
    workspace.dns_resolver.clear()
    return {**state(workspace), 'message': '已确认WPAD完整来源；尚未下载或执行。新会话首次使用时重新发现并核对来源，再读取脚本。选择自动模式并保存可撤销。'}


def check_pac(workspace, data):
    if not isinstance(data, dict) or set(data) != {'revision'} or type(data['revision']) is not int:
        raise InputError('仅接受当前网络偏好修订号。')
    settings = read_settings(workspace)
    if settings['revision'] != data['revision'] or settings.get('proxy_mode') not in {'pac', 'pac_url', 'pac_url_dns', 'pac_domain', 'pac_domain_dns', 'system_pac', 'wpad'}:
        raise InputError('PAC设置已改变或尚未导入，请刷新后重试。')
    policy = capture_policy(workspace, settings=settings)
    try:
        selected = policy.for_request('https://pac-check.invalid/path?probe=explicit').for_host('pac-check.invalid')
    except LocalProxyError as exc:
        return {'passed': False, 'code': exc.code, 'target_requested': False,
                'message': {**pac_domain.MESSAGES, **system_pac.MESSAGES, **wpad.MESSAGES}.get(exc.code, PROXY_CONFLICT)}
    finally:
        policy.close()
    return {'passed': True, 'code': 'pac_evaluated', 'transport': policy.transport_name(selected),
            'target_requested': False,
            'message': '脚本对内置检查域名返回了有效路线；未连接代理或目标。不代表招聘域名的选路、登录或取数已通过。'}


DNS_MESSAGES = {
    **workspace_proxy_auth.MESSAGES,
    **VM_MESSAGES,
    **pac_domain.MESSAGES,
    **system_pac.MESSAGES,
    **wpad.MESSAGES,
    'workspace_proxy_environment_conflict': PROXY_CONFLICT,
    'non_public_address': '系统返回非公网地址。若网络检查显示映射地址，可在“网络自动适配”阅读说明并启用加密解析；无需改系统DNS或关闭VPN。其他私网地址仍会拒绝。',
    'encrypted_dns_disabled': '当前会话的加密解析许可已撤销或范围已改变；请停止并重开会话以采用已保存的设置。原任务与数据保留。',
    'encrypted_dns_invalid_response': '加密解析响应未通过校验，已停止；未把异常地址用于取数。',
    'encrypted_dns_non_public_answer': '加密解析仍返回非公网地址，已停止；不会放行内网或映射地址。',
    'encrypted_dns_tls_failed': '解析服务证书或TLS校验失败，已停止；不会关闭校验或切换其他解析商。',
    'encrypted_dns_route_failed': '按当前网络路线无法连接解析服务，已停止；不会绕开所选代理直连。',
    'encrypted_dns_unavailable': '加密解析暂不可用；原数据保留，网络恢复后可再次确认，不无限重试。',
    'encrypted_dns_timeout': '加密解析超时，已停止当前动作并保留任务。',
    'encrypted_dns_http_rejected': '解析服务拒绝请求或要求等待，已停止；不会跟随重定向或更换解析商。',
    'encrypted_dns_refused': '解析服务没有接受该域名查询，未发送目标网站请求。',
    'encrypted_dns_name_not_found': '解析服务未找到该域名，未改用其他来源冒充成功。',
    'encrypted_dns_empty_answer': '解析服务未提供可用公网地址，未发送目标网站请求。',
    'encrypted_dns_expired_answer': '解析结果在完成前已过期，已停止；不会使用过期地址。',
    'encrypted_dns_cooldown': '解析暂处于保护等待，请稍后再继续；增加采集预算无效。',
    'encrypted_dns_budget': '本机本轮解析请求已达保护上限；保留任务，稍后继续。',
    'encrypted_dns_clock_rollback': '解析时钟异常，已丢弃缓存并停止；未使用过期结果。',
}
