"""Independent artificial request data only; no network or real account."""
import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.adapters import builtins, Registry, DOMAdapter
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.native_browser import BusinessObservation, NativeBackend
from vibe_job_radar.guided.native_policy import contract_for
from vibe_job_radar.guided.liepin_search import request_context
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.rate import RateLedger, Limits
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.store import Store
from test_liepin_recorded_layout import markup, posting, BODY

ADAPTER = builtins().get('liepin')
SEARCH = ADAPTER.search_url('时间序列')
JOB = 'https://www.liepin.com/job/123.shtml'
API = 'https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-job'


def request(**updates):
    data = {'key': '时间序列', 'currentPage': 0, 'pageSize': 40}
    data.update(updates)
    return {'postData': json.dumps({'data': {'mainSearchPcConditionForm': data}})}


def payload(jobs=None, page=0):
    return {'flag': 1, 'data': {'data': {'jobCardList': jobs if jobs is not None else [
        {'job': {'jobId': 'different-internal-id', 'title': '时间序列算法工程师', 'link': JOB},
         'recruiter': {'name': 'never-save-this-fixture'}}]},
        'pagination': {'currentPage': page, 'pageSize': 40, 'hasNext': False, 'totalCounts': 800}}}


def snapshot(data=None, context=None, html='<h1>无链接人工搜索页</h1>'):
    context = request_context(ADAPTER, 'liepin_search', request(), SEARCH) if context is None else context
    return PageSnapshot(SEARCH, html, (BusinessObservation(1, 'liepin_search', 1, payload() if data is None else data, context),))


