"""The single publisher-linked career page verified on 2026-09-24.

This is an explicit collection route, not a fallback for a refused search.
Only main-list cards are candidates; no query, pagination or recommendation
endpoint is inferred from this page.
"""
from __future__ import annotations

import re

from .html_parser import Document, Node
from .network import FetchError
from .public_job_links import public_detail_parser

MODE = 'liepin_category'
URL = 'https://www.liepin.com/career/360321/'
LABEL = '猎聘架构师公开分类'
PARSER = 'liepin_architect_category_v1'
MAX_DETAILS = 5


def _class(node, name):
    return name in node.attrs.get('class', '').split()


def _only(nodes):
    values = list(nodes)
    if len(values) != 1:
        raise FetchError('category_structure_changed')
    return values[0]


def parse_category(final_url: str, markup: str) -> list[dict]:
    if final_url != URL:
        raise FetchError('category_identity_mismatch')
    root = Document(markup).root
    document = _only(n for n in root.children if isinstance(n, Node) and n.tag == 'html')
    head = _only(n for n in document.children if isinstance(n, Node) and n.tag == 'head')
    title = _only(n for n in head.children if isinstance(n, Node) and n.tag == 'title').text().strip()
    if title != '【架构师招聘_招聘架构师人才】-猎聘':
        raise FetchError('category_identity_mismatch')
    for node in head.children:
        if isinstance(node, Node) and node.tag == 'link' and 'canonical' in node.attrs.get('rel', '').lower().split():
            if node.attrs.get('href') != URL:
                raise FetchError('category_identity_mismatch')
    main = _only(n for n in root.walk() if n.attrs.get('id') == 'main-container')
    left = _only(n for n in main.walk() if _class(n, 'left-job-box'))
    jobs = _only(n for n in left.walk() if _class(n, 'job-list-box'))
    listing = _only(n for n in jobs.walk() if _class(n, 'left-list-box'))
    cards = [n for n in listing.walk() if _class(n, 'job-card-pc-container')]
    if not cards:
        # No observed publisher empty-state contract: never report zero jobs.
        raise FetchError('category_no_confirmed_jobs')
    if len(cards) > 100:
        raise FetchError('category_structure_changed')
    rows, seen = [], {}
    for position, card in enumerate(cards, 1):
        row = dict(position=position, title='', url='', status='category_invalid_card')
        rows.append(row)
        anchors = [n for n in card.walk() if n.tag == 'a' and n.attrs.get('data-nick') == 'job-detail-job-info']
        if len(anchors) != 1:
            continue
        anchor = anchors[0]
        # Do not remove unknown parameters or retain tracking attribute values.
        url = anchor.attrs.get('href', '')
        if not re.fullmatch(r'https://www\.liepin\.com/(?:job|a)/[0-9]{1,80}\.shtml', url):
            continue
        titles = [n for box in anchor.walk() if _class(box, 'job-title-box')
                  for n in box.walk() if n.tag == 'div' and _class(n, 'ellipsis-1') and n.attrs.get('title')]
        if len(titles) != 1:
            continue
        node = titles[0]
        title = ' '.join(node.text().split())
        if not title or len(title) > 200 or title != ' '.join(node.attrs['title'].split()):
            continue
        row.update(title=title, url=url)
        if url in seen:
            row.update(status='duplicate', duplicate_of=seen[url])
            continue
        seen[url] = position
        row['status'] = 'available' if public_detail_parser(url) else 'category_unsupported_detail'
    return rows


def new_outcome() -> dict:
    return dict(url=URL, label=LABEL, parser=PARSER, source_scope='career_category',
                submitted_keyword=False, pagination=False, status='pending', candidates=[])
