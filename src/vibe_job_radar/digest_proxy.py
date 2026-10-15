"""Explicit HTTP Digest on a loopback proxy, with one bounded challenge exchange."""
from __future__ import annotations

import ipaddress
import math
import re
import socket
import time
from dataclasses import dataclass

from .digest_auth import DigestError, select_challenge
from .loopback_proxy import LocalProxyError, LoopbackProxy

_NAME = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")


@dataclass(frozen=True)
class LoopbackDigestProxy(LoopbackProxy):
    auth_scheme = 'digest'

    def open_tunnel(self, public_ip: str, timeout: float, source_address=None) -> socket.socket:
        try:
            address = ipaddress.ip_address(public_ip)
        except (ValueError, TypeError):
            raise LocalProxyError('non_public_address') from None
        if not address.is_global or '%' in public_ip:
            raise LocalProxyError('non_public_address')
        if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
                or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError('a finite positive timeout is required')
        if self.credentials is None:
            raise LocalProxyError('local_proxy_credentials_invalid')
        authority = ('[' + str(address) + ']' if address.version == 6 else str(address)) + ':443'
        deadline = time.monotonic() + timeout
        sock = None

        def remaining():
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise TimeoutError()
            return budget

        def exchange(authorization=None):
            nonlocal sock
            sock = socket.create_connection((self.host, self.port), remaining(), source_address)
            request = f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n'
            if authorization is not None:
                request += 'Proxy-Authorization: ' + authorization + '\r\n'
            sock.settimeout(remaining())
            sock.sendall((request + '\r\n').encode('latin-1'))
            raw = bytearray()
            while b'\r\n\r\n' not in raw:
                if len(raw) >= 16384:
                    raise DigestError()
                sock.settimeout(remaining())
                part = sock.recv(min(4096, 16384 - len(raw)))
                if not part:
                    raise DigestError()
                raw.extend(part)
            block, extra = bytes(raw).split(b'\r\n\r\n', 1)
            lines = block.split(b'\r\n')
            if (len(lines) > 129 or not re.fullmatch(rb'HTTP/1\.[01] [0-9]{3}(?: [^\r\n]*)?', lines[0])
                    or any((byte < 32 and byte != 9) or byte == 127 for line in lines for byte in line)):
                raise DigestError()
            headers = {}
            for line in lines[1:]:
                name, sep, value = line.partition(b':')
                if not sep or not _NAME.fullmatch(name):
                    raise DigestError()
                headers.setdefault(name.decode('ascii').lower(), []).append(value.strip().decode('latin-1'))
            return int(lines[0].split(b' ', 2)[1]), headers, extra

        try:
            status, headers, _ = exchange()
            # No preemptive credentials and no anonymous success in Digest mode.
            if status != 407:
                raise DigestError()
            challenge = select_challenge(headers.get('proxy-authenticate', []))
            sock.close()
            sock = None
            authorization, proof = self.credentials.digest_authorization(challenge, 'CONNECT', authority)
            remaining()
            status, headers, extra = exchange(authorization)
            # Never retry stale/rejected credentials, downgrade, or replay origin traffic.
            if status != 200 or extra:
                raise DigestError()
            proof.verify(headers.get('proxy-authentication-info', []))
            sock.settimeout(remaining())
            result, sock = sock, None
            return result
        except DigestError:
            raise LocalProxyError('local_proxy_auth_failed') from None
        except OSError:
            raise LocalProxyError('local_proxy_connection_failed') from None
        finally:
            if sock is not None:
                sock.close()