class SearchObservationTests(unittest.TestCase):
    def test_default_entry_response_and_dom_never_become_query_cards_or_empty_results(self):
        context = request_context(ADAPTER, 'liepin_search', request(key=''), ADAPTER.search_base)
        for data in (payload(), payload([])):
            with self.subTest(empty=not data['data']['data']['jobCardList']):
                page = PageSnapshot(ADAPTER.search_base, '<a href="'+JOB+'">默认推荐</a>',
                    (BusinessObservation(1, 'liepin_search', 1, data, context),))
                self.assertTrue(ADAPTER.native_ready(page.business))
                with self.assertRaisesRegex(CrawlError, 'not_job_list'):
                    ADAPTER.cards(page)
                self.assertFalse(ADAPTER.confirmed_empty(page))

    def test_entry_response_cannot_replace_a_later_matching_keyword_response(self):
        context = request_context(ADAPTER, 'liepin_search', request(key=''), ADAPTER.search_base)
        entry = BusinessObservation(1, 'liepin_search', 9, payload(), {**context, 'sequence':9})
        page = snapshot(payload([]))
        page = replace(page, business=(*page.business, entry))
        self.assertEqual(ADAPTER.cards(page), [])
        self.assertTrue(ADAPTER.confirmed_empty(page))

    def test_reads_api_when_dom_has_no_links(self):
        cards = ADAPTER.cards(snapshot())
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].url, JOB)
        self.assertEqual(cards[0].id, hashlib.sha256(JOB.encode()).hexdigest()[:24])
        self.assertNotIn('never-save', repr(cards))

    def test_does_not_construct_url_from_internal_jobid(self):
        cards = ADAPTER.cards(snapshot())
        self.assertEqual(cards[0].url, JOB)
        self.assertNotIn('different', cards[0].url)

    def test_dom_fallback_unchanged(self):
        page = PageSnapshot(SEARCH, '<a href="'+JOB+'">人工</a>')
        self.assertEqual(ADAPTER.cards(page), DOMAdapter.cards(ADAPTER, page))

    def test_live_native_page_never_uses_stale_dom_while_current_response_is_missing(self):
        previous = snapshot(context={'query':'previous-query', 'page':0}).business
        for observations in ((), previous):
            page = PageSnapshot(SEARCH, '<a href="'+JOB+'">上一查询的岗位</a>',
                                business=observations, business_required=True)
            with self.subTest(observations=bool(observations)):
                with self.assertRaisesRegex(CrawlError, 'page_not_ready'):
                    ADAPTER.cards(page)
                with self.assertRaisesRegex(CrawlError, 'page_not_ready'):
                    ADAPTER.confirmed_empty(page)

    def test_live_native_matching_response_replaces_stale_dom_including_confirmed_empty(self):
        for data in (payload(), payload([])):
            page = replace(snapshot(data, html='<a href="https://www.liepin.com/job/999.shtml">旧岗位</a>'),
                           business_required=True)
            cards = ADAPTER.cards(page)
            self.assertEqual([card.url for card in cards], [JOB] if data['data']['data']['jobCardList'] else [])
            self.assertEqual(ADAPTER.confirmed_empty(page), not cards)

    def test_explicit_empty_not_login(self):
        page = snapshot(payload([]))
        self.assertEqual(ADAPTER.cards(page), [])
        self.assertTrue(ADAPTER.confirmed_empty(page))

    def test_missing_data_not_treated_as_empty(self):
        for data in ({}, {'flag': 1}, {'flag': 0, 'data': {}}, {'flag': True, 'data': {}}):
            with self.subTest(data=data), self.assertRaises(CrawlError) as caught:
                ADAPTER.cards(snapshot(data))
            self.assertEqual(caught.exception.code, 'native_business_response_invalid')

    def test_other_query_response_not_used(self):
        page = snapshot(context={'query':'other', 'page':0})
        self.assertEqual(ADAPTER.cards(page), [])
        self.assertFalse(ADAPTER.confirmed_empty(page))

    def test_response_page_mismatch_rejected(self):
        with self.assertRaises(CrawlError):
            ADAPTER.cards(snapshot(payload(page=1)))

    def test_unknown_operation_does_not_supply_cards(self):
        page = snapshot(); page = replace(page, business=(replace(page.business[0], operation='user_profile'),))
        self.assertEqual(ADAPTER.cards(page), [])

    def test_external_links_rejected(self):
        for link in ['https://outside.test/job/123.shtml', 'https://www.liepin.com/profile/', JOB+'?token=SECRET']:
            data = payload(); data['data']['data']['jobCardList'][0]['job']['link'] = link
            with self.subTest(link=link), self.assertRaises(CrawlError): ADAPTER.cards(snapshot(data))

    def test_invalid_item_not_silently_dropped(self):
        data = payload(); data['data']['data']['jobCardList'].append({'job':{}})
        with self.assertRaises(CrawlError): ADAPTER.cards(snapshot(data))

    def test_repeated_delivery_does_not_duplicate_card(self):
        data = payload(); data['data']['data']['jobCardList'] *= 2
        self.assertEqual(len(ADAPTER.cards(snapshot(data))), 1)

    def test_late_older_response_never_replaces_new_query(self):
        page = snapshot(); old = replace(page.business[0], context={**page.business[0].context, 'sequence':1})
        new = replace(old, context={**old.context, 'sequence':2}, payload=payload([]))
        self.assertEqual(ADAPTER.cards(replace(page, business=(new, old))), [])

    def test_snapshot_and_observation_repr_exclude_payload(self):
        self.assertNotIn('never-save', repr(snapshot()))
        self.assertNotIn('never-save', repr(snapshot().business))

    def test_challenge_does_not_use_previously_returned_results(self):
        with self.assertRaises(CrawlError) as error:
            ADAPTER.cards(snapshot(html='<h1>请完成安全验证</h1>'))
        self.assertEqual(error.exception.code, 'manual_required')

    def test_unselected_background_page_is_rejected(self):
        with self.assertRaises(CrawlError):
            request_context(ADAPTER, 'liepin_search', request(currentPage=1), SEARCH)

    def test_incomplete_page_not_ready_without_observation(self):
        self.assertFalse(ADAPTER.native_ready(()))
        self.assertTrue(ADAPTER.native_ready(snapshot().business))


