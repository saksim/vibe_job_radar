"""Synthetic publisher visibility fields; no live data or HTTP requests."""
import copy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.adapters import Registry
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.native_browser import BusinessObservation
from vibe_job_radar.guided.native_policy import contract_for
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace
from test_liepin_native_search import ADAPTER, JOB, SEARCH, ObservedBackend, payload, snapshot
from test_guided_batch_identity import Pages


def card(ident, title, *, hidden=False):
    row = {'jobId':str(ident), 'jobKind':'2', 'title':title,
           'link':f'https://www.liepin.com/job/{ident}.shtml'}
    if hidden:
        row.update(g='synthetic', j=hashlib.md5(f'synthetic{ident}2'.encode()).hexdigest())
    return {'job':row}


def search_page(rows, page=0):
    url = SEARCH if page == 0 else SEARCH+'&currentPage='+str(page)
    context = {'query':hashlib.sha256(url.encode()).hexdigest(), 'page':page, 'size':40}
    return PageSnapshot(url, '<h1>人工无链接列表</h1>',
        (BusinessObservation(1,'liepin_search',1,payload(rows,page),context),), business_required=True)


class PublisherVisibilityTests(unittest.TestCase):
    def test_native_list_excludes_publisher_hidden_rows_without_title_filtering(self):
        rows = [card(900,'独立人工隐藏条目',hidden=True),
                card(123,'时间序列算法工程师'),
                card(901,'另一人工隐藏条目',hidden=True)]
        cards = ADAPTER.cards(snapshot(payload(rows)))
        self.assertEqual([c.url for c in cards], [JOB])

    def test_visible_ads_and_unrelated_titles_are_not_silently_filtered(self):
        row = card(123,'与查询无关但网页显示的岗位')
        row['job']['advViewFlag'] = True
        self.assertEqual(ADAPTER.cards(snapshot(payload([row])))[0].title, row['job']['title'])

    def test_nonmatching_or_absent_marker_keeps_publisher_visible_rows(self):
        original = card(123,'可见人工岗位',hidden=True)
        changes = ({'j':'0'*32}, {'j':original['job']['j'].upper()}, {'j':'not-a-digest'},
                   {'j':None}, {'j':True}, {'g':''}, {'g':None}, {'g':False}, {'g':0})
        for change in changes:
            row = copy.deepcopy(original);row['job'].update(change)
            with self.subTest(change=change):
                self.assertEqual([c.url for c in ADAPTER.cards(snapshot(payload([row])))], [JOB])

    def test_publisher_hash_uses_utf16_low_bytes_and_safe_integer_concatenation(self):
        for seed, ident, kind in (('é',123,2), ('α😀','opaque-internal-id','2')):
            text = seed+str(ident)+str(kind)
            row = card(123,'隐藏Unicode人工岗位')
            row['job'].update(g=seed,jobId=ident,jobKind=kind,
                j=hashlib.md5(text.encode('utf-16-le')[::2]).hexdigest())
            with self.subTest(seed=seed):
                self.assertEqual(ADAPTER.cards(snapshot(payload([row]))), [])
                row['job']['j'] = hashlib.md5(text.encode('utf-8')).hexdigest()
                self.assertEqual([c.url for c in ADAPTER.cards(snapshot(payload([row])))], [JOB])

    def test_unknown_or_oversized_hash_inputs_fail_before_cards_are_returned(self):
        for change in ({'g':{}},{'g':'x'*4097},{'jobId':{}},{'jobId':True},
                       {'jobId':2**54},{'jobKind':2.5},{'jobKind':'x'*257}):
            row=card(123,'人工',hidden=True);row['job'].update(change)
            with self.subTest(change_type=next(iter(change))):
                with self.assertRaisesRegex(CrawlError,'^native_business_response_invalid$'):
                    ADAPTER.cards(snapshot(payload([row])))


class PublisherVisibilityServiceTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.workspace=Workspace(Path(temporary.name))
        self.service=GuidedService(self.workspace,registry=Registry([ADAPTER]),backend_factory=ObservedBackend)
        self.addCleanup(self.service.close);self.service._submit=Mock()

    def create(self, **changes):
        ident=self.service.create(dict(platform='liepin',keyword='时间序列',roles=['time_series'],
            consent=True,rights_note='SYNTHETIC PUBLISHER VISIBILITY',
            max_pages=changes.pop('max_pages',1),max_jobs=1,**changes))['id']
        return self.service._load(ident)

    def test_all_publisher_hidden_rows_are_empty_without_detail_or_login(self):
        state=self.create(auto_collect=True)
        with patch.object(ObservedBackend,'data',payload([card(900,'隐藏人工',hidden=True)])):
            self.service._run('search',state,None)
        self.assertEqual((state['status'],state['code']),('ready','no_matching_jobs'))
        self.assertEqual(state['cards'],[]);self.assertEqual(state['selection'],[])
        self.assertFalse(state['report_id']);self.assertFalse(self.workspace.db.exists())

    def test_automatic_batch_starts_from_visible_card_and_retains_original_report_path(self):
        state=self.create(auto_collect=True)
        rows=[card(900,'隐藏人工首条',hidden=True),card(123,'时间序列算法工程师')]
        opened=[]
        original=ObservedBackend.open
        def record(backend,url,authentication=False):
            opened.append(url);return original(backend,url,authentication)
        with patch.object(ObservedBackend,'data',payload(rows)), patch.object(ObservedBackend,'open',record):
            self.service._run('search',state,None)
        self.assertEqual([c['url'] for c in state['cards']],[JOB])
        self.assertEqual(opened,[SEARCH,JOB])
        self.assertEqual(state['outcome']['saved'],1);self.assertTrue(state['report_id'])
        saved=self.service._path(state['id']).read_text(encoding='utf8')
        self.assertNotIn('synthetic',saved);self.assertNotIn(rows[0]['job']['j'],saved)

    def test_changing_only_hidden_rows_does_not_create_a_second_distinct_page(self):
        state=self.create(max_pages=2)
        first=search_page([card(900,'隐藏第一',hidden=True),card(123,'人工可见')])
        second=search_page([card(901,'隐藏第二',hidden=True),card(123,'人工可见')],1)
        self.service._gather(state,Pages([first,second]),ADAPTER)
        self.assertEqual(state['list_end'],'repeated_page')
        self.assertEqual(len(state['pages_seen']),1)
        self.assertEqual([c['url'] for c in state['cards']],[JOB])

    def test_previous_native_reader_binding_cannot_resume_or_collect_saved_selection(self):
        state=self.create(backend='native',native_consent=True)
        old=copy.deepcopy(state)
        digest=hashlib.sha256(json.dumps(asdict(contract_for(ADAPTER)),sort_keys=True,
            ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
        old['execution_binding']['backend']='native:'+digest
        self.service._save(old)
        before=self.service._path(old['id']).read_bytes()
        backend=Mock(side_effect=AssertionError('No browser may be allocated'))
        with patch.object(self.service,'_backend',backend):
            for action in ('resume','collect'):
                with self.subTest(action=action), self.assertRaisesRegex(CrawlError,'^checkpoint_incompatible$'):
                    self.service._run(action,old,None)
        backend.assert_not_called()
        self.assertEqual(self.service._path(old['id']).read_bytes(),before)

    def test_unversioned_native_checkpoint_is_preserved_and_refused_before_browser(self):
        state=self.create(backend='native',native_consent=True)
        state.pop('execution_binding');self.service._save(state)
        before=self.service._path(state['id']).read_bytes()
        with patch.object(self.service,'_backend') as backend:
            with self.assertRaisesRegex(CrawlError,'^checkpoint_incompatible$'):
                self.service._run('resume',state,None)
        backend.assert_not_called()
        self.assertEqual(self.service._path(state['id']).read_bytes(),before)


if __name__ == '__main__':
    unittest.main()
