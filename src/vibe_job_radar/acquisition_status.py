"""One offline capability catalogue for the registry, UI, documentation and reports.

Recorded CI evidence belongs to its dated source revision and artificial scope.
It never attests to the user's browser/network, account or current live pages.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict

BASELINE = 'ec4587992113848be879da2fbc4cebdac62d10e2'
EVIDENCE_HEAD = 'cde1e1cf842fee2a15ade77470753201fc7fdc45'
EVIDENCE_DATE = '2026-09-18'
LEVELS = ('implemented', 'controlled_verified', 'pilot_verified', 'live_verified', 'blocked')
REPO = 'https://github.com/saksim/vibe_job_radar'
# Bind historical evidence to reviewed adapter definitions, not just their keys.
# These hashes are change detection, not a signature or proof of site permission.
DEFINITIONS = {
    'boss':'20cc35b82135ead18f509a2c8da594577f4bfcc96175f51a70789405d8952f2a',
    'liepin':'891af2f2b1494357c2cf880eb1ecb9bd16444f3d9f427b0de7dfcdd41c15e075',
    '51job':'ba24c8f94c26b51360a1fdfc30f24815454a30f9ac9702759445625b1bc49221',
}
NATIVE_DEFINITION = '1e1531acdeea880808c168fa33df4b4b7d957c3b707b1c0a437607358234ca31'


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def describe_adapter(adapter):
    from .guided.native_policy import contract_for
    from .guided.contracts import CrawlError
    try:
        identity = _digest({'adapter':asdict(adapter),
                           'class':type(adapter).__module__+'.'+type(adapter).__qualname__})
    except TypeError:
        identity = None
    known = identity is not None and identity == DEFINITIONS.get(adapter.key)
    try:
        contract = contract_for(adapter)
    except CrawlError:
        contract = None
    issue = {'boss':55, 'liepin':54, '51job':56}.get(adapter.key, 9)
    rows = []
    for backend in ('bridge','native'):
        available = backend == 'bridge' or contract is not None
        matched = known and (backend == 'bridge' or contract is not None
                             and _digest(asdict(contract)) == NATIVE_DEFINITION)
        evidence = []
        if matched:
            # Shared backend fixtures are not dedicated live-site certification.
            evidence = [{'level':'controlled_verified', 'date':EVIDENCE_DATE,
                'source_revision':EVIDENCE_HEAD,
                'scope':'人工页面与真实浏览器/报告引擎；未使用平台账号，未认证实站',
                'browser_os':(['Chromium / Linux'] if backend == 'bridge' else
                              ['Chromium / Linux（有头与无头）','Edge / Windows（有头与无头）']),
                'network':'人工上游或本机 TLS 夹具；不代表用户 VPN/TUN',
                'url':REPO+'/actions/runs/'+('35358529040' if backend == 'bridge' else '35358529120')}]
        if not available:
            message = '尚无本站原生访问契约，不能选择该模式。'
        elif not known:
            message = '当前适配定义没有匹配的已记录验收；需单独验证，实站未认证。'
        elif adapter.key == 'liepin':
            message = ('已有本机实现；真实搜索仍受阻，正常登录和完整 JD 尚未验通。'
                       if backend == 'bridge' else
                       '原生模式仍为显式实验；本站取数受阻，人工夹具通过不代表实站可用。')
        else:
            message = '通用页面适配已实现；本站专用契约与真实登录、正文仍待验证。'
        rows.append({'backend':backend, 'available':available, 'default':backend == 'bridge',
            'implementation_status':'implemented' if available else 'blocked',
            'verification_level':'controlled_verified' if evidence else ('implemented' if available else 'blocked'),
            'verification_scope':'historical_fixture_evidence_only', 'evidence':evidence,
            'contract':contract.key if backend == 'native' and contract else '',
            'access_scope':('已配置的搜索/详情页面与资源；按 robots 和正常登录范围执行' if backend == 'bridge' else
                            '仅代码内已审核的主机/方法/路径；不接受页面扩展权限' if contract else '未开放'),
            'live_status':'blocked' if adapter.key == 'liepin' else 'not_verified',
            'live_verified':False, 'pilot_verified':False, 'user_network_verified':False,
            'blocking_issues':[issue] + ([63] if adapter.key == 'liepin' else []), 'message':message})
    return {'platform':adapter.key, 'label':adapter.label,
            'adapter_version':getattr(adapter,'version','custom'),
            'definition_matches_recorded_evidence':known, 'certification':'not_live_verified', 'backends':rows}


def snapshot(registry=None):
    if registry is None:
        from .guided.adapters import builtins
        registry = builtins()
    return {'schema_version':1, 'catalog_revision':'2026-09-23', 'main_baseline':BASELINE,
        'scope':'软件能力及历史受控证据；不证明本报告岗位的获取方式、真实登录或市场覆盖',
        'sites':[site['acquisition'] for site in registry.describe()]}


def markdown_table():
    lines = ['| 平台 / 适配版本 | 后端 / 默认 | 实现 | 最近记录的受控验证 | 实站 / 剩余跟踪 |',
             '|---|---|---|---|---|']
    for site in snapshot()['sites']:
        for row in site['backends']:
            proof = row['evidence']
            evidence = ('['+proof[0]['date']+' 人工夹具]('+proof[0]['url']+')' if proof else '无匹配记录')
            issues = ' / '.join('[#'+str(i)+']('+REPO+'/issues/'+str(i)+')' for i in row['blocking_issues'])
            lines.append('| '+site['label']+' / '+site['adapter_version']+' | '+row['backend']+
                         (' / 默认' if row['default'] else ' / 显式实验' if row['available'] else ' / 不可用')+
                         ' | '+row['implementation_status']+' | '+evidence+' | '+row['live_status']+'；'+issues+' |')
    return '\n'.join(lines)
