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
from .vm_proxy import VmHTTPProxy, VmSocks5Proxy, ERROR_MESSAGES as VM_MESSAGES
from .proxy_credentials import configured as credentials_configured
from .html_parser import _unique_object
from .utils import atomic_json, atomic_text, utc_now
from .workspace import InputError
from .pac import PacSnapshot, CONSENT as PAC_CONSENT, MESSAGES as PAC_MESSAGES, validate_script
from . import pac_native

CONSENT = 'cloudflare-doh-v1'
MODES = {'system', 'fake_ip_doh'}
PROXY_CONSENT = 'workspace-anonymous-loopback-v1'
VM_CONSENT = 'workspace-anonymous-vm-host-v1'
PROXY_PARSERS = {'http': LoopbackProxy, 'socks5': LoopbackSocks5,
                 'vm_http': VmHTTPProxy, 'vm_socks5': VmSocks5Proxy}
PROXY_MODES = {'auto', *PROXY_PARSERS}
PROXY_DEFAULT = {'proxy_mode': 'auto', 'proxy_endpoint': '', 'proxy_consent_version': ''}
PAC_FIELDS = {'pac_name', 'pac_sha256', 'pac_id'}
PAC_DISCLOSURE = ('只导入你信任的本地PAC文件（UTF-8，最多64KiB）。Windows会执行脚本，原生DNS函数可能解析域名。'
    '脚本只收到HTTPS根地址和域名，不含职位路径、关键词、Cookie或密码。仅接受DIRECT、匿名本机PROXY和明确SOCKS5；'
    '完整检查返回项，只采用第一项，失败不自动直连。不会自动读取系统PAC、发现WPAD或修改系统代理。')
PROXY_CONFLICT = '已有应用专用代理或代理凭据环境设置；请先清除冲突或选择自动模式。不会把这些凭据交给工作区的新入口。'
DISCLOSURE = ('仅当系统DNS为映射地址时，使用Cloudflare加密解析目标域名；解析服务可看到域名和网络出口，'
              '不发送职位正文、查询参数、Cookie、密码或个人材料。不修改系统DNS，不要求关闭VPN/TUN。'
              '不开启时维持系统解析；不是所有VPN或企业网络的兼容保证。')


def _proxy(mode, endpoint):
    try:
        if not isinstance(mode, str) or mode not in PROXY_MODES or not isinstance(endpoint, str):
            raise ValueError()
        if mode == 'auto':
            if endpoint != '':
                raise ValueError()
            return None
        return PROXY_PARSERS[mode].from_url(endpoint)
    except (ValueError, TypeError, LocalProxyError):
        raise InputError('请按所选模式填写匿名HTTP或SOCKS5代理及端口：本机模式只接受loopback，宿主机模式只接受RFC1918 IPv4。不能包含账号密码、路径、其他远程地址或代理DNS。') from None


def _endpoint(mode, proxy):
    if proxy is None:
        return ''
    host = '[' + proxy.host + ']' if ':' in proxy.host else proxy.host
    return f'{mode.removeprefix("vm_")}://{host}:{proxy.port}'


