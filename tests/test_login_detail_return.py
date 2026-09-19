"""Selected-detail login continuation. All HTML and accounts are synthetic."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from vibe_job_radar.guided.adapters import DOMAdapter, Registry, builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.login_return import (
    DetailTarget, LoginReturnManager, ReturnedDetail,
    matching_detail_signature, pending_detail_target,
)
from vibe_job_radar.guided.rate import Limits, RateLedger
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import InputError, Workspace

ADAPTER = builtins().get('liepin')
SEARCH = ADAPTER.search_url('时间序列算法工程师')
URL = 'https://www.liepin.com/job/2.shtml'
BODY = '岗位职责：负责时间序列预测与评估。任职要求：熟悉Python、统计学，使用Cursor编写测试并审查代码。仅为人工测试正文。'

def markup(body=BODY, title='时间序列算法工程师'):
    return f'<h1>{title}</h1><div class="job-description">{body}</div>'

def task_state():
    return {'id':'task', 'platform':'liepin', 'search_url':SEARCH, 'status':'waiting_manual',
            'authentication':'manual_pending', 'phase':'collect', 'auto_continue_after_login':True,
            'selection':['one','two'], 'cards':[
                {'id':'one','url':URL.replace('/2.', '/1.'),'status':'ok'},
                {'id':'two','url':URL,'status':'manual_required'},
            ]}

class DetailSignatureTests(unittest.TestCase):
    def test_full_selected_detail_is_ready(self):
        self.assertTrue(matching_detail_signature(ADAPTER, URL, PageSnapshot(URL, markup())))

    def test_same_entity_tracking_changes_are_allowed_by_adapter(self):
        self.assertEqual(
            matching_detail_signature(ADAPTER, URL+'?d_sfrom=a', PageSnapshot(URL+'?d_sfrom=b', markup())),
            matching_detail_signature(ADAPTER, URL, PageSnapshot(URL, markup())))

    def test_other_job_and_identical_title_are_not_the_selected_job(self):
        self.assertIsNone(matching_detail_signature(ADAPTER, URL, PageSnapshot(URL.replace('/2.', '/3.'), markup())))

    def test_conflicting_canonical_cannot_be_consumed(self):
        with self.assertRaises(CrawlError) as caught:
            matching_detail_signature(ADAPTER, URL, PageSnapshot(URL, '<link rel="canonical" href="/job/3.shtml">'+markup()))
        self.assertEqual(caught.exception.code, 'job_identity_mismatch')

    def test_summary_and_challenge_remain_pending(self):
        for body in (BODY+'展开全部', BODY+'登录后查看完整职位', '请完成安全验证'):
            with self.subTest(body=body), self.assertRaises(CrawlError):
                matching_detail_signature(ADAPTER, URL, PageSnapshot(URL, markup(body)))

    def test_non_job_pages_are_rejected(self):
        for url in ('https://www.liepin.com/', SEARCH, 'https://elsewhere.invalid/job/2.shtml'):
            with self.subTest(url=url), self.assertRaises(CrawlError):
                matching_detail_signature(ADAPTER, URL, PageSnapshot(url, markup()))

    def test_changed_body_changes_signature(self):
        a=matching_detail_signature(ADAPTER, URL, PageSnapshot(URL, markup()))
        b=matching_detail_signature(ADAPTER, URL, PageSnapshot(URL, markup(BODY+'另一职责。')))
        self.assertNotEqual(a,b)

    def test_surrounding_advertisement_does_not_destabilize_full_jd(self):
        a=matching_detail_signature(ADAPTER, URL, PageSnapshot(URL, markup()))
        b=matching_detail_signature(ADAPTER, URL, PageSnapshot(URL, markup()+'<aside>动态推荐 123</aside>'))
        self.assertEqual(a,b)

    def test_zero_ai_requirements_are_valid_body(self):
        self.assertTrue(matching_detail_signature(ADAPTER, URL, PageSnapshot(URL, markup(
            '岗位职责：负责时间序列模型开发与评估。任职要求：熟悉统计学和机器学习，有预测模型落地经验。仅为人工测试。'))))

    def test_unvalidated_adapter_cannot_invent_url_alias(self):
        adapter=DOMAdapter('fixture','fixture',('liepin.com',),SEARCH,'key',r'^/job/\d+\.shtml$',
                           'https://www.liepin.com/',('www.liepin.com',))
        self.assertIsNone(matching_detail_signature(adapter, URL, PageSnapshot(URL+'?other=value', markup())))

    def test_adapter_snippet_is_not_complete_detail(self):
        adapter=Mock(key='liepin')
        adapter.accept_url.side_effect=lambda url,**_:url
        adapter.job_identity=None; adapter.validate_detail_identity=None
        adapter.detail.return_value={'title':'title','text':BODY,'evidence_level':'snippet'}
        with self.assertRaises(CrawlError) as caught:
            matching_detail_signature(adapter, URL, PageSnapshot(URL, markup()))
        self.assertEqual(caught.exception.code,'invalid_job_data')

    def test_pending_target_skips_only_successful_selected_jobs(self):
        state=task_state()
        state['cards'].insert(0, {'id':'ad','url':URL.replace('/2.', '/99.'),'status':'discovered'})
        self.assertEqual(pending_detail_target(state), DetailTarget('two',URL))

    def test_no_selection_or_wrong_phase_cannot_capture_current_detail(self):
        for changes in ({'selection':[]}, {'phase':'search'}, {'cards':[]}):
            state=task_state(); state.update(changes)
            self.assertIsNone(pending_detail_target(state))

    def test_access_refusal_cannot_be_skipped_for_a_later_readable_job(self):
        state=task_state(); state['cards'][0]['status']='http_403'
        self.assertIsNone(pending_detail_target(state))

class DetailWatcherTests(unittest.TestCase):
    def setUp(self):
        self.now=0.0
        self.state=task_state()
        self.backend=Mock()
        self.backend.snapshot.return_value=PageSnapshot(URL,markup())
        self.manager=LoginReturnManager(clock=lambda:self.now,timeout=10)
        self.service=SimpleNamespace(_lock=threading.RLock(),_busy=False,_shutdown=threading.Event(),
            _cancel=threading.Event(),_backends={'task':self.backend},registry=Registry([ADAPTER]),
            _load=lambda _:self.state,_save=lambda state,**kw:state.update(kw),_submit=Mock())
        self.manager.arm(self.state,self.backend)
    def tick(self):
        self.manager.tick(self.service); self.now+=1
    def test_two_reads_enqueue_one_bounded_metadata_handoff(self):
        self.tick(); self.service._submit.assert_not_called()
        self.tick(); self.tick()
        self.service._submit.assert_called_once()
        action,ident,handoff=self.service._submit.call_args.args
        self.assertEqual((action,ident),('resume_returned_detail','task'))
        self.assertEqual(handoff.target,DetailTarget('two',URL))
        self.assertIs(handoff.backend,self.backend)
        self.assertFalse(hasattr(handoff,'html'));self.assertFalse(hasattr(handoff,'parsed'))
        self.assertNotIn(URL,repr(handoff))
        self.backend.open.assert_not_called()
        self.assertEqual(self.state['authentication'],'manual_pending')
    def test_detail_origin_handoff_can_resume_without_list_page(self):
        self.state['search_url'] = URL
        self.manager.arm(self.state, self.backend)
        self.tick(); self.tick()
        self.assertEqual(self.service._submit.call_args.args[0], 'resume_returned_detail')
        self.backend.open.assert_not_called()

    def test_api_only_original_list_keeps_search_observations(self):
        from test_liepin_native_search import snapshot
        page = snapshot()
        self.state['search_url'] = page.url
        self.state['phase'] = 'select'
        self.backend.snapshot.return_value = page
        self.manager.arm(self.state, self.backend)
        self.tick(); self.tick()
        self.service._submit.assert_called_once_with('capture', 'task')
        self.backend.open.assert_not_called()

    def test_unselected_or_later_detail_is_never_auto_consumed(self):
        self.backend.snapshot.return_value=PageSnapshot(URL.replace('/2.', '/3.'),markup())
        self.tick();self.tick();self.service._submit.assert_not_called()
    def test_incomplete_then_full_needs_two_fresh_observations(self):
        self.tick()
        self.backend.snapshot.return_value=PageSnapshot(URL,markup(BODY+'展开更多'))
        self.tick()
        self.backend.snapshot.return_value=PageSnapshot(URL,markup())
        self.tick();self.service._submit.assert_not_called()
        self.tick();self.service._submit.assert_called_once()
    def test_changed_body_does_not_reuse_previous_stability(self):
        self.tick()
        self.backend.snapshot.return_value=PageSnapshot(URL,markup(BODY+'另一职责。'))
        self.tick();self.service._submit.assert_not_called()
        self.tick();self.service._submit.assert_called_once()
    def test_original_list_resume_still_supported(self):
        self.backend.snapshot.return_value=PageSnapshot(SEARCH, '<a href="/job/2.shtml">原岗位</a>')
        self.tick();self.tick();self.service._submit.assert_called_once_with('resume','task')
    def test_pause_during_second_read_wins(self):
        self.tick()
        def snapshot():
            self.service._cancel.set()
            return PageSnapshot(URL,markup())
        self.backend.snapshot.side_effect=snapshot
        self.tick();self.service._submit.assert_not_called()
    def test_detail_without_pending_selection_is_not_a_list(self):
        self.state['selection']=[]
        self.manager.arm(self.state,self.backend)
        self.tick();self.tick();self.service._submit.assert_not_called()
    def test_status_change_rejects_frozen_target(self):
        self.tick();self.state['cards'][1]['status']='ok'
        self.tick();self.service._submit.assert_not_called()
        self.assertEqual(self.state['login_continuation'],'needs_attention')

class DetailReturnServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.workspace=Workspace(Path(self.tmp.name))
        class Backend:
            """Synthetic returned DOM; never creates or fetches an external page."""
            def __init__(self,adapter,ledger,cancelled,progress):
                self.adapter=adapter;self.page=None;self.calls=[];self.auth_mode=False
                self.unlocked=False;self.observations=0;self.on_read=None
                self.wire=SimpleNamespace(ensure_robots=Mock())
            def open(self,url,authentication=False):
                self.calls.append((url,authentication));self.auth_mode=authentication
                if '/zhaopin/' in url:
                    self.page=PageSnapshot(url,'<a href="/job/1.shtml">时间序列算法一</a><a href="/job/2.shtml">时间序列算法二</a>')
                elif url==URL and not self.unlocked:
                    self.page=PageSnapshot(url,markup('登录后查看职位'))
                    raise CrawlError('manual_required')
                elif '/job/' in url:
                    self.page=PageSnapshot(url,markup())
                else:
                    self.page=PageSnapshot(url,'<h1>人工登录页面</h1>')
                return self.page
            def snapshot(self):
                if self.on_read: self.on_read(self)
                return self.page
            def collection_mode(self): self.auth_mode=False
            def next_page(self):return False
            def pump(self):pass
            def close(self):pass
        self.service=GuidedService(self.workspace,registry=Registry([ADAPTER]),backend_factory=Backend,
            ledger=RateLedger(self.workspace.root/'guided'/'rates.sqlite',Limits(login_interval=0)))
        self.ident=self.service.create({'platform':'liepin','keyword':'时间序列算法工程师','roles':['time_series'],
            'consent':True,'rights_note':'合成页面测试，非猎聘实站验收','max_pages':1,'max_jobs':2})['id']
        self.wait_idle()
        state=self.service._load(self.ident)
        self.service.action({'id':self.ident,'action':'collect','selected':[r['id'] for r in state['cards']]})
        self.wait_idle()
        state=self.service._load(self.ident)
        self.assertEqual([r['status'] for r in state['cards']], ['ok','manual_required'])
        self.partial_report=state['report_id'];self.first_record=state['cards'][0]['record_id']
        self.backend=self.service._backends[self.ident]
        self.service._login_return.interval=.01
    def tearDown(self):
        self.service.close();self.tmp.cleanup()
    def wait_idle(self):
        end=time.monotonic()+5
        while self.service.state()['busy'] and time.monotonic()<end:time.sleep(.01)
        self.assertFalse(self.service.state()['busy'])
    def await_state(self,key,value):
        end=time.monotonic()+5
        while time.monotonic()<end:
            # One locked view: reading JSON before a separate busy check can
            # return the prior completed/manual_pending snapshot while the
            # worker finishes its final user_resumed save in between.
            view=self.service.state()
            state=next(j for j in view['jobs'] if j['id']==self.ident)
            if state.get(key)==value and not view['busy']:return state
            time.sleep(.01)
        self.fail(f'expected {key}={value}; got {self.service._load(self.ident)}')
    def login(self):
        self.service.action({'id':self.ident,'action':'login','auto_continue':True})
        self.wait_idle()
        self.assertEqual(self.backend.calls[-1],(URL,True))
        self.backend.unlocked=True;self.backend.page=PageSnapshot(URL,markup())
    def test_login_returned_full_jd_enters_report_without_second_fetch(self):
        self.login(); calls=list(self.backend.calls)
        state=self.await_state('status','completed')
        self.assertEqual(self.backend.calls,calls)
        self.assertEqual([r['status'] for r in state['cards']], ['ok','ok'])
        self.assertEqual(state['cards'][0]['record_id'],self.first_record)
        self.assertNotEqual(state['report_id'],self.partial_report)
        self.assertTrue(self.workspace.report_file(self.partial_report,'run_manifest.json').is_file())
        self.assertEqual(state['authentication'],'user_resumed')
        self.assertEqual(state['login_continuation'],'resumed_detail')
        self.assertEqual(state['cards'][1]['acquisition_path'],'login_returned_detail')
        self.backend.wire.ensure_robots.assert_any_call(URL)
        self.assertFalse(self.backend.auth_mode)
        with Store(self.workspace.db) as store:
            records=store.records()
        self.assertEqual(len(records),2)
        self.assertEqual(next(r for r in records if r.url==URL).text,BODY)
        audit=json.loads(self.workspace.report_file(state['report_id'],'guided_acquisition.json').read_text(encoding='utf-8'))
        self.assertEqual(audit['items'][1]['acquisition_path'],'login_returned_detail')
    def test_new_return_path_is_not_exposed_as_http_action(self):
        with self.assertRaises(InputError):
            self.service.action({'id':self.ident,'action':'resume_returned_detail'})
    def test_default_manual_login_keeps_platform_entry(self):
        self.service.action({'id':self.ident,'action':'login'});self.wait_idle()
        self.assertEqual(self.backend.calls[-1],(ADAPTER.login_url,True))
        self.assertFalse(self.service._login_return._watches)
    def third_read(self,fn):
        def on_read(backend):
            if not backend.unlocked:return
            backend.observations+=1
            if backend.observations==3:fn(backend)
        self.backend.on_read=on_read
    def test_navigation_changed_before_execution_never_fetches_or_saves(self):
        self.third_read(lambda b:setattr(b,'page',PageSnapshot(URL.replace('/2.', '/9.'),markup())))
        self.login(); calls=list(self.backend.calls)
        state=self.await_state('code','login_return_changed')
        self.assertEqual(self.backend.calls,calls)
        self.assertEqual(state['cards'][1]['status'],'manual_required')
        self.assertEqual(state['report_id'],self.partial_report)
        self.assertEqual(state['login_continuation'],'needs_attention')
    def test_body_changed_before_execution_never_fetches_or_saves(self):
        self.third_read(lambda b:setattr(b,'page',PageSnapshot(URL,markup(BODY+'Changed'))))
        self.login();calls=list(self.backend.calls)
        state=self.await_state('code','login_return_changed')
        self.assertEqual(self.backend.calls,calls)
        self.assertEqual(state['report_id'],self.partial_report)
    def test_cancel_during_revalidation_prevents_persistence(self):
        self.third_read(lambda _:self.service._cancel.set())
        self.login();calls=list(self.backend.calls)
        state=self.await_state('status','paused')
        self.assertEqual(self.backend.calls,calls)
        self.assertNotEqual(state['cards'][1]['status'],'ok')
        with Store(self.workspace.db) as store:self.assertEqual(len(store.records()),1)
    def test_robots_denial_on_returned_page_is_not_skipped(self):
        self.backend.wire.ensure_robots.side_effect=CrawlError('robots_denied')
        self.login();calls=list(self.backend.calls)
        state=self.await_state('code','robots_denied')
        self.assertEqual(self.backend.calls,calls)
        self.assertEqual(state['report_id'],self.partial_report)
    def test_hard_error_during_revalidation_is_not_cleared(self):
        def fail(_):raise CrawlError('http_403')
        self.third_read(fail)
        self.login();calls=list(self.backend.calls)
        state=self.await_state('code','http_403')
        self.assertEqual(self.backend.calls,calls)
        self.assertEqual(state['report_id'],self.partial_report)
    def test_replaced_browser_cannot_consume_old_handoff(self):
        state=self.service._load(self.ident); state['authentication']='manual_pending'
        handoff=ReturnedDetail(pending_detail_target(state),'signature',Mock())
        with self.assertRaises(CrawlError) as caught:
            self.service._run('resume_returned_detail',state,handoff)
        self.assertEqual(caught.exception.code,'login_return_changed')

    def test_dead_owner_does_not_create_or_restore_replacement_browser(self):
        state = self.service._load(self.ident)
        state['authentication'] = 'manual_pending'
        self.backend.alive = Mock(return_value=False)
        handoff = ReturnedDetail(pending_detail_target(state), 'signature', self.backend)
        self.service._backend = Mock(side_effect=AssertionError('must not create a browser'))
        with self.assertRaises(CrawlError) as caught:
            self.service._run('resume_returned_detail', state, handoff)
        self.assertEqual(caught.exception.code, 'login_return_changed')
        self.service._backend.assert_not_called()
        self.assertEqual(state['report_id'], self.partial_report)

if __name__=='__main__':unittest.main()
