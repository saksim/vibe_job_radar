"""Bounded robots rules shared by HTTP and browser paths; no network access."""
from __future__ import annotations
import re
from urllib.parse import quote, urlsplit


class RobotsError(ValueError):
    def __init__(self, code='robots_unavailable'):
        self.code = code
        super().__init__(code)


def _octets(value):
    """RFC9309 comparison: UTF-8 octets, unreserved %-escapes decoded only."""
    def escape(m):
        c = chr(int(m[0][1:], 16))
        return c if c.isascii() and (c.isalnum() or c in '-._~') else m[0].upper()
    return re.sub(r'%[0-9A-Fa-f]{2}', escape, quote(value, safe="/%*?$&=:+,;@!'-._~()"))


def _glob_matches(pattern, target, anchored):
    """Literal chunks only: publisher wildcards cannot cause regex backtracking."""
    chunks = pattern.split('*')
    if not target.startswith(chunks[0]):
        return False
    offset = len(chunks[0])
    if len(chunks) == 1:
        return not anchored or offset == len(target)
    for chunk in chunks[1:-1]:
        found = target.find(chunk, offset)
        if found < 0:
            return False
        offset = found + len(chunk)
    last = chunks[-1]
    if anchored:
        return target.endswith(last) and len(target) - len(last) >= offset
    return target.find(last, offset) >= 0


class RobotsRules:
    """Bounded explicit rules, with conservative unavailable/invalid handling.

    A 404/410 means no robots file (RFC 9309 section 2.3.1.3). Other errors,
    including authentication/rate denials and invalid 200 bodies, still stop.
    This does not grant an operation outside the site's request contract.
    Crawl-delay and Request-rate never reduce the existing ledger policy.
    """
    def __init__(self, status, content_type, body, *, user_agent):
        self.rules, self.delay, self.windows = [], 0.0, []
        if status in {404, 410}:
            # Do not interpret an error document as publisher rules. In the
            # native browser it is delivered with scripts/resources disabled.
            return
        if (status != 200 or content_type.split(';')[0].strip().lower() != 'text/plain'
                or len(body) > 512 * 1024):
            raise RobotsError('robots_unavailable')
        try:
            text = body.decode('utf-8-sig')
        except UnicodeError as exc:
            raise RobotsError('robots_unavailable') from exc
        if re.search(r'<\s*(?:!doctype|html|script|body)\b', text, re.I):
            raise RobotsError('robots_unavailable')
        groups, agents, entries = [], [], []
        for raw in text.splitlines():
            line = raw.split('#', 1)[0].strip()
            if not line or ':' not in line:
                continue
            k, value = (x.strip() for x in line.split(':', 1)); k = k.lower()
            if k == 'user-agent':
                if entries:
                    groups.append((agents, entries)); agents, entries = [], []
                agents.append(value.lower())
            elif agents and k in {'allow','disallow','crawl-delay','request-rate'}:
                if len(value) > 2048:
                    raise RobotsError('robots_unavailable')
                entries.append((k, value))
        if agents:
            groups.append((agents, entries))
        if not groups:
            raise RobotsError('robots_unavailable')
        token = user_agent.split('/')[0].lower()
        selected = [v for a,v in groups if token in a]
        if not selected:
            selected = [v for a,v in groups if '*' in a]
        for group in selected:
            for key, value in group:
                if key in {'allow','disallow'} and value:
                    if not value.startswith('/'):
                        raise RobotsError('robots_unavailable')
                    value = _octets(value)
                    end = value.endswith('$')
                    pattern = value[:-1] if end else value
                    # Literal chunk matching bounds work even for hostile wildcards.
                    if pattern.count('*') > 32:
                        raise RobotsError('robots_unavailable')
                    pattern = re.sub(r'\*+', '*', pattern)
                    self.rules.append((len(value.replace('*','').rstrip('$')), key == 'allow', pattern, end))
                elif key == 'crawl-delay':
                    if not re.fullmatch(r'[0-9]{1,6}(?:\.[0-9]{1,3})?', value):
                        raise RobotsError('robots_unavailable')
                    self.delay = max(self.delay, float(value))
                elif key == 'request-rate':
                    m = re.fullmatch(r'([1-9][0-9]{0,6})\s*/\s*([1-9][0-9]{0,7})', value)
                    if not m:
                        raise RobotsError('robots_unavailable')
                    self.windows.append(tuple(map(int, m.groups())))
        if len(self.rules) > 2000:
            raise RobotsError('robots_unavailable')

    def allowed(self, url):
        p = urlsplit(url)
        target = _octets(p.path or '/') + (('?' + _octets(p.query)) if p.query else '')
        matches = [(n, allow) for n,allow,pattern,end in self.rules if _glob_matches(pattern,target,end)]
        return max(matches, default=(0, True))[1]
