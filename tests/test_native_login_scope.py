"""Login return must use the same bound native query scope as collection."""
from dataclasses import replace
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from vibe_job_radar.guided.adapters import Registry
from vibe_job_radar.guided.contracts import PageSnapshot
from vibe_job_radar.guided.login_return import LoginReturnManager
from vibe_job_radar.guided.native_browser import BusinessObservation
from vibe_job_radar.guided.search_scope import check_scope
from test_liepin_native_search import payload
from test_liepin_published_query import ADAPTER, bind, published_request


def native_page(*, metadata=None, **fields):
    query, body = published_request(**fields)
    if metadata:
        query.update(metadata)
        body['data']['passThroughForm'].update({k:v for k,v in metadata.items() if k != 'ckId'})
    context, url = bind(query, body)
    return PageSnapshot(url, '<h1>合成查询</h1>',
        (BusinessObservation(1, 'liepin_search', 1, payload(page=fields.get('currentPage',0)), context),),
        business_required=True)


class NativeLoginScopeTests(unittest.TestCase):
    def setUp(self):
        self.now = 0
        self.manager = LoginReturnManager(clock=lambda:self.now, timeout=60)
        self.page = native_page()
        self.backend = Mock()
        self.backend.snapshot.return_value = self.page
        self.state = dict(id='task', platform='liepin', search_url=ADAPTER.search_url('架构师 C++ / AI'),
            keyword='架构师 C++ / AI', query_scope_version=1, status='waiting_manual',
            authentication='manual_pending', phase='search', auto_continue_after_login=True)
        self.service = SimpleNamespace(_lock=threading.RLock(), _busy=False, _shutdown=threading.Event(),
            _cancel=threading.Event(), _backends={'task':self.backend}, registry=Registry([ADAPTER]),
            _load=lambda _:self.state, _save=lambda state,**kw:state.update(kw), _submit=Mock())
        self.manager.arm(self.state, self.backend)

    def tick(self):
        self.manager.tick(self.service)
        self.now += 1

    def test_expanded_publisher_defaults_return_after_two_reads_without_new_input(self):
        self.tick(); self.service._submit.assert_not_called()
        self.tick(); self.tick()
        self.service._submit.assert_called_once_with('capture','task')
        self.backend.open.assert_not_called()
        self.backend.password_login.assert_not_called()
        self.assertEqual(self.state['authentication'],'manual_pending')
        self.assertNotIn('effective_search', self.state)

    def test_first_observation_filter_change_requires_two_fresh_matching_reads(self):
        self.tick()
        self.backend.snapshot.return_value = native_page(city='020')
        self.tick(); self.service._submit.assert_not_called()
        self.tick(); self.service._submit.assert_called_once_with('capture','task')
        self.assertNotIn('effective_search', self.state)

    def test_already_frozen_conditions_never_change_during_login_return(self):
        check_scope(self.state, ADAPTER, self.page.url)
        frozen = dict(self.state['effective_search'])
        for fields in ({'city':'020'}, {'dq':'020'}, {'pubTime':'7'}, {'salaryCode':'10$30'}, {'suggestTag':'other'}):
            with self.subTest(fields=fields):
                self.backend.snapshot.return_value = native_page(**fields)
                self.manager.arm(self.state,self.backend)
                self.tick(); self.tick()
                self.service._submit.assert_not_called()
                self.assertEqual(self.state['effective_search'],frozen)

    def test_explicit_seed_filter_cannot_be_replaced_by_publisher_defaults(self):
        self.state['search_url'] += '&city=020'
        self.manager.arm(self.state,self.backend)
        self.tick(); self.tick()
        self.service._submit.assert_not_called()

    def test_publisher_interaction_rotation_keeps_stability_for_its_own_response(self):
        self.tick()
        self.backend.snapshot.return_value = native_page(metadata={'ckId':'c'*32,'scene':'fixture-return'})
        self.tick()
        self.service._submit.assert_called_once_with('capture','task')

    def test_other_keyword_page_or_duplicate_filter_does_not_resume(self):
        for page in (native_page(key='other'), native_page(currentPage=1),
                     replace(self.page,url=self.page.url+'&city=410')):
            with self.subTest():
                self.backend.snapshot.return_value=page
                self.manager.arm(self.state,self.backend)
                self.tick(); self.tick()
                self.service._submit.assert_not_called()

    def test_metadata_change_cannot_adopt_an_old_response(self):
        changed = native_page(metadata={'ckId':'c'*32})
        self.backend.snapshot.return_value=replace(self.page,url=changed.url)
        self.tick(); self.tick()
        self.service._submit.assert_not_called()
        self.assertEqual(self.state['login_continuation'],'needs_attention')

    def test_plain_dom_return_keeps_original_exact_query_gate(self):
        self.backend.snapshot.return_value=replace(self.page,business_required=False)
        self.tick(); self.tick()
        self.service._submit.assert_not_called()
