"""Explicit local HTTP NTLMv2 CONNECT, two requests on one TCP connection."""
from __future__ import annotations
import base64
import binascii
import ipaddress
import math
import re
import socket
import time
from dataclasses import dataclass

from .loopback_proxy import LocalProxyError, LoopbackProxy
from .ntlm_worker import MAX_TOKEN

_NAME = re.compile(rb"[!#$%&'*+\-.^_\x60|~0-9A-Za-z]+")


def _response(sock, remaining):
    raw = bytearray()
    while b'\r\n\r\n' not in raw:
        if len(raw) >= 16384:
            raise LocalProxyError('local_proxy_auth_failed')
        sock.settimeout(remaining())
        block = sock.recv(min(4096, 16384 - len(raw)))
        if not block:
            raise LocalProxyError('local_proxy_auth_failed')
        raw.extend(block)
    head, extra = bytes(raw).split(b'\r\n\r\n', 1)
    lines = head.split(b'\r\n')
    if (len(lines) > 129 or not re.fullmatch(rb'HTTP/1\.[01] [0-9]{3}(?: [^\r\n]*)?', lines[0])
            or any((byte < 32 and byte != 9) or byte == 127 for line in lines for byte in line)):
        raise LocalProxyError('local_proxy_auth_failed')
    headers = {}
    for line in lines[1:]:
        name, sep, value = line.partition(b':')
        if not sep or not _NAME.fullmatch(name):
            raise LocalProxyError('local_proxy_auth_failed')
        headers.setdefault(name.decode('ascii').lower(), []).append(value.strip().decode('latin-1'))
    return int(lines[0].split(b' ', 2)[1]), headers, extra, lines[0].startswith(b'HTTP/1.1 ')


def _challenge_body(sock, headers, extra, http11, remaining):
    # Authentication is connection-bound. Ambiguous, chunked or close-delimited
    # 407 bodies are not eligible for another request on the same connection.
    connection = {x.strip().lower() for value in headers.get('connection', [])
                  + headers.get('proxy-connection', []) for x in value.split(',')}
    lengths = headers.get('content-length', [])
    if ('close' in connection or (not http11 and 'keep-alive' not in connection)
            or 'transfer-encoding' in headers or len(lengths) != 1
            or not re.fullmatch(r'[0-9]{1,5}', lengths[0]) or int(lengths[0]) > 65536):
        raise LocalProxyError('local_proxy_auth_failed')
    left = int(lengths[0]) - len(extra)
    if left < 0:
        raise LocalProxyError('local_proxy_auth_failed')
    while left:
        sock.settimeout(remaining())
        chunk = sock.recv(min(4096, left))
        if not chunk:
            raise LocalProxyError('local_proxy_auth_failed')
        left -= len(chunk)


def _challenge(headers):
    offers = []
    for value in headers.get('proxy-authenticate', []):
        # Independent Basic/Digest offers can be ignored; never negotiate them.
        for item in value.split(','):
            scheme, _, token = item.strip().partition(' ')
            if scheme.lower() == 'ntlm':
                offers.append(token.strip())
    if len(offers) != 1 or not 1 <= len(offers[0]) <= ((MAX_TOKEN + 2) // 3) * 4:
        raise LocalProxyError('local_proxy_auth_failed')
    try:
        token = base64.b64decode(offers[0], validate=True)
        if base64.b64encode(token).decode('ascii') != offers[0]:
            raise ValueError()
        return token
    except (ValueError, binascii.Error):
        raise LocalProxyError('local_proxy_auth_failed') from None


@dataclass(frozen=True)
class LoopbackNtlmProxy(LoopbackProxy):
    auth_scheme = 'ntlmv2'

    def open_tunnel(self, public_ip, timeout, source_address=None):
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
        sock = session = None

        def remaining():
            budget = deadline - time.monotonic()
            if budget <= 0:
                raise LocalProxyError('local_proxy_ntlm_timeout')
            return budget

        def send(token):
            authorization = base64.b64encode(token).decode('ascii')
            request = (f'CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n'
                       f'Proxy-Authorization: NTLM {authorization}\r\n'
                       'Proxy-Connection: Keep-Alive\r\n\r\n')
            sock.settimeout(remaining())
            sock.sendall(request.encode('ascii'))

        try:
            session = self.credentials.ntlm_session(deadline)
            sock = socket.create_connection((self.host, self.port), remaining(), source_address)
            send(session.first)
            status, headers, extra, http11 = _response(sock, remaining)
            if status != 407:
                raise LocalProxyError('local_proxy_auth_failed')
            challenge = _challenge(headers)
            _challenge_body(sock, headers, extra, http11, remaining)
            send(session.respond(challenge))
            status, headers, extra, _ = _response(sock, remaining)
            if (status != 200 or extra or 'transfer-encoding' in headers
                    or headers.get('content-length', ['0']) != ['0']):
                raise LocalProxyError('local_proxy_auth_failed')
            session.close()
            session = None
            sock.settimeout(remaining())
            result, sock = sock, None
            return result
        except LocalProxyError:
            raise
        except TimeoutError:
            raise LocalProxyError('local_proxy_ntlm_timeout') from None
        except OSError:
            raise LocalProxyError('local_proxy_connection_failed') from None
        finally:
            if sock is not None:
                sock.close()
            if session is not None:
                session.close()
