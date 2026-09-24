"""Authored adjacent-page contracts; no copied jobs or live network."""
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from vibe_job_radar.collection import Collector, TERMINAL
from vibe_job_radar.network import FetchError, Response, SiteFetcher
from vibe_job_radar.public_category import URL, get_category, parse_category, parse_category_page
from vibe_job_radar.workspace import Workspace, InputError
from test_public_category import Wire, card, data, job_url, listing


def page_html(page=0, numbers=(0, 1, 2), *, cards=None, disabled=False, category_id='architect'):
    category = get_category(category_id)
    title = f'【{category.name}招聘_招聘{category.name}人才】-猎聘' + (f'-第{page+1}页' if page else '')
    pager = '<ul class="ant-pagination">'
    for n in numbers:
        active = ' ant-pagination-item-active' if n == page else ''
        pager += (f'<li class="ant-pagination-item{active}" title="{n+1}"><a '
                  f'data-selector="pagintion-item-selector" data-currentpage="{n}" '
                  f'href="{category.url}pn{n}/">{n+1}</a></li>')
    # The publisher also exposes a jump-icon link: it is not a numbered next page.
    pager += f'<li class="ant-pagination-jump-next"><a href="{category.url}pn{page}2/"></a></li>'
    pager += '<li class="ant-pagination-next' + (' ant-pagination-disabled' if disabled else '') + '"><a></a></li></ul>'
    html = listing(cards if cards is not None else ''.join(card(n) for n in range(11,17)), decoy=pager)
    html = html.replace('【架构师招聘_招聘架构师人才】-猎聘', title)
    return html.replace('</title>', '</title><link rel="canonical" href="'+category.url+'">', 1)


class CategoryPageParserTests(unittest.TestCase):
    def test_first_and_adjacent_identity_with_same_canonical_and_explicit_next(self):
        for key in ('architect', 'algorithm'):
            category = get_category(key)
            for page in (0, 1):
                with self.subTest(category=key, page=page):
                    url = category.url if not page else category.url+'pn1/'
                    rows, paging = parse_category_page(url, page_html(page, category_id=key), key, page=page)
                    self.assertEqual(len(rows), 6)
                    self.assertEqual(paging['page'], page)
                    self.assertEqual(paging['url'], url)
                    self.assertEqual(paging['status'], 'available')
                    self.assertEqual(paging['next_url'], category.url+f'pn{page+1}/')
        with self.assertRaises(FetchError):
            parse_category(URL+'pn1/', page_html(1))

    def test_missing_legacy_pager_cannot_authorize_another_page(self):
        rows, paging = parse_category_page(URL, listing())
        self.assertEqual(len(rows), 6)
        self.assertEqual(paging['status'], 'unavailable')
        self.assertEqual(paging['next_url'], '')
        html = page_html(1)
        html = html[:html.index('<ul class="ant-pagination">')] + '</div></body></html>'
        with self.assertRaises(FetchError):parse_category_page(URL+'pn1/', html, page=1)

    def test_title_url_active_index_or_canonical_disagreement_rejects_page(self):
        html = page_html(1)
        for changed in (html.replace('-第2页',''), html.replace('-第2页','-第3页'),
                        html.replace('title="2"','title="3"'),
                        html.replace('data-currentpage="1"','data-currentpage="2"'),
                        html.replace('rel="canonical"','rel="other"'),
                        html.replace('href="'+URL+'"','href="https://example.invalid/"'),
                        html.replace(' ant-pagination-item-active',''),
                        html.replace('class="ant-pagination-item" title="1"','class="ant-pagination-item ant-pagination-item-active" title="1"')):
            with self.subTest(changed=changed[:60]), self.assertRaises(FetchError):
                parse_category_page(URL+'pn1/', changed, page=1)
        with self.assertRaises(FetchError):parse_category_page(URL+'pn2/', html, page=1)

    def test_foreign_or_ambiguous_numbered_link_never_becomes_next_url(self):
        html = page_html()
        for replacement in ('https://example.invalid/page2', URL+'pn1/?token=secret',
                            'https://www.liepin.com/career/suanfakaifa/pn1/', URL+'pn3/'):
            with self.subTest(replacement=replacement):
                rows, paging = parse_category_page(URL, html.replace(URL+'pn1/', replacement))
                self.assertEqual(len(rows), 6)
                self.assertEqual(paging['status'], 'unavailable')
                self.assertEqual(paging['next_url'], '')
        _, paging = parse_category_page(URL, page_html(numbers=(0,1,1)))
        self.assertEqual(paging['status'], 'unavailable')

    def test_only_explicit_disabled_next_can_establish_terminal_page(self):
        for disabled, expected in ((True,'terminal'), (False,'unavailable')):
            _, paging = parse_category_page(URL+'pn1/', page_html(1,numbers=(0,1),disabled=disabled),page=1)
            self.assertEqual(paging['status'], expected)
        with self.assertRaises(FetchError):
            parse_category_page(URL+'pn1/',page_html(1,disabled=True),page=1)

    def test_fifth_page_limit_is_distinct_from_publisher_terminal(self):
        _, paging = parse_category_page(URL+'pn4/',page_html(4,numbers=(2,3,4,5)),page=4)
        self.assertEqual(paging['status'],'limit')
        self.assertEqual(paging['next_url'],'')


