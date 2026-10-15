"""Explicit portable domain-only PAC using the existing isolated worker."""
from __future__ import annotations

from dataclasses import dataclass, field

from .loopback_proxy import LocalProxyError
from .pac import canonical_target
from .pac_url import UrlPacSnapshot, MESSAGES as URL_MESSAGES

CONSENT = 'workspace-trusted-portable-domain-pac-v1'
DNS_CONSENT = 'workspace-trusted-portable-domain-pac-dns-v1'
MESSAGES = {**URL_MESSAGES,
    'pac_domain_unavailable': '跨平台域名PAC组件未安装或版本不兼容；已停止，未改为直连。'}
DISCLOSURE = ('只导入你信任的本地PAC文件（UTF-8，最多64KiB）。独立进程执行脚本，'
    '只提供规范HTTPS根地址和域名，不提供路径、查询参数、Cookie、请求头或正文。'
    '提供五个字符串及三个日期时间助手，不提供网络、文件、DNS或本机地址接口。'
    '每个域名在当前会话中只采用首次结果，最多128个域名；时间变化不会自动重新选路。'
    'HTTP、请求桥和原生浏览器使用同一域名路线。只接受DIRECT、匿名本机PROXY和明确SOCKS5；'
    '只采用第一项，失败不换路线。不会读取系统PAC、发现WPAD或修改系统代理。')
DNS_DISCLOSURE = (DISCLOSURE.replace('不提供网络、文件、DNS或本机地址接口。',
    '可使用另行许可的系统DNS和本机IPv4助手，不提供文件或其他联网接口。')
    + '此范围允许dnsResolve、isResolvable、isInNet和myIpAddress；DNS服务可看到脚本请求的域名，'
    '脚本可看到系统主机名解析的首个IPv4，不保证多网卡路由判断。每次最多16个不同名称，受原执行期限限制。'
    '保存和状态读取不会执行或解析；明确检查会执行脚本要求的DNS查询。实际目标仍须公网IP和TLS校验。')


@dataclass(frozen=True)
class DomainPacSnapshot:
    source: str = field(repr=False)
    permission: object = field(repr=False, compare=False)
    dns_helpers: bool = False
    _engine: object = field(init=False, repr=False, compare=False)
    kind = 'portable_domain_pac'

    def __post_init__(self):
        object.__setattr__(self, '_engine', UrlPacSnapshot(self.source, self.permission, self.dns_helpers))

    @property
    def sha256(self):
        return self._engine.sha256

    @property
    def binding(self):
        return ('portable-domain-dns-v1:' if self.dns_helpers else 'portable-domain-v1:') + self.sha256

    def ensure_active(self):
        self._engine.ensure_active()

    def bind_cancellation(self, event):
        self._engine.bind_cancellation(event)

    def close(self):
        self._engine.close()

    def for_host(self, host):
        self.ensure_active()
        host, root = canonical_target(host)
        try:
            # Only a canonical origin enters the URL worker, never a caller's URL.
            return self._engine.for_url(root).for_host(host)
        except LocalProxyError as exc:
            code = {'pac_url_cache_limit': 'pac_cache_limit',
                    'pac_url_unavailable': 'pac_domain_unavailable'}.get(exc.code, exc.code)
            raise LocalProxyError(code) from None