class SearchBindingTests(unittest.TestCase):
    def test_exact_query_free_entry_is_tagged_without_becoming_a_user_query(self):
        context = request_context(ADAPTER, 'liepin_search', request(key=''), ADAPTER.search_base)
        self.assertEqual(context, {'query': hashlib.sha256(ADAPTER.search_base.encode()).hexdigest(),
                                  'page':0, 'size':40, 'entry_bootstrap':True})
        self.assertNotIn('entry_bootstrap', request_context(ADAPTER, 'liepin_search', request(), SEARCH))

    def test_entry_exception_cannot_hide_keyword_filter_or_pagination_mismatches(self):
        attempts = [(request(), ADAPTER.search_base), (request(key=''), SEARCH),
                    (request(key=''), ADAPTER.search_base+'?key='),
                    (request(key=''), ADAPTER.search_base+'?city=010'),
                    (request(key=''), ADAPTER.search_base+'?init=1'),
                    (request(key=''), ADAPTER.search_base+'?utm_source=entry')]
        attempts += [(request(key='', **fields), ADAPTER.search_base) for fields in (
            {'currentPage':1}, {'currentPage':True}, {'pageSize':20}, {'pageSize':True})]
        attempts.append((request(key=' '), ADAPTER.search_base))
        for req, url in attempts:
            with self.subTest(request=req, url=url), self.assertRaisesRegex(CrawlError, 'liepin_search_query_mismatch'):
                request_context(ADAPTER, 'liepin_search', req, url)

    def test_expected_query_binds_without_retaining_raw_data(self):
        context = request_context(ADAPTER, 'liepin_search', request(), SEARCH)
        self.assertNotIn('时间序列', json.dumps(context, ensure_ascii=False))
        self.assertEqual(context['page'], 0)

    def test_other_operations_have_no_binding(self):
        self.assertEqual(request_context(ADAPTER, 'other', {}, 'bad'), {})

    def test_wrong_keyword_rejected(self):
        with self.assertRaises(CrawlError): request_context(ADAPTER, 'liepin_search', request(key='other'), SEARCH)

    def test_page_must_be_search_not_detail_or_home(self):
        for url in [JOB, 'https://www.liepin.com/', 'https://evil.test/zhaopin/?key=时间序列']:
            with self.subTest(url=url), self.assertRaises(CrawlError):
                request_context(ADAPTER, 'liepin_search', request(), url)

    def test_all_url_filters_must_match(self):
        with self.assertRaises(CrawlError): request_context(ADAPTER, 'liepin_search', request(city='020'), SEARCH+'&city=010')
        self.assertTrue(request_context(ADAPTER, 'liepin_search', request(city='010'), SEARCH+'&city=010'))

    def test_unknown_and_duplicate_query_filters_rejected(self):
        for suffix in ['&someFilter=x', '&key=x']:
            with self.subTest(suffix=suffix), self.assertRaises(CrawlError): request_context(ADAPTER, 'liepin_search', request(), SEARCH+suffix)

    def test_invalid_request_json_and_duplicate_fields(self):
        for raw in ['null', '[]', '{', '{"data":{},"data":{}}', 'x'*100001]:
            with self.subTest(raw=raw[:30]), self.assertRaises(CrawlError): request_context(ADAPTER, 'liepin_search', {'postData':raw}, SEARCH)

    def test_invalid_pagination_rejected(self):
        for fields in [{'currentPage':-1}, {'currentPage':True}, {'pageSize':1000}, {'pageSize':0}]:
            with self.subTest(fields=fields), self.assertRaises(CrawlError): request_context(ADAPTER, 'liepin_search', request(**fields), SEARCH)