class PageWire(Wire):
    def __init__(self, first=None, second=None, **kwargs):
        super().__init__(first or page_html(cards=''.join(card(n) for n in range(1,6))), **kwargs)
        self.pages = {URL+'pn1/': second or page_html(1)}

    def public_get(self, url):
        if url in self.pages:
            self.calls.append(url)
            return Response(self.statuses.get(url,200), {'content-type':'text/html'}, self.pages[url].encode(), url)
        return super().public_get(url)


class CategoryPageCollectionTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.collector=Collector(self.workspace)

    def finish(self, state, wire):
        self.collector.clients[(state['id'],'liepin')]=SiteFetcher({'liepin.com'},transport=wire)
        for _ in range(12):
            state=self.collector.step({'id':state['id']})
            if state['status'] in TERMINAL:return state
        self.fail('bounded batch did not complete')

    def first(self, wire=None, **changes):
        return self.finish(self.collector.start({**data(),**changes}),wire or PageWire())

    def next_page(self, parent):
        plan=self.collector.category_page_preview({'id':parent['id']})
        result=self.collector.category_page_start(dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True))
        return plan,result

    def test_adjacent_page_keeps_original_source_report_and_exact_batch(self):
        first=self.first()
        raw=self.collector._path(first['id']).read_bytes()
        old=self.workspace.root/'reports'/first['report_id']
        hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in old.iterdir() if p.is_file()}
        with patch('vibe_job_radar.collection.SiteFetcher',side_effect=AssertionError('preview/start is offline')):
            plan,result=self.next_page(first)
        self.assertTrue(plan['can_start']);self.assertEqual(plan['next_page'],2)
        self.assertEqual(plan['next_url'],URL+'pn1/');self.assertEqual(plan['external_network_requests'],0)
        self.assertEqual(result['task']['phase'],'category');self.assertEqual(result['task']['status'],'paused')
        self.assertEqual(result['task']['category_attempts'],0)
        self.assertIn('第 1 页',first['route_label'])
        self.assertIn('第 2 页',result['task']['route_label'])
        wire=PageWire();second=self.finish(result['task'],wire)
        self.assertEqual(second['status'],'completed')
        self.assertEqual(second['category_attempts'],1);self.assertEqual(second['detail_attempts'],5)
        self.assertEqual(wire.calls,['https://www.liepin.com/robots.txt',URL+'pn1/']+[job_url(n) for n in range(11,16)])
        self.assertEqual(second['category_outcomes'][0]['page_snapshot']['page'],1)
        self.assertIn('第 2 页',second['user_summary'])
        self.assertNotIn('第一页',second['user_summary'])
        self.assertEqual(self.collector._path(first['id']).read_bytes(),raw)
        self.assertEqual(hashes,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in old.iterdir() if p.is_file()})
        manifest=json.loads((self.workspace.root/'reports'/second['report_id']/'run_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['stats']['full_text_job_groups'],5)

    def test_restart_and_duplicate_submit_keep_the_same_child_without_replay(self):
        first=self.first();plan,result=self.next_page(first)
        self.collector=Collector(self.workspace)
        again=self.collector.category_page_start(dict(id=first['id'],fingerprint=plan['fingerprint'],consent=True))
        self.assertFalse(again['created']);self.assertEqual(again['task']['id'],result['task']['id'])
        second=self.finish(again['task'],PageWire())
        before=self.collector._path(second['id']).read_bytes()
        again=self.collector.category_page_start(dict(id=first['id'],fingerprint=plan['fingerprint'],consent=True))
        self.assertEqual(again['task']['report_id'],second['report_id'])
        self.assertEqual(self.collector._path(second['id']).read_bytes(),before)

    def test_previous_page_duplicates_stay_in_audit_and_same_page_tail_still_works(self):
        first=self.first();_,result=self.next_page(first)
        second_html=page_html(1,cards=card(1)+card(1)+''.join(card(n) for n in range(11,17)))
        wire=PageWire(second=second_html);second=self.finish(result['task'],wire)
        rows=second['category_outcomes'][0]['candidates']
        self.assertEqual([r['status'] for r in rows[:2]],['previous_page_duplicate','duplicate'])
        self.assertEqual(second['category_outcomes'][0]['selected_positions'],[3,4,5,6,7])
        self.assertNotIn(job_url(1),wire.calls)
        page_plan=self.collector.category_page_preview({'id':second['id']})
        self.assertTrue(page_plan['can_start']);self.assertEqual(page_plan['remaining_on_current_page'],1)
        plan=self.collector.category_next_preview({'id':second['id']})
        self.assertEqual([r['position'] for r in plan['items']],[8])
        result=self.collector.category_next_start(dict(id=second['id'],fingerprint=plan['fingerprint'],consent=True))
        wire=PageWire();last=self.finish(result['task'],wire)
        self.assertEqual(wire.calls,['https://www.liepin.com/robots.txt',job_url(16)])
        self.assertEqual(last['category_page_context'],second['category_page_context'])
        following=self.collector.category_page_preview({'id':last['id']})
        self.assertTrue(following['can_start']);self.assertEqual(following['next_url'],URL+'pn2/')

    def test_repeated_page_stops_without_detail_fetch_or_historical_report(self):
        first=self.first();_,result=self.next_page(first)
        wire=PageWire(second=page_html(1,cards=''.join(card(n) for n in range(1,6))))
        second=self.finish(result['task'],wire)
        self.assertEqual(second['status'],'needs_attention')
        self.assertEqual(second['category_outcomes'][0]['status'],'category_repeated_page')
        self.assertEqual(second['details'],[]);self.assertEqual(second['report_id'],'')
        self.assertEqual(wire.calls,['https://www.liepin.com/robots.txt',URL+'pn1/'])
        with self.assertRaises(InputError):self.collector.category_page_preview({'id':second['id']})

    def test_wrong_page_body_never_supplies_details(self):
        first=self.first();_,result=self.next_page(first)
        wire=PageWire(second=page_html(0));second=self.finish(result['task'],wire)
        self.assertEqual(second['category_outcomes'][0]['status'],'category_identity_mismatch')
        self.assertEqual(second['detail_attempts'],0);self.assertEqual(second['report_id'],'')
        self.assertEqual(len(wire.calls),2)

    def test_remaining_list_is_preserved_but_legacy_evidence_cannot_authorize_a_page(self):
        first=self.first(wire=PageWire(first=page_html(cards=''.join(card(n) for n in range(1,7)))))
        before=self.collector._path(first['id']).read_bytes()
        plan=self.collector.category_page_preview({'id':first['id']})
        self.assertTrue(plan['can_start']);self.assertEqual(plan['remaining_on_current_page'],1)
        self.assertEqual([r['position'] for r in self.collector.category_next_preview({'id':first['id']})['items']],[6])
        _,child=self.next_page(first)
        self.assertEqual(child['task']['detail_budget'],5)
        self.assertEqual(self.collector._path(first['id']).read_bytes(),before)
        raw=self.collector._load(first['id']);raw['category_outcomes'][0].pop('page_snapshot');self.collector._save(raw)
        plan=self.collector.category_page_preview({'id':first['id']})
        self.assertFalse(plan['can_start']);self.assertEqual(plan['code'],'legacy_no_pagination')
        with self.assertRaises(InputError):
            self.collector.category_page_start(dict(id=first['id'],fingerprint=plan['fingerprint'],consent=True))

    def test_refusal_remains_a_hard_stop_for_page_continuation(self):
        first=self.first(wire=PageWire(statuses={job_url(1):403}))
        before=self.collector._path(first['id']).read_bytes()
        with self.assertRaises(InputError):self.collector.category_page_preview({'id':first['id']})
        self.assertEqual(self.collector._path(first['id']).read_bytes(),before)

    def test_unapproved_fields_or_stale_preview_create_no_task(self):
        first=self.first();plan=self.collector.category_page_preview({'id':first['id']})
        payload=dict(id=first['id'],fingerprint=plan['fingerprint'],consent=True)
        for changed in ({'consent':False},{'url':URL+'pn3/'},{'page':3},{'fingerprint':'0'*64}):
            with self.subTest(changed=changed),self.assertRaises(InputError):
                self.collector.category_page_start({**payload,**changed})
        raw=self.collector._load(first['id']);raw['warnings'].append('authored change');self.collector._save(raw)
        with self.assertRaises(InputError):self.collector.category_page_start(payload)
        self.assertEqual(len(self.collector.list()['runs']),1)

    def test_changed_parent_or_cyclic_child_is_refused_before_network(self):
        first=self.first();_,result=self.next_page(first)
        child=self.collector._load(result['task']['id'])
        raw=self.collector._load(first['id']);raw['warnings'].append('authored change');self.collector._save(raw)
        with patch('vibe_job_radar.collection.SiteFetcher',side_effect=AssertionError('must not request')),self.assertRaises(InputError):
            self.collector.step({'id':child['id']})
        child['category_page_context']['parent_id']=child['id'];self.collector._save(child)
        with patch('vibe_job_radar.collection.SiteFetcher',side_effect=AssertionError('must not request')),self.assertRaises(InputError):
            self.collector.step({'id':child['id']})

    def test_corrupted_pagination_url_is_not_authorization(self):
        first=self.first()
        raw=self.collector._load(first['id']);raw['category_outcomes'][0]['page_snapshot']['next_url']='https://example.invalid/page'
        self.collector._save(raw)
        with self.assertRaises(InputError):self.collector.category_page_preview({'id':first['id']})

    def test_changed_parent_blocks_detail_resume_after_the_page_was_read(self):
        first=self.first();_,result=self.next_page(first)
        child=result['task'];wire=PageWire()
        self.collector.clients[(child['id'],'liepin')]=SiteFetcher({'liepin.com'},transport=wire)
        child=self.collector.step({'id':child['id']})
        self.assertEqual(child['phase'],'detail');self.assertEqual(child['detail_attempts'],0)
        before=self.collector._path(child['id']).read_bytes()
        raw=self.collector._load(first['id']);raw['warnings'].append('changed while paused');self.collector._save(raw)
        with self.assertRaises(InputError):self.collector.step({'id':child['id']})
        self.assertEqual(wire.calls,['https://www.liepin.com/robots.txt',URL+'pn1/'])
        self.assertEqual(self.collector._path(child['id']).read_bytes(),before)

    def test_changed_ancestor_also_blocks_later_same_page_batches(self):
        first=self.first();_,result=self.next_page(first)
        second=self.finish(result['task'],PageWire())
        raw=self.collector._load(first['id']);raw['warnings'].append('authored source change');self.collector._save(raw)
        with self.assertRaises(InputError):self.collector.category_next_preview({'id':second['id']})

    def test_multiple_pages_keep_ancestry_and_stop_at_five_page_limit(self):
        first_html=page_html(cards=card(1),numbers=tuple(range(6)))
        current=self.first(wire=PageWire(first=first_html),detail_budget=1)
        for page in range(1,5):
            plan,result=self.next_page(current)
            wire=PageWire();url=URL+f'pn{page}/'
            wire.pages[url]=page_html(page,cards=card(page*10+1),numbers=tuple(range(6)))
            current=self.finish(result['task'],wire)
            self.assertEqual(current['status'],'completed')
            self.assertEqual(current['category_page_context']['page'],page)
            self.assertEqual(current['detail_attempts'],1)
        plan=self.collector.category_page_preview({'id':current['id']})
        self.assertFalse(plan['can_start']);self.assertEqual(plan['code'],'page_limit')
        self.assertEqual(len(self.collector.list()['runs']),5)

    def test_algorithm_category_keeps_original_roles_on_its_own_page_path(self):
        category=get_category('algorithm')
        wire=PageWire();wire.pages[category.url]=page_html(cards=card(1),category_id='algorithm')
        first=self.first(wire=wire,roles=list(category.roles),category_id='algorithm')
        plan,result=self.next_page(first)
        self.assertEqual(plan['next_url'],category.url+'pn1/')
        self.assertEqual(result['task']['roles'],list(category.roles))
        wire=PageWire();wire.pages[category.url+'pn1/']=page_html(1,category_id='algorithm')
        second=self.finish(result['task'],wire)
        self.assertEqual(second['status'],'completed')
        self.assertEqual(second['category_id'],'algorithm')
