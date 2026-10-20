"""Reject explicit expansion controls in an otherwise parsed DOM job body."""
from __future__ import annotations

import re

from ..html_parser import Document
from .contracts import CrawlError
from .page_surface import _text

_PROMPT = re.compile(
    r'(?:(?:点击)?(?:展开|查看)(?:全部|更多|完整)(?:职位描述|岗位描述|职位|描述)?|'
    r'(?:show|view|read)\s+(?:the\s+)?(?:full|complete)\s+job\s+description)',
    re.I,
)


def _visible_nodes(node):
    yield from node.walk(visible_only=True)


def require_expanded(markup, parsed, *, visible_text=None):
    # A paragraph about designing an "expand" button is ordinary job content.
    # Only a standalone instruction or an actual control in the selected body
    # qualifies; unrelated recommendations/login dialogs are outside this scope.
    if not parsed['parser'].startswith('dom:'):
        if any(_PROMPT.fullmatch(line.strip()) for line in parsed['text'].splitlines()):
            raise CrawlError('jd_incomplete')
        return
    # Live browser innerText reflects stylesheet/computed visibility. A DOM
    # template absent from that observation is not an active expand control.
    # Offline snapshots keep the conservative HTML-only fallback.
    rendered = None if visible_text is None else ' '.join(visible_text.split()).casefold()
    if rendered == '':
        raise CrawlError('jd_incomplete')
    def active_prompt(value):
        value = value.strip()
        return (_PROMPT.fullmatch(value) and
                (rendered is None or ' '.join(value.split()).casefold() in rendered))
    selector = parsed['parser'].removeprefix('dom:')
    for container in _visible_nodes(Document(markup).root):
        if selector not in container.attrs.get('class', '').split():
            continue
        if any(active_prompt(line) for line in _text(container).splitlines()):
            raise CrawlError('jd_incomplete')
        for node in _visible_nodes(container):
            if ((node.tag in {'button', 'a'} or node.attrs.get('role') == 'button')
                    and active_prompt(node.text())):
                raise CrawlError('jd_incomplete')