def _proxy_consent(mode):
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
        if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] not in (1, 2, 3):
            raise ValueError
        expected = set(default) | (set(PROXY_DEFAULT) if value['schema_version'] >= 2 else set())
        if value['schema_version'] == 3:
            expected |= PAC_FIELDS
        if (set(value) != expected
                or value['mode'] not in MODES or type(value['revision']) is not int
                or not 1 <= value['revision'] < 2**31 or not isinstance(value['updated_at'], str)
                or value['consent_version'] != (CONSENT if value['mode'] == 'fake_ip_doh' else '')):
            raise ValueError
        if value['schema_version'] == 2:
            proxy = _proxy(value['proxy_mode'], value['proxy_endpoint'])
            if (value['proxy_endpoint'] != _endpoint(value['proxy_mode'], proxy)
                    or value['proxy_consent_version'] != _proxy_consent(value['proxy_mode'])):
                raise ValueError
        elif value['schema_version'] == 3:
            if (value['proxy_mode'] != 'pac' or value['proxy_endpoint'] != ''
                    or value['proxy_consent_version'] != PAC_CONSENT
                    or not _pac_name(value['pac_name'])
                    or not isinstance(value['pac_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', value['pac_sha256'])
                    or not isinstance(value['pac_id'], str) or not re.fullmatch('[0-9a-f]{32}', value['pac_id'])):
                raise ValueError
        return value
    except (ValueError, TypeError, OSError, InputError):
        raise InputError('网络偏好文件无效或版本不兼容；已停止联网，离线数据不受影响。') from None


def capture_policy(workspace, *, settings=None):
    if settings is None:
        settings = read_settings(workspace)
    mode = settings.get('proxy_mode', 'auto')
    if mode == 'pac':
        try:
            source = _pac_source(workspace, settings)
        except InputError:
            return NetworkPolicy('explicit_workspace', error='pac_file_invalid', pac_id=settings['pac_id'])
        def permitted():
            current = read_settings(workspace)
            return (current.get('pac_id') == settings['pac_id']
                    and current.get('pac_sha256') == settings['pac_sha256']
                    and current.get('proxy_mode') == 'pac'
                    and not _environment_conflict()
                    and _pac_source(workspace, current) == source)
        policy = NetworkPolicy('explicit_workspace', pac=PacSnapshot(source, permitted), pac_id=settings['pac_id'],
            error='workspace_proxy_environment_conflict' if _environment_conflict() else
                  None if pac_native.available() else 'pac_unavailable')
    elif (proxy := _proxy(mode, settings.get('proxy_endpoint', ''))) is None:
        policy = NetworkPolicy.capture()
    elif _environment_conflict():
        policy = NetworkPolicy('explicit_workspace', error='workspace_proxy_environment_conflict')
    else:
        policy = NetworkPolicy('explicit_workspace', proxy)
    return replace(policy, encrypted_dns=settings['mode'] == 'fake_ip_doh',
                   resolver=workspace.dns_resolver)


def state(workspace):
    settings = read_settings(workspace)
    return {**PROXY_DEFAULT, **settings, 'disclosure': DISCLOSURE, 'provider': 'Cloudflare',
            'pac_available': pac_native.available(), 'pac_disclosure': PAC_DISCLOSURE,
            'network_tested': False, 'policy': capture_policy(workspace, settings=settings).describe()}


def save(workspace, data):
    if (not isinstance(data, dict) or set(data) != {'mode','revision','consent'}
            or not isinstance(data['mode'], str) or data['mode'] not in MODES or type(data['revision']) is not int
            or type(data['consent']) is not bool
            or (data['mode'] == 'fake_ip_doh' and data['consent'] is not True)):
        raise InputError('请明确同意加密解析说明；不接受地址、凭据或额外参数。')
    from .collection import writer_lock
    with writer_lock(workspace.root):
        previous = read_settings(workspace)
        if previous['revision'] >= 2**31-1:
            raise InputError('网络偏好修订号已达到上限；未覆盖原设置。')
        if previous['revision'] != data['revision']:
            raise InputError('网络偏好已被其他页面更新，请刷新后重试。')
        value = {**previous, 'mode': data['mode'], 'revision': previous['revision']+1,
                 'consent_version': CONSENT if data['mode'] == 'fake_ip_doh' else '', 'updated_at': utc_now()}
        atomic_json(workspace.root/'network-preferences.json', value)
    workspace.dns_resolver.clear()
    return {**state(workspace), 'message': '已保存。新公开查询、高级采集的下一步和新浏览器会话采用此设置；已有浏览器会话不偷偷更换网络。关闭后不再发起新的加密DNS请求。'}


def save_proxy(workspace, data):
    if (not isinstance(data, dict) or set(data) != {'mode', 'endpoint', 'revision', 'consent'}
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
        value = {**{k:v for k,v in previous.items() if k not in PAC_FIELDS},
                 'schema_version': 2, 'revision': previous['revision'] + 1,
                 'updated_at': utc_now(), 'proxy_mode': data['mode'],
                 'proxy_endpoint': _endpoint(data['mode'], proxy),
                 'proxy_consent_version': _proxy_consent(data['mode'])}
        atomic_json(workspace.root/'network-preferences.json', value)
    workspace.dns_resolver.clear()
    return {**state(workspace), 'message': '已保存本工作区代理，未进行联网测试。请停止并重开已有采集会话以采用新设置；其他工作区和系统设置未修改。'}


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


def save_pac(workspace, data):
    if (not isinstance(data, dict) or set(data) != {'name', 'script', 'revision', 'consent'}
            or type(data['revision']) is not int or data['consent'] is not True
            or not _pac_name(data['name'])):
        raise InputError('请选择本地PAC文件并明确同意执行说明；不接受远程URL或额外参数。')
    if not pac_native.available(): raise InputError(PAC_MESSAGES['pac_unavailable'])
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
        value = {**previous, 'schema_version': 3, 'revision': previous['revision']+1,
            'updated_at': utc_now(), 'proxy_mode': 'pac', 'proxy_endpoint': '',
            'proxy_consent_version': PAC_CONSENT, 'pac_name': data['name'],
            'pac_sha256': sha, 'pac_id': secrets.token_hex(16)}
        atomic_json(workspace.root/'network-preferences.json', value)
    workspace.dns_resolver.clear()
    return {**state(workspace), 'message': '已保存PAC副本及许可；尚未执行或联网测试。新会话采用此脚本，已有会话请停止并重开。'}


def check_pac(workspace, data):
    if not isinstance(data, dict) or set(data) != {'revision'} or type(data['revision']) is not int:
        raise InputError('仅接受当前网络偏好修订号。')
    settings = read_settings(workspace)
    if settings['revision'] != data['revision'] or settings.get('proxy_mode') != 'pac':
        raise InputError('PAC设置已改变或尚未导入，请刷新后重试。')
    policy = capture_policy(workspace, settings=settings)
    try:
        selected = policy.for_host('pac-check.invalid')
    except LocalProxyError as exc:
        return {'passed': False, 'code': exc.code, 'target_requested': False,
                'message': PAC_MESSAGES.get(exc.code, PROXY_CONFLICT)}
    return {'passed': True, 'code': 'pac_evaluated', 'transport': policy.transport_name(selected),
            'target_requested': False,
            'message': '脚本对内置检查域名返回了有效路线；未连接代理或目标。不代表招聘域名的选路、登录或取数已通过。'}


DNS_MESSAGES = {
    **VM_MESSAGES,
    **PAC_MESSAGES,
    'workspace_proxy_environment_conflict': PROXY_CONFLICT,
    'non_public_address': '系统返回非公网地址。若网络检查显示映射地址，可在“网络自动适配”阅读说明并启用加密解析；无需改系统DNS或关闭VPN。其他私网地址仍会拒绝。',
    'encrypted_dns_disabled': '加密解析的许可已撤销；未发起新的解析。已有任务与数据保留。',
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
