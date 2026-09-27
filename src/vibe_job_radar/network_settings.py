"""One local consent choice for Fake-IP repair, not an arbitrary proxy editor."""
from __future__ import annotations

import json
from dataclasses import replace

from .network_policy import NetworkPolicy
from .utils import atomic_json, utc_now
from .workspace import InputError

CONSENT = 'cloudflare-doh-v1'
MODES = {'system', 'fake_ip_doh'}
DISCLOSURE = ('仅当系统DNS为映射地址时，使用Cloudflare加密解析目标域名；解析服务可看到域名和网络出口，'
              '不发送职位正文、查询参数、Cookie、密码或个人材料。不修改系统DNS，不要求关闭VPN/TUN。'
              '不开启时维持系统解析；不是所有VPN或企业网络的兼容保证。')


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
        value = json.loads(path.read_text(encoding='utf-8'))
        if (not isinstance(value, dict) or set(value) != set(default)
                or type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['mode'] not in MODES or type(value['revision']) is not int
                or not 1 <= value['revision'] < 2**31 or not isinstance(value['updated_at'], str)
                or value['consent_version'] != (CONSENT if value['mode'] == 'fake_ip_doh' else '')):
            raise ValueError
        return value
    except (ValueError, TypeError, OSError):
        raise InputError('网络偏好文件无效；未启用加密解析，离线数据不受影响。') from None


def capture_policy(workspace):
    settings = read_settings(workspace)
    return replace(NetworkPolicy.capture(), encrypted_dns=settings['mode'] == 'fake_ip_doh',
                   resolver=workspace.dns_resolver)


def state(workspace):
    settings = read_settings(workspace)
    return {**settings, 'disclosure': DISCLOSURE, 'provider': 'Cloudflare',
            'network_tested': False, 'policy': capture_policy(workspace).describe()}


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
        value = {'schema_version': 1, 'mode': data['mode'], 'revision': previous['revision']+1,
                 'consent_version': CONSENT if data['mode'] == 'fake_ip_doh' else '', 'updated_at': utc_now()}
        atomic_json(workspace.root/'network-preferences.json', value)
    workspace.dns_resolver.clear()
    return {**state(workspace), 'message': '已保存。新公开查询、高级采集的下一步和新浏览器会话采用此设置；已有浏览器会话不偷偷更换网络。关闭后不再发起新的加密DNS请求。'}


DNS_MESSAGES = {
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
