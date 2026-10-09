"""User-triggered DNS check: raw system observation vs consented application path.

No job request, site login, browser launch, arbitrary URL, quota reset or TLS
bypass. Uses the workspace's existing resolver, permission callback and budget.
"""
from __future__ import annotations

from ..dns_wire import ResolutionError
from ..network_settings import DNS_MESSAGES
from ..loopback_proxy import LocalProxyError
from ..tls_context import status as tls_status


def diagnose_workspace(workspace, host, *, raw_probe, cancelled, has_sessions=False):
    policy = workspace.network_policy()
    result = {
        'tls_environment': tls_status(),
        'host': host, 'passed': False, 'code': 'not_checked', 'addresses': [],
        'system_dns': None, 'effective_resolution': {'tested': False, 'passed': False},
        'policy': policy.describe(host), 'encrypted_dns_enabled': policy.encrypted_dns,
        'existing_sessions_may_use_previous_policy': has_sessions,
        'target_connection_tested': False, 'browser_tested': False,
        'network_scope': 'User-triggered DNS resolution only; no target HTTP/TLS, login or collection certification',
    }
    try:
        policy.for_host(host)  # Broken explicit route: do not leak DNS first.
    except LocalProxyError as exc:
        result.update(code=exc.code, message='当前代理策略无效，未解析或请求目标；请查看策略错误，不会偷偷直连。')
        return result
    raw = raw_probe(host)
    result['system_dns'] = raw
    result['addresses'] = raw.get('addresses', [])  # Retain the legacy raw observation.
    result['code'] = raw.get('code', 'dns_error')
    all_fake = bool(result['addresses']) and all(a.get('fake_ip_range') is True for a in result['addresses'])
    if raw.get('passed') is True:
        result.update(passed=True, code='dns_ok', message='系统解析得到公网地址；仅 DNS 检查通过，不代表浏览器已就绪、目标连接成功或已登录。')
        result['effective_resolution'] = {'tested': True, 'passed': True, 'source': 'system_dns',
                                          'addresses': [a['ip'] for a in result['addresses']]}
    elif all_fake and not policy.encrypted_dns:
        result.update(code='encrypted_dns_consent_required',
                      message='系统返回 Fake-IP 映射，不能据此判断 VPN 故障。当前工作区未启用加密解析；可在“网络自动适配”阅读说明、同意并保存后再次检查。没有修改系统 DNS 或请求招聘网站。')
    elif all_fake and policy.encrypted_dns:
        try:
            # Revalidates system DNS, route, consent, cache age and resolver quota.
            # Never reuse the raw observation to skip these existing checks.
            snapshot = workspace.dns_resolver.resolve(host, policy, cancelled=cancelled)
            result.update(passed=True, code='effective_dns_ok',
                          message='系统原始解析为 Fake-IP；按当前工作区策略已取得经校验的公网解析地址。仅应用解析通过，不代表浏览器启动、目标 TLS、登录或取数通过。')
            result['effective_resolution'] = {
                'tested': True, 'passed': True, 'source': snapshot.source,
                'addresses': list(snapshot.addresses), 'cache_reused': snapshot.cache_reused,
                'policy_id': snapshot.policy_id,
            }
        except ResolutionError as exc:
            result.update(code=exc.code, message=DNS_MESSAGES.get(exc.code, '当前策略的解析未通过；未请求目标网站，也不切换出口或关闭证书校验。'))
            result['effective_resolution'] = {'tested': True, 'passed': False, 'code': exc.code}
            if exc.code == 'encrypted_dns_tls_failed' and exc.diagnostic:
                result['effective_resolution']['tls_diagnostic'] = exc.diagnostic
                result['message'] += ' ' + exc.diagnostic['next_action']
                if exc.diagnostic.get('reused_failure'):
                    result['message'] += ' 当前处于原保护等待期，显示的是上次失败证据，本次没有重连解析服务。'
                    if exc.diagnostic.get('matches_current_policy') is False:
                        result['message'] += ' 原失败的策略标识与当前不同，不作为新策略已经失败的证据。'
            elif exc.code in {'encrypted_dns_unavailable', 'encrypted_dns_cooldown'}:
                from ..dns_transport_diagnostic import public_details
                details = public_details(exc.diagnostic)
                if details is not None:
                    result['effective_resolution']['transport_diagnostic'] = details
                    result['message'] += ' ' + details['next_action']
                    if details['reused_failure']:
                        result['message'] += ' 当前处于原保护等待期，显示的是上次失败证据，本次没有重连解析服务。'
                        if details.get('matches_current_policy') is False:
                            result['message'] += ' 原失败的策略标识与当前不同，不作为新策略已经失败的证据。'
    else:
        result['message'] = ('系统 DNS 未得到可用结果；此检查没有证明浏览器故障或账号问题。'
                             if result['code'] == 'dns_error' else
                             '系统返回私网、混合或异常地址，不属于已支持的纯 Fake-IP 修复场景；没有放行这些地址。')
    if has_sessions:
        result['message'] += ' 当前检查使用新会话策略；已有采集会话不会悄悄更换网络设置。'
    return result
