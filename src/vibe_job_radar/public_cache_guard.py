"""Persist only a source's hard failure code so cache reuse cannot conceal it.

No URL, query, header, credential or response body is stored. Callers serialize
reads/writes with their existing workspace lock. A successful, fully validated
fetch is the only normal operation that clears the guard; cache hits cannot.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path

from .utils import atomic_json
from .workspace import InputError


# Only these fixed transport failures may reuse an otherwise valid old snapshot.
# This does not grant offline permissions/quota, clear a previous hard failure,
# retry a request or change its route. Auth, protocol, TLS and unknown errors stay hard.
TRANSIENT_FETCH_ERRORS = frozenset({
    'dns_error', 'network_error', 'http_429', 'http_500', 'http_502', 'http_503', 'http_504',
    'local_proxy_connection_failed', 'local_socks_connection_failed', 'local_socks_timeout',
    'vm_proxy_connection_failed', 'vm_proxy_timeout', 'encrypted_dns_unavailable',
    'encrypted_dns_timeout', 'encrypted_dns_cooldown', 'encrypted_dns_budget',
})


class CacheFailureGuard:
    def __init__(self, root: Path, scope: str):
        digest = hashlib.sha256(scope.encode('utf-8')).hexdigest()
        self.path = Path(root) / ('failure-' + digest + '.json')

    def _check_path(self):
        if self.path.is_symlink():
            raise InputError('来源状态文件不能使用符号链接。')

    def read(self, now: float) -> str | None:
        self._check_path()
        if not self.path.exists():
            return None
        try:
            if self.path.stat().st_size > 1024:
                raise ValueError
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if (not isinstance(data, dict) or set(data) != {'schema_version', 'code', 'observed_at'}
                    or type(data['schema_version']) is not int or data['schema_version'] != 1
                    or not isinstance(data['code'], str)
                    or not re.fullmatch(r'[a-z0-9_]{1,80}', data['code'])
                    or type(data['observed_at']) not in (int, float)
                    or not math.isfinite(data['observed_at']) or data['observed_at'] > now):
                raise ValueError
            return data['code']
        except (ValueError, TypeError, OSError):
            raise InputError('来源状态记录无效；未使用旧缓存替代当前查询，历史报告仍保留。') from None

    def record(self, code: str, now: float):
        self._check_path()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise InputError('来源状态时间无效，未覆盖原状态。')
        safe_code = code if isinstance(code, str) and re.fullmatch(r'[a-z0-9_]{1,80}', code) else 'public_response_invalid'
        atomic_json(self.path, {'schema_version': 1, 'code': safe_code, 'observed_at': now})

    def clear(self):
        self._check_path()
        self.path.unlink(missing_ok=True)
