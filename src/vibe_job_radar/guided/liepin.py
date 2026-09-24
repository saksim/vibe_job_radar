"""Liepin detail identity and bounded semantic JD extraction.

No private API, login automation, network permission or live certification is
introduced here. Only content already returned through the existing backend is
read. The semantic fallback is deliberately conservative; unknown layouts fail.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlsplit
from html import unescape
from typing import Callable

from ..html_parser import (Document, Node, ParseError, jobpostings, plain_text,
                           _posting_identity, _unique_object)
from .adapters import DOMAdapter
from .contracts import Card, CrawlError, PageSnapshot
from .page_surface import surface_text

_OMIT = {'script', 'style', 'nav', 'footer', 'aside', 'noscript', 'template', 'iframe', 'svg'}
_HEADINGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'dt'}
# The observed phrase 公司信息部 names a department inside a duty, not an
# employer-information panel. Keep the exception limited to that exact phrase.
_FOREIGN = re.compile(r'推荐职位|相似职位|猜你喜欢|公司简介|公司信息(?!部)|猎聘温馨提示')
_INCOMPLETE = re.compile(r'登录后.{0,8}(?:查看|浏览)|查看完整.{0,4}(?:职位|描述)|展开(?:全部|更多)|安全验证|滑动.{0,8}验证')
_JOB_CONTENT = re.compile(r'职责|要求|岗位描述|职位描述|工作内容|工作职能|任职资格')


def _hidden(node: Node) -> bool:
    style = re.sub(r'\s+', '', node.attrs.get('style', '')).lower()
    return (node.tag in _OMIT or 'hidden' in node.attrs
            or node.attrs.get('aria-hidden', '').lower() == 'true'
            or bool(re.search(r'(?:^|;)display:none(?:!important)?(?:;|$)', style))
            or bool(re.search(r'(?:^|;)visibility:hidden(?:!important)?(?:;|$)', style)))


def _walk(node: Node, parent: Node | None = None):
    if _hidden(node):
        return
    yield node, parent
    for child in node.children:
        if isinstance(child, Node):
            yield from _walk(child, node)


def _text(node: Node) -> str:
    if _hidden(node):
        return ''
    result = ''.join(_text(c) if isinstance(c, Node) else c for c in node.children)
    return result + ('\n' if node.tag in {'p','div','li','br','h1','h2','h3','h4','section','dd'} else '')


def _clean(value: str) -> str:
    return re.sub(r'\n\s*\n+', '\n', re.sub(r'[ \t\xa0]+', ' ', value)).strip()


def _has_structured_job(root: Node) -> bool:
    # An explicit structured identity conflict must not disappear through DOM
    # fallback. The established JSON-LD parser remains authoritative in that case.
    for node in root.walk():
        if node.tag == 'script' and node.attrs.get('type', '').lower() == 'application/ld+json':
            try:
                if any(jobpostings(json.loads(node.text(include_script=True)))):
                    return True
            except (ValueError, TypeError, RecursionError):
                continue
    return False


def semantic_detail(markup: str) -> dict:
    """Read one locally returned, explicitly labelled introduction, not the body."""
    if not isinstance(markup, str) or len(markup) > 5_000_000:
        raise CrawlError('response_too_large')
    try:
        root = Document(markup).root
        visible = list(_walk(root))
        titles = [_clean(_text(n)) for n, _ in visible if n.tag == 'h1']
        headings = [(n, p) for n, p in visible
                    if n.tag in {'dt','h2','h3','h4'} and _clean(_text(n)) == '职位介绍']
        if len(titles) != 1 or not titles[0] or len(headings) != 1:
            raise CrawlError('structure_changed')
        heading, parent = headings[0]
        if parent is None or parent.tag not in {'dl','section','article','div'}:
            raise CrawlError('structure_changed')
        # Identity, not dataclass equality: two equal-looking heading nodes must
        # not make us extract from the wrong sibling position.
        index = next(i for i, node in enumerate(parent.children) if node is heading)
        fragments = []
        for child in parent.children[index + 1:]:
            if not isinstance(child, Node):
                if child.strip():
                    raise CrawlError('structure_changed')
                continue
            if _hidden(child):
                continue
            if child.tag in _HEADINGS:
                break
            if parent.tag == 'dl' and child.tag != 'dd':
                raise CrawlError('structure_changed')
            if any(n.tag in _HEADINGS for n, _ in _walk(child)):
                # Nested sections need a separately verified layout; do not take
                # an enclosing panel that also contains employer/recommendations.
                raise CrawlError('structure_changed')
            fragments.append(_text(child))
        body = _clean('\n'.join(fragments))
        if _INCOMPLETE.search(body):
            raise CrawlError('jd_incomplete')
        if (_FOREIGN.search(body) or len(body) < 40 or len(body) > 150_000
                or not _JOB_CONTENT.search(body)):
            raise CrawlError('structure_changed')
        return {'title': titles[0], 'text': body, 'parser': 'liepin:semantic_intro:v1'}
    except CrawlError:
        raise
    except (ValueError, TypeError, RecursionError, StopIteration) as exc:
        raise CrawlError('structure_changed') from exc


def _jsonld_whitespace(raw: str) -> tuple[str, bool]:
    """Escape only literal JSON string CR/LF/TAB, not syntax or other controls.

    Some published Liepin HTML serializes description line breaks literally.
    This local representation repair never evaluates JS, unescapes URLs, drops
    duplicate keys, invents braces, or makes a truncated document parseable.
    """
    if len(raw) > 1_000_000:
        raise CrawlError('response_too_large')
    out, quoted, escaped, changed = [], False, False, False
    for char in raw:
        if quoted and not escaped and char in '\r\n\t':
            out.append({'\r': r'\r', '\n': r'\n', '\t': r'\t'}[char])
            changed = True
            continue
        out.append(char)
        if escaped:
            escaped = False
        elif quoted and char == '\\':
            escaped = True
        elif char == '"':
            quoted = not quoted
    return ''.join(out), changed


def _invalid_json_constant(value):
    raise ParseError('non-JSON structured-data value')


def _intro_posting(postings: list[dict], url: str, identity: Callable[[str], str]) -> dict:
    """Select the same platform entity, ignoring only irrelevant query variants.

    The URL family and hostname are part of identity. Metadata remains data:
    no URL is visited and it cannot authorize another host or change the task.
    """
    unique = list({json.dumps(p, sort_keys=True, ensure_ascii=False): p for p in postings}.values())
    target = identity(url)
    matches, references = [], []
    for posting in unique:
        refs = _posting_identity(posting, url)
        references.append(refs)
        try:
            if refs and all(identity(ref) == target for ref in refs):
                matches.append(posting)
        except CrawlError:
            # An unrelated recommended entity may legitimately be present, but
            # it must never become the selected job or grant network permission.
            continue
    if len(matches) == 1:
        return matches[0]
    if len(unique) == 1 and not references[0]:
        return unique[0]
    raise CrawlError('job_identity_mismatch')


def structured_intro_detail(markup: str, url: str,
                            identity: Callable[[str], str]) -> dict | None:
    """Liepin's recorded dd/JSON-LD representation, including pages without h1.

    Return None only when this representation is absent, so established generic
    layouts retain their parser. If present but ambiguous/broken, fail explicitly
    rather than recovering unrelated DOM text. See docs/LIEPIN_RECORDED_LAYOUT.md.
    """
    if not isinstance(markup, str) or len(markup) > 5_000_000:
        raise CrawlError('response_too_large')
    try:
        root = Document(markup).root
        nodes = list(root.walk())
        anchors = [n for n in nodes if n.attrs.get('data-selector') == 'job-intro-content']
        visible_ids = {id(n) for n, _ in _walk(root)}
        postings, repaired_job = [], False
        scripts = [n for n in nodes if n.tag == 'script'
                   and n.attrs.get('type', '').strip().lower() == 'application/ld+json']
        if len(scripts) > 64:
            raise CrawlError('response_too_large')
        for node in scripts:
            raw = node.text(include_script=True)
            normalized, repaired = _jsonld_whitespace(raw)
            try:
                data = json.loads(normalized, object_pairs_hook=_unique_object,
                                  parse_constant=_invalid_json_constant)
                found = list(jobpostings(data))
            except (ValueError, TypeError) as exc:
                # Do not hide an explicitly broken JobPosting behind another
                # script or DOM fallback. Unrelated SEO metadata is not a JD.
                if re.search(r'"@type"\s*:\s*(?:"JobPosting"|\[[^\]]{0,256}"JobPosting")', raw):
                    raise CrawlError('structure_changed') from exc
                continue
            postings.extend(found)
            repaired_job = repaired_job or (repaired and bool(found))
            if len(postings) > 64:
                raise CrawlError('response_too_large')
        if not anchors and not repaired_job:
            return None
        if not postings:
            raise CrawlError('structure_changed')
        posting = _intro_posting(postings, url, identity)
        title, description = posting.get('title'), posting.get('description')
        if not isinstance(title, str) or not isinstance(description, str):
            raise CrawlError('structure_changed')
        if any(ord(c) < 32 and c not in '\r\n\t' for c in title + description):
            raise CrawlError('structure_changed')
        title = unescape(title).strip()
        description = _clean(plain_text(description))
        if not title or len(title) > 500 or len(description) < 20:
            raise CrawlError('jd_incomplete')
        body = description
        if anchors:
            if len(anchors) != 1 or anchors[0].tag != 'dd':
                raise CrawlError('structure_changed')
            anchor = anchors[0]
            if id(anchor) not in visible_ids:
                raise CrawlError('jd_incomplete')
            body = _clean(_text(anchor))
            # A visible complete introduction can extend a structured prefix;
            # incompatible descriptions must not mix metadata from another job.
            compact_body = re.sub(r'\s+', '', body)
            compact_description = re.sub(r'\s+', '', description)
            if not compact_body.startswith(compact_description):
                raise CrawlError('jd_incomplete')
            parser = 'liepin:job_intro_jsonld:v1'
        else:
            parser = 'liepin:jsonld_string_whitespace:v1'
        if _INCOMPLETE.search(body) or _INCOMPLETE.search(description):
            raise CrawlError('jd_incomplete')
        if (len(body) < 40 or len(body) > 150_000 or _FOREIGN.search(body)
                or not _JOB_CONTENT.search(body)):
            raise CrawlError('structure_changed')
        org = posting.get('hiringOrganization')
        company = org.get('name', '') if isinstance(org, dict) else ''
        posted = posting.get('datePosted', '')
        return {'title': title, 'text': body,
                'company': company.strip() if isinstance(company, str) else '',
                'published_at': posted if isinstance(posted, str) else '', 'parser': parser}
    except CrawlError:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise CrawlError('structure_changed') from exc


class LiepinAdapter(DOMAdapter):
    """Site-specific entity checks without widening the base access contract."""

    def job_identity(self, url: str) -> str:
        accepted = self.accept_url(url, detail=True)
        p = urlsplit(accepted)
        match = re.fullmatch(r'/(job|a)/([A-Za-z0-9_-]{1,80})\.(?:shtml|html)|/lptjob/([0-9]{1,80})', p.path)
        if not match:
            raise CrawlError('not_job_url')
        kind, ident = (match[1], match[2]) if match[3] is None else ('lptjob', match[3])
        # Different URL families and hostnames are NOT assumed to be aliases.
        return f'{self.key}:{p.hostname}:{kind}:{ident}'

    def cards(self, page: PageSnapshot) -> list[Card]:
        if urlsplit(page.url).path.rstrip('/') != urlsplit(self.search_base).path.rstrip('/'):
            # Challenge URLs keep their meaningful existing error classification.
            if self.challenged('', page.url):
                raise CrawlError('manual_required')
            raise CrawlError('not_job_list')
        # Preserve the complete legacy card sequence and IDs, including tracking
        # variants: persisted page signatures and selections depend on them.
        # Entity-level cross-page/task deduplication belongs to D05, not a silent
        # migration during detail-parser rollout.
        if self.challenged(surface_text(page), page.url):
            raise CrawlError('manual_required')
        from .liepin_search import observed_cards
        observed = observed_cards(self, page)
        return observed if observed is not None else super().cards(page)

    def native_request_context(self, operation, request, page_url):
        from .liepin_search import request_context
        return request_context(self, operation, request, page_url)

    def native_ready(self, observations):
        return any(o.operation == 'liepin_search' for o in observations)

    def confirmed_empty(self, page):
        from .liepin_search import observed_cards
        return observed_cards(self, page) == []

    def validate_detail_identity(self, expected_url: str, page: PageSnapshot) -> None:
        expected = self.job_identity(expected_url)
        if self.job_identity(page.url) != expected:
            raise CrawlError('job_identity_mismatch')
        # Canonical declarations are checked as data, never followed or trusted
        # as permission. Conflicting declarations must remain visible as failure.
        if not isinstance(page.html, str) or len(page.html) > 5_000_000:
            raise CrawlError('response_too_large')
        try:
            root = Document(page.html).root
        except (ValueError, RecursionError) as exc:
            raise CrawlError('structure_changed') from exc
        for node in root.walk():
            if node.tag == 'link' and 'canonical' in node.attrs.get('rel', '').lower().split():
                href = node.attrs.get('href', '')
                if not href or self.job_identity(urljoin(page.url, href)) != expected:
                    raise CrawlError('job_identity_mismatch')

    def detail(self, page: PageSnapshot) -> dict:
        self.job_identity(page.url)
        self.validate_detail_identity(page.url, page)
        visible = surface_text(page)
        if re.search(r'该职位已(?:暂停|停止|结束)招聘|职位已下线|职位已关闭', visible):
            raise CrawlError('job_unavailable')
        if self.challenged(visible, page.url):
            raise CrawlError('manual_required')
        intro = structured_intro_detail(page.html, page.url, self.job_identity)
        if intro is not None:
            return intro
        try:
            parsed = super().detail(page)
            if _INCOMPLETE.search(parsed['text']):
                raise CrawlError('jd_incomplete')
            return parsed
        except CrawlError as exc:
            if exc.code != 'structure_changed':
                raise
            if _has_structured_job(Document(page.html).root):
                raise
            return semantic_detail(page.html)
