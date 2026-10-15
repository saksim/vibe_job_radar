"""Bounded HTTP Digest challenge/response, never origin authentication.

RFC 7616 algorithms, session variants, qop auth/auth-int and legacy no-qop.
Credentials retain the application's printable-ASCII contract. Header values
are decoded as Latin-1 and re-encoded unchanged, including UTF-8 realm bytes.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets

_TOKEN = re.compile(r"^[!#$%&'*+\-.^_\x60|~0-9A-Za-z]+$")
_ALGORITHMS = {
    'MD5': ('md5', 'MD5', 1),
    'SHA-256': ('sha256', 'SHA-256', 2),
    'SHA-512-256': ('sha512_256', 'SHA-512-256', 3),
}


class DigestError(ValueError):
    def __init__(self):
        super().__init__('unsupported_or_invalid_digest_response')


def _segments(value):
    if (not isinstance(value, str) or len(value) > 16384
            or any((ord(c) < 32 and c != '\t') or ord(c) == 127 or ord(c) > 255 for c in value)):
        raise DigestError()
    result, start, quoted, escaped = [], 0, False, False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif quoted and char == '\\':
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == ',' and not quoted:
            result.append(value[start:index].strip())
            start = index + 1
    if quoted or escaped:
        raise DigestError()
    result.append(value[start:].strip())
    if not all(result) or len(result) > 64:
        raise DigestError()
    return result


def _parameter(segment):
    name, sep, raw = segment.partition('=')
    name, raw = name.strip().lower(), raw.strip()
    if not sep or not _TOKEN.fullmatch(name) or not raw:
        raise DigestError()
    if raw.startswith('"'):
        if len(raw) < 2 or not raw.endswith('"'):
            raise DigestError()
        value, escaped = [], False
        for char in raw[1:-1]:
            if escaped:
                value.append(char)
                escaped = False
            elif char == '\\':
                escaped = True
            elif char == '"':
                raise DigestError()
            else:
                value.append(char)
        if escaped:
            raise DigestError()
        value = ''.join(value)
    else:
        if not _TOKEN.fullmatch(raw):
            raise DigestError()
        value = raw
    if len(value) > 4096:
        raise DigestError()
    return name, value


def _parameters(segments):
    result = {}
    if len(segments) > 24:
        raise DigestError()
    for segment in segments:
        key, value = _parameter(segment)
        if key in result:
            raise DigestError()
        result[key] = value
    return result


def _quote(value):
    if any((ord(c) < 32 and c != '\t') or ord(c) == 127 or ord(c) > 255 for c in value):
        raise DigestError()
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


@dataclass(frozen=True, repr=False)
class DigestProof:
    response: str
    cnonce: str
    qop: str
    nc: str

    def verify(self, headers):
        if not headers:
            return
        if len(headers) > 8 or sum(map(len, headers)) > 8192:
            raise DigestError()
        values = _parameters(_segments(','.join(headers)))
        # nextnonce is not cached: each tunnel starts a fresh challenge.
        for key, expected in (('cnonce', self.cnonce), ('qop', self.qop), ('nc', self.nc)):
            if key in values and values[key] != expected:
                raise DigestError()
        if 'rspauth' in values:
            supplied = values['rspauth']
            if (len(supplied) != len(self.response) or not re.fullmatch('[0-9a-fA-F]+', supplied)
                    or not hmac.compare_digest(supplied.lower(), self.response)):
                raise DigestError()


@dataclass(frozen=True, repr=False)
class DigestChallenge:
    realm: str
    nonce: str
    opaque: str | None
    algorithm: str
    hash_name: str
    session: bool
    qop: str
    userhash: bool
    strength: int

    @classmethod
    def parse(cls, values):
        if ('realm' not in values or not values.get('nonce')
                or len(values['realm']) > 1024 or len(values['nonce']) > 2048
                or len(values.get('opaque', '')) > 2048
                or values.get('charset', 'UTF-8').upper() != 'UTF-8'
                or values.get('userhash', 'false').lower() not in {'true', 'false'}):
            raise DigestError()
        algorithm = values.get('algorithm', 'MD5').upper()
        session = algorithm.endswith('-SESS')
        base = algorithm.removesuffix('-SESS') if session else algorithm
        if base not in _ALGORITHMS:
            raise DigestError()
        hash_name, canonical, strength = _ALGORITHMS[base]
        try:
            hashlib.new(hash_name)
        except (ValueError, TypeError):
            raise DigestError() from None
        if 'qop' in values:
            offered = [v.strip() for v in values['qop'].split(',')]
            if 'auth' in offered:
                qop = 'auth'
            elif 'auth-int' in offered:
                qop = 'auth-int'
            else:
                raise DigestError()
        else:
            qop = ''
        return cls(values['realm'], values['nonce'], values.get('opaque'),
                   canonical + ('-sess' if session else ''), hash_name, session,
                   qop, values.get('userhash', 'false').lower() == 'true', strength)

    def authorize(self, username, password, method, uri, *, cnonce=None):
        if (not isinstance(username, bytes) or not isinstance(password, bytes)
                or not 1 <= len(username) <= 255 or not 1 <= len(password) <= 255
                or b':' in username or any(not 32 <= c <= 126 for c in username + password)
                or not isinstance(method, str) or not _TOKEN.fullmatch(method)
                or not isinstance(uri, str) or not 1 <= len(uri) <= 2048
                or any(not 33 <= ord(c) <= 126 for c in uri)):
            raise DigestError()
        cnonce = secrets.token_hex(32) if cnonce is None else cnonce
        if (not isinstance(cnonce, str) or not 16 <= len(cnonce) <= 128
                or any(not 33 <= ord(c) <= 126 for c in cnonce)):
            raise DigestError()
        def digest(data):
            return hashlib.new(self.hash_name, data).hexdigest()
        def encoded(value):
            return value.encode('latin-1')
        ha1 = digest(username + b':' + encoded(self.realm) + b':' + password)
        if self.session:
            ha1 = digest(encoded(ha1 + ':' + self.nonce + ':' + cnonce))
        def response(verb):
            a2 = verb + ':' + uri
            if self.qop == 'auth-int':
                a2 += ':' + digest(b'')  # CONNECT has no HTTP entity body.
            tail = self.nonce + ':'
            if self.qop:
                tail += '00000001:' + cnonce + ':' + self.qop + ':'
            return digest(encoded(ha1 + ':' + tail + digest(encoded(a2))))
        user = digest(username + b':' + encoded(self.realm)) if self.userhash else username.decode('ascii')
        fields = ['username=' + _quote(user), 'realm=' + _quote(self.realm),
                  'nonce=' + _quote(self.nonce), 'uri=' + _quote(uri),
                  'response=' + _quote(response(method)), 'algorithm=' + self.algorithm]
        if self.opaque is not None:
            fields.append('opaque=' + _quote(self.opaque))
        if self.qop:
            fields += ['qop=' + self.qop, 'nc=00000001', 'cnonce=' + _quote(cnonce)]
        elif self.session:
            fields.append('cnonce=' + _quote(cnonce))
        if self.userhash:
            fields.append('userhash=true')
        header = 'Digest ' + ', '.join(fields)
        if len(header.encode('latin-1')) > 8192:
            raise DigestError()
        return header, DigestProof(response(''), cnonce, self.qop, '00000001' if self.qop else '')


def select_challenge(headers):
    """Prefer the strongest supported Digest offer; never select Basic."""
    if not headers or len(headers) > 16 or sum(map(len, headers)) > 16384:
        raise DigestError()
    groups = []
    for header in headers:
        scheme, parameters = None, []
        for segment in _segments(header):
            words = segment.split(None, 1)
            if ((len(words) == 1 and _TOKEN.fullmatch(segment))
                    or (len(words) == 2 and _TOKEN.fullmatch(words[0]) and not words[1].lstrip().startswith('='))):
                if scheme is not None:
                    groups.append((scheme, parameters))
                scheme, parameters = words[0].lower(), [words[1]] if len(words) == 2 else []
            else:
                if scheme is None:
                    raise DigestError()
                parameters.append(segment)
        if scheme is not None:
            groups.append((scheme, parameters))
    if len(groups) > 16:
        raise DigestError()
    choices = []
    for scheme, parameters in groups:
        if scheme != 'digest':
            continue
        try:
            choices.append(DigestChallenge.parse(_parameters(parameters)))
        except DigestError:
            continue
    if not choices:
        raise DigestError()
    return max(choices, key=lambda candidate: candidate.strength)
