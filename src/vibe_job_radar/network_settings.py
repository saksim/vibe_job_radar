"""Workspace DNS consent and explicit anonymous loopback proxy preferences."""
from __future__ import annotations

import json
import os
from dataclasses import replace

from .network_policy import NetworkPolicy
from .loopback_proxy import LoopbackProxy, LocalProxyError
from .loopback_socks import LoopbackSocks5
from .vm_proxy import VmHTTPProxy, VmSocks5Proxy, ERROR_MESSAGES as VM_MESSAGES
from .proxy_credentials import configured as credentials_configured
from .html_parser import _unique_object
from .utils import atomic_json, utc_now
from .workspace import InputError

CONSENT = 'cloudflare-doh-v1'
MODES = {'system', 'fake_ip_doh'}
PROXY_CONSENT = 'workspace-anonymous-loopback-v1'
VM_CONSENT = 'workspace-anonymous-vm-host-v1'
PROXY_PARSERS = {'http': LoopbackProxy, 'socks5': LoopbackSocks5,
                 'vm_http': VmHTTPProxy, 'vm_socks5': VmSocks5Proxy}
PROXY_MODES = {'auto', *PROXY_PARSERS}
PROXY_DEFAULT = {'proxy_mode': 'auto', 'proxy_endpoint': '', 'proxy_consent_version': ''}
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
        if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] not in (1, 2):
            raise ValueError
        expected = set(default) | (set(PROXY_DEFAULT) if value['schema_version'] == 2 else set())
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
        return value
    except (ValueError, TypeError, OSError, InputError):
        raise InputError('网络偏好文件无效或版本不兼容；已停止联网，离线数据不受影响。') from None


def capture_policy(workspace, *, settings=None):
    if settings is None:
        settings = read_settings(workspace)
    proxy = _proxy(settings.get('proxy_mode', 'auto'), settings.get('proxy_endpoint', ''))
    if proxy is None:
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
        value = {**previous, 'schema_version': 2, 'revision': previous['revision'] + 1,
                 'updated_at': utc_now(), 'proxy_mode': data['mode'],
                 'proxy_endpoint': _endpoint(data['mode'], proxy),
                 'proxy_consent_version': _proxy_consent(data['mode'])}
        atomic_json(workspace.root/'network-preferences.json', value)
    workspace.dns_resolver.clear()
    return {**state(workspace), 'message': '已保存本工作区代理，未进行联网测试。请停止并重开已有采集会话以采用新设置；其他工作区和系统设置未修改。'}


DNS_MESSAGES = {
    **VM_MESSAGES,
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
