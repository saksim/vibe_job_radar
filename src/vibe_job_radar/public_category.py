"""Explicit publisher-linked career pages observed on 2026-09-24.

This is an explicit collection route, not a fallback for a refused search.
Only main-list cards are candidates. Adjacent pagination uses explicit numbered
links and matching page identity; no query or recommendation endpoint is inferred.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .html_parser import Document, Node
from .network import FetchError
from .public_job_links import public_detail_parser
from .workspace import InputError

MODE = 'liepin_category'
URL = 'https://www.liepin.com/career/360321/'
LABEL = '猎聘架构师公开分类'
PARSER = 'liepin_architect_category_v1'
MAX_DETAILS = 5
MAX_CATEGORY_PAGES = 5
PAGINATION_PARSER = 'liepin_category_pagination_v1'


@dataclass(frozen=True)
class Category:
    key: str
    url: str
    name: str
    parser: str
    roles: tuple[str, ...]

    @property
    def label(self):
        return f'猎聘{self.name}公开分类'


CATEGORIES = {
    'architect': Category('architect', URL, '架构师', PARSER, ('architect',)),
    'algorithm': Category('algorithm', 'https://www.liepin.com/career/suanfakaifa/',
                          '算法工程师', 'liepin_algorithm_category_v1', ('domain_algorithm', 'time_series')),
}


def get_category(key='architect') -> Category:
    if not isinstance(key, str) or key not in CATEGORIES:
        raise InputError('请选择已支持的猎聘公开分类；不能传入其他分类地址。')
    return CATEGORIES[key]


def category_for_state(state) -> Category:
    # Missing key is only the original architect checkpoint format.
    category = get_category(state.get('category_id', 'architect'))
    roles = state.get('roles')
    if (not isinstance(roles, list) or any(not isinstance(role, str) for role in roles)
            or set(roles) != set(category.roles) or state.get('platforms') != ['liepin']):
        raise InputError('保存的分类与岗位或来源范围不一致，不能继续访问。')
    outcomes = state.get('category_outcomes')
    if (not isinstance(outcomes, list) or len(outcomes) != 1 or not isinstance(outcomes[0], dict)
            or outcomes[0].get('url') != category_page_url(category, page_for_state(state))
            or outcomes[0].get('parser') != category.parser):
        raise InputError('保存的分类身份与解析版本不一致，不能继续访问。')
    return category


def page_for_state(state):
    if 'category_page_context' not in state:
        return 0
    context = state['category_page_context']
    if (not isinstance(context, dict) or set(context) != {
            'version', 'page', 'url', 'parent_id', 'parent_fingerprint', 'seen_urls', 'visited_urls'}
            or type(context['version']) is not int or context['version'] != 1
            or type(context['page']) is not int or not 1 <= context['page'] < MAX_CATEGORY_PAGES
            or not isinstance(context['parent_id'], str) or not re.fullmatch('[a-f0-9]{32}', context['parent_id'])
            or not isinstance(context['parent_fingerprint'], str) or not re.fullmatch('[a-f0-9]{64}', context['parent_fingerprint'])):
        raise InputError('保存的分页来源关系无效，未扩大访问范围。')
    category = get_category(state.get('category_id', 'architect'))
    if context['url'] != category_page_url(category, context['page']):
        raise InputError('保存的页号与分类地址不一致。')
    expected_visited = [category_page_url(category, n) for n in range(context['page'])]
    if context['visited_urls'] != expected_visited:
        raise InputError('保存的分页路径重复、跳页或跨分类。')
    urls = context['seen_urls']
    if (not isinstance(urls, list) or not 1 <= len(urls) <= 100 * context['page']
            or any(not isinstance(url, str) or not re.fullmatch(r'https://www\.liepin\.com/(?:job|a)/[0-9]{1,80}\.shtml', url) for url in urls)
            or len(set(urls)) != len(urls)):
        raise InputError('此前页面的岗位集合不完整或重复，不能可靠去重。')
    return context['page']


def _class(node, name):
    return name in node.attrs.get('class', '').split()


def _only(nodes):
    values = list(nodes)
    if len(values) != 1:
        raise FetchError('category_structure_changed')
    return values[0]


def category_page_url(category, page):
    """Validate known page identity; navigation must still use an observed link."""
    if type(page) is not int or not 0 <= page < MAX_CATEGORY_PAGES:
        raise InputError('本次公开分类最多支持前5页，请保留实际来源范围。')
    return category.url if page == 0 else f'{category.url}pn{page}/'


def _parse_category(final_url, markup, category_id, page):
    category = get_category(category_id)
    if final_url != category_page_url(category, page):
        raise FetchError('category_identity_mismatch')
    root = Document(markup).root
    document = _only(n for n in root.children if isinstance(n, Node) and n.tag == 'html')
    head = _only(n for n in document.children if isinstance(n, Node) and n.tag == 'head')
    title = _only(n for n in head.children if isinstance(n, Node) and n.tag == 'title').text().strip()
    expected_title = f'【{category.name}招聘_招聘{category.name}人才】-猎聘' + (f'-第{page+1}页' if page else '')
    if title != expected_title:
        raise FetchError('category_identity_mismatch')
    canonicals = [node for node in head.children if isinstance(node, Node) and node.tag == 'link'
                  and 'canonical' in node.attrs.get('rel', '').lower().split()]
    if (page and len(canonicals) != 1) or any(n.attrs.get('href') != category.url for n in canonicals):
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
    return rows, main


def parse_category(final_url: str, markup: str, category_id='architect') -> list[dict]:
    """Keep the original first-page-only parser contract for existing callers."""
    return _parse_category(final_url, markup, category_id, 0)[0]


def _pagination(main, category, page):
    pager = _only(n for n in main.walk() if n.tag == 'ul' and _class(n, 'ant-pagination'))
    active = _only(n for n in pager.children if isinstance(n, Node) and n.tag == 'li'
                   and _class(n, 'ant-pagination-item-active'))
    numbered = {}
    for item in pager.children:
        if not isinstance(item, Node) or item.tag != 'li' or not _class(item, 'ant-pagination-item'):
            continue
        anchor = _only(n for n in item.walk() if n.tag == 'a')
        label = anchor.text().strip()
        if not re.fullmatch(r'[1-9][0-9]{0,2}', label):
            raise FetchError('category_pagination_invalid')
        index = int(label) - 1
        if (index in numbered or item.attrs.get('title') != label
                or anchor.attrs.get('data-currentpage') != str(index)
                or anchor.attrs.get('data-selector') != 'pagintion-item-selector'
                or anchor.attrs.get('href') != f'{category.url}pn{index}/'):
            raise FetchError('category_pagination_invalid')
        numbered[index] = anchor.attrs['href']
        if item is active and index != page:
            raise FetchError('category_pagination_invalid')
    if page not in numbered or not _class(active, 'ant-pagination-item'):
        raise FetchError('category_pagination_invalid')
    next_items = [n for n in pager.children if isinstance(n, Node) and n.tag == 'li' and _class(n, 'ant-pagination-next')]
    if len(next_items) > 1:
        raise FetchError('category_pagination_invalid')
    disabled = bool(next_items and _class(next_items[0], 'ant-pagination-disabled'))
    if disabled and page + 1 in numbered:
        raise FetchError('category_pagination_invalid')
    if page + 1 == MAX_CATEGORY_PAGES:
        return 'limit', ''
    if page + 1 in numbered:
        return 'available', numbered[page + 1]
    if disabled:
        return 'terminal', ''
    return 'unavailable', ''


def parse_category_page(final_url, markup, category_id='architect', *, page=0):
    """A usable adjacent page needs URL, title, canonical and active-index proof."""
    rows, main = _parse_category(final_url, markup, category_id, page)
    pagination = dict(parser=PAGINATION_PARSER, page=page, url=final_url, status='unavailable', next_url='')
    try:
        status, next_url = _pagination(main, get_category(category_id), page)
        pagination.update(status=status, next_url=next_url)
    except FetchError:
        # Legacy/missing pagination does not invalidate an otherwise verified
        # first-page list, but cannot authorize another page.
        if page:
            raise FetchError('category_pagination_invalid') from None
    return rows, pagination


def new_outcome(category_id='architect', *, page=0) -> dict:
    category = get_category(category_id)
    return dict(url=category_page_url(category, page), label=category.label, parser=category.parser, source_scope='career_category',
                submitted_keyword=False, pagination=page > 0, status='pending', candidates=[])
