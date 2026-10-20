"""Reject explicit expansion controls in an otherwise parsed DOM job body."""
from __future__ import annotations

import re

from ..html_parser import Document, Node
from .contracts import CrawlError
from .page_surface import _text

_PROMPT = re.compile(
    r'(?:(?:点击)?(?:展开|查看)(?:全部|更多|完整)(?:职位描述|岗位描述|职位|描述)?|'
    r'(?:show|view|read)\s+(?:the\s+)?(?:full|complete)\s+job\s+description)',
    re.I,
)


def _visible_nodes(node):
    if node.tag in {'script', 'style', 'template', 'noscript', 'head'} or 'hidden' in node.attrs:
        return
    style = re.sub(r'\s+', '', node.attrs.get('style', '')).lower()
    if re.search(r'(?:^|;)(?:display:none|visibility:(?:hidden|collapse))(?:!important)?(?:;|$)', style):
        return
    yield node
    for child in node.children:
        if isinstance(child, Node):
            yield from _visible_nodes(child)


def require_expanded(markup, parsed):
    # A paragraph about designing an "expand" button is ordinary job content.
    # Only a standalone instruction or an actual control in the selected body
    # qualifies; unrelated recommendations/login dialogs are outside this scope.
    if not parsed['parser'].startswith('dom:'):
        if any(_PROMPT.fullmatch(line.strip()) for line in parsed['text'].splitlines()):
            raise CrawlError('jd_incomplete')
        return
    selector = parsed['parser'].removeprefix('dom:')
    for container in _visible_nodes(Document(markup).root):
        if selector not in container.attrs.get('class', '').split():
            continue
        if any(_PROMPT.fullmatch(line.strip()) for line in _text(container).splitlines()):
            raise CrawlError('jd_incomplete')
        for node in _visible_nodes(container):
            if ((node.tag in {'button', 'a'} or node.attrs.get('role') == 'button')
                    and _PROMPT.fullmatch(node.text().strip())):
                raise CrawlError('jd_incomplete')