class NativeSearchContractTests(unittest.TestCase):
    def setUp(self): self.contract = contract_for(ADAPTER)

    def test_precise_search_post_not_arbitrary_api(self):
        rule = self.contract.match(API, 'POST', 'Fetch')
        self.assertEqual(rule.key, 'liepin_search')
        rule.validate_headers('POST', {'Origin':'https://www.liepin.com'})
        for method, url in [('GET',API), ('POST',API+'/extra'), ('POST','https://api-c.liepin.com/api/login')]:
            with self.subTest(method=method,url=url), self.assertRaises(CrawlError): self.contract.match(url,method,'Fetch')

    def test_source_origin_checked(self):
        rule = self.contract.match(API,'POST','Fetch')
        for origin in ['', 'https://evil.test', 'https://www.liepin.com.evil.test']:
            with self.subTest(origin=origin), self.assertRaises(CrawlError): rule.validate_headers('POST',{'Origin':origin})

    def test_region_catalogue_is_exact_origin_bound_get_with_its_own_preflight_method(self):
        url = 'https://api-dok.liepin.com/api/com.liepin.bd.p.v4.get-all-dq'
        rule = self.contract.match(url, 'GET', 'XHR')
        self.assertEqual(rule.key, 'liepin_regions')
        rule.validate_headers('GET', {'Origin':'https://www.liepin.com'})
        preflight = self.contract.match(url, 'OPTIONS', 'XHR')
        headers = {'Origin':'https://www.liepin.com', 'Access-Control-Request-Method':'GET',
                   'Access-Control-Request-Headers':'x-client-type,x-requested-with,x-fscp-std-info'}
        preflight.validate_headers('OPTIONS', headers)
        for change in ({'Access-Control-Request-Method':'POST'}, {'Origin':'https://other.test'},
                       {'Access-Control-Request-Headers':'authorization'}):
            with self.subTest(change=change), self.assertRaises(CrawlError):
                preflight.validate_headers('OPTIONS', {**headers, **change})
        for method, path, kind in [('POST', url, 'XHR'), ('GET', url, 'Document'),
                                  ('GET', url+'/extra', 'XHR'),
                                  ('GET', url.replace('v4.get-all-dq','suggest-dq'), 'XHR'),
                                  ('POST', url.replace('p.v4.get-all-dq','v3.batch-lookup-dq'), 'XHR')]:
            with self.subTest(method=method, path=path, kind=kind), self.assertRaises(CrawlError):
                self.contract.match(path, method, kind)
        with self.assertRaises(CrawlError):
            self.contract.match(API,'OPTIONS','XHR').validate_headers('OPTIONS', headers)

    def test_exact_cors_preflight(self):
        rule = self.contract.match(API,'OPTIONS','Preflight')
        headers={'Origin':'https://www.liepin.com', 'Access-Control-Request-Method':'POST', 'Access-Control-Request-Headers':'content-type,x-client-type'}
        rule.validate_headers('OPTIONS',headers)
        rule.validate_headers('OPTIONS',{**headers,'Access-Control-Request-Headers':
            'content-type,x-client-type,x-fscp-bi-stat,x-fscp-fe-version,x-fscp-std-info,x-fscp-trace-id,x-fscp-version,x-requested-with,x-xsrf-token'})
        for extra in [{'Access-Control-Request-Method':'DELETE'}, {'Access-Control-Request-Headers':'authorization'}, {'Origin':'https://evil.test'}]:
            with self.subTest(extra=extra),self.assertRaises(CrawlError):rule.validate_headers('OPTIONS',{**headers,**extra})

    def test_search_suggestions_only_allow_the_published_read_and_get_preflight(self):
        url = 'https://api-c.liepin.com/api/com.liepin.searchfront4c.pc-search-suggest-list'
        self.assertEqual(self.contract.match(url,'GET','XHR').key, 'liepin_search_suggest')
        headers = {'Origin':'https://www.liepin.com', 'Access-Control-Request-Method':'GET',
                   'Access-Control-Request-Headers':'x-client-type'}
        rule = self.contract.match(url,'OPTIONS','XHR')
        rule.validate_headers('OPTIONS',headers)
        with self.assertRaises(CrawlError):
            rule.validate_headers('OPTIONS',{**headers,'Access-Control-Request-Method':'POST'})
        for target, method, kind in ((url,'POST','XHR'),(url,'GET','Document'),(url+'/extra','GET','XHR'),
                                     (url.replace('pc-search-suggest-list','pc-hot-search-word-list'),'GET','XHR')):
            with self.subTest(target=target,method=method),self.assertRaises(CrawlError):
                self.contract.match(target,method,kind)

    def test_observed_asset_host_does_not_allow_posts_or_documents(self):
        url='https://concat.lietou-static.com/fe-www-pc/v6/css/common.hash.css'
        self.assertEqual(self.contract.match(url,'GET','Stylesheet').role,'asset')
        for method,resource in [('POST','Fetch'),('GET','Document')]:
            with self.subTest(method=method),self.assertRaises(CrawlError):self.contract.match(url,method,resource)

    def test_published_search_filter_initialization_is_exact_and_origin_bound(self):
        url=API+'-cond-init'
        rule=self.contract.match(url,'POST','XHR')
        self.assertEqual(rule.key,'liepin_search_filters')
        rule.validate_headers('POST',{'Origin':'https://www.liepin.com'})
        with self.assertRaises(CrawlError):rule.validate_headers('POST',{'Origin':'https://other.test'})
        self.assertEqual(self.contract.match(url,'OPTIONS','XHR').key,'liepin_filters_preflight')
        for changed in [API+'-cond-update',url+'/extra']:
            with self.assertRaises(CrawlError):self.contract.match(changed,'POST','XHR')

    def test_shared_ui_manifest_is_only_a_static_read_not_chat_permission(self):
        self.assertEqual(self.contract.match('https://feim.liepin.com/lp-manifest.json','GET','XHR').role,'asset')
        self.assertEqual(self.contract.match('https://feim.liepin.com/lp-manifest.js','GET','Script').role,'asset')
        self.assertEqual(self.contract.match('https://concat.lietou-static.com/fe-im-pc/v6/remote.js','GET','Script').role,'asset')
        for url,method,resource in [('https://feim.liepin.com/lp-manifest.json','POST','XHR'),
                                    ('https://feim.liepin.com/lp-manifest.json','GET','Document'),
                                    ('https://feim.liepin.com/api/messages','GET','XHR'),
                                    ('https://concat.lietou-static.com/fe-im-pc/v6/remote.js','POST','Fetch')]:
            with self.subTest(url=url,method=method),self.assertRaises(CrawlError):self.contract.match(url,method,resource)

    def test_login_still_not_assumed_from_cookies(self):
        for auth in (False, True):
            with self.assertRaises(CrawlError): self.contract.match('https://passport.liepin.com/login','POST','Document',authentication=auth)


