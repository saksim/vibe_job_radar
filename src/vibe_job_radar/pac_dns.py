"""Bounded system DNS helpers for a separately consented PAC subprocess.

These answers select a route only. The HTTP transport still independently
validates every destination and pins public addresses before connecting.
"""
from __future__ import annotations
import ipaddress
import re
import socket

MAX_NAMES = 16

HELPERS = r"""
function dnsResolve(host) { return __radar_dns_resolve(host); }
function isResolvable(host) { return dnsResolve(host) !== null; }
function myIpAddress() { return __radar_my_ip(); }
function isInNet(host, pattern, mask) {
    function numeric(value) {
        if (typeof value !== 'string') return null;
        var parts = value.split('.'), result = 0;
        if (parts.length !== 4) return null;
        for (var i = 0; i < 4; i++) {
            if (!/^(0|[1-9][0-9]{0,2})$/.test(parts[i]) || Number(parts[i]) > 255) return null;
            result = (result << 8) | Number(parts[i]);
        }
        return result;
    }
    var network = numeric(pattern), netmask = numeric(mask);
    if (network === null || netmask === null) return false;
    var address = numeric(host);
    if (address === null) address = numeric(dnsResolve(host));
    return address !== null && (address & netmask) === (network & netmask);
}
"""


class DnsHelpers:
    def __init__(self):
        self.cache = {}
        self.error = None
        self.local_address = None

    def resolve(self, host):
        if not isinstance(host, str) or not 1 <= len(host) <= 253:
            return None
        try:
            literal = ipaddress.ip_address(host)
            return str(literal) if literal.version == 4 else None
        except ValueError:
            # Malformed dotted IPv4 must not become a hostname lookup.
            if re.fullmatch(r'[0-9]+(?:\.[0-9]+){3}', host):return None
        try:
            name = host.rstrip('.').encode('idna').decode('ascii').lower()
        except UnicodeError:
            return None
        if not name or len(name) > 253 or any(not re.fullmatch(
                r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in name.split('.')):
            return None
        if name in self.cache:
            return self.cache[name]
        if len(self.cache) >= MAX_NAMES:
            self.error = 'pac_dns_limit'
            return None
        self.cache[name] = None  # Count failed queries too; never loop on DNS.
        try:
            answers = socket.getaddrinfo(name, None, socket.AF_INET, socket.SOCK_STREAM)
            for answer in answers:
                address = ipaddress.IPv4Address(answer[4][0])
                self.cache[name] = str(address)
                break
        except socket.gaierror as exc:
            absent = {socket.EAI_NONAME, getattr(socket, 'EAI_NODATA', socket.EAI_NONAME)}
            if exc.errno not in absent:
                self.error = 'pac_dns_failed'
        except (OSError, ValueError, TypeError, IndexError):
            self.error = 'pac_dns_failed'
        return self.cache[name]

    def my_ip(self):
        if self.local_address is None:
            try:
                self.local_address = self.resolve(socket.gethostname()) or '127.0.0.1'
            except OSError:
                self.error = 'pac_dns_failed'
                self.local_address = '127.0.0.1'
        return self.local_address
