"""Use the rendered page for login detection without changing JD extraction.

HTML contains dormant login dialogs as well as the visible job. A live browser
already checks its innerText; adapters must consume that same observation rather
than reclassifying hidden HTML as an active login wall. Offline snapshots use a
conservative fallback which only omits explicitly hidden markup.
"""
from __future__ import annotations

import re

from ..html_parser import Document, Node
from .contracts import CrawlError, PageSnapshot

_INERT = {'script', 'style', 'template', 'noscript', 'head'}


def _text(node: Node) -> str:
    if node.tag in _INERT or 'hidden' in node.attrs:
        return ''
    style = re.sub(r'\s+', '', node.attrs.get('style', '')).lower()
    if re.search(r'(?:^|;)(?:display:none|visibility:(?:hidden|collapse))(?:!important)?(?:;|$)', style):
        return ''
    text = ''.join(_text(child) if isinstance(child, Node) else child for child in node.children)
    return text + ('\n' if node.tag in {'p', 'div', 'li', 'br', 'section', 'h1', 'h2', 'h3'} else '')


def surface_text(page: PageSnapshot) -> str:
    if page.visible_text is not None:
        if not isinstance(page.visible_text, str) or len(page.visible_text) > 5_000_000:
            raise CrawlError('invalid_page_observation')
        return page.visible_text
    if not isinstance(page.html, str) or len(page.html) > 5_000_000:
        raise CrawlError('response_too_large')
    try:
        return _text(Document(page.html).root)
    except (ValueError, RecursionError) as exc:
        raise CrawlError('structure_changed') from exc