class ObservedBackend:
    data = None
    def __init__(self,*args):self.page=None
    def open(self,url,authentication=False):
        self.page = snapshot(self.data) if '/zhaopin/' in url else PageSnapshot(JOB,markup(posting(url=JOB)))
        return self.page
    def snapshot(self):return self.page
    def next_page(self):return False
    def pump(self):pass
    def close(self):pass


class ObservedServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.workspace=Workspace(Path(self.tmp.name))
        self.service=GuidedService(self.workspace,registry=Registry([ADAPTER]),backend_factory=ObservedBackend)
        self.addCleanup(self.service.close)
        self.service._submit=Mock()
        task=self.service.create({'platform':'liepin','keyword':'时间序列','roles':['time_series'], 'max_pages':1,'max_jobs':1,'consent':True,'rights_note':'independent artificial fixture'})
        self.state=self.service._load(task['id'])

    def test_response_only_list_to_original_report_without_login(self):
        with patch.object(ObservedBackend,'data',payload()):
            self.service._run('search',self.state,None)
            self.assertEqual(self.state['status'],'ready')
            self.assertEqual(len(self.state['cards']),1)
            self.state['selection']=[self.state['cards'][0]['id']];self.state['phase']='collect'
            self.service._run('collect',self.state,None)
        self.assertEqual(self.state['outcome']['saved'],1)
        self.assertTrue(self.state['report_id'])
        with Store(self.workspace.db) as store:self.assertEqual(store.records()[0].text,BODY)

    def test_explicit_zero_results_not_waiting_for_manual_login(self):
        with patch.object(ObservedBackend,'data',payload([])):
            self.service._run('search',self.state,None)
        self.assertEqual(self.state['code'],'no_matching_jobs')
        self.assertEqual(self.state['status'],'ready')
        self.assertEqual(self.state['list_end'],'confirmed_empty')

    def test_invalid_response_not_empty_or_old_report(self):
        with patch.object(ObservedBackend,'data',{'flag':0}):
            with self.assertRaises(CrawlError):self.service._run('search',self.state,None)
        self.assertFalse(self.state.get('report_id'))
