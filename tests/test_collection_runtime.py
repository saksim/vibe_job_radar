"""Real Collector/ledger/ownership; only upstream responses are controlled."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar.collection import Collector
from vibe_job_radar.collection_runtime import CollectionRunner, CollectionBusy
from vibe_job_radar.network import SafeHTTP
from vibe_job_radar.workspace import InputError
import test_collection_shared_rate as shared
from test_public_category import Wire, data, listing, card, job_url


class RuntimeTests(unittest.TestCase):
    finish = shared.CollectionSharedRateTests.finish

    def setUp(self):
        shared.CollectionSharedRateTests.setUp(self)
        self.runners=[];self.releases=[]

    def tearDown(self):
        for event in self.releases:event.set()
        for runner in self.runners:runner.close()

    def runner(self, collector=None):
        runner = CollectionRunner(collector or self.collector)
        self.runners.append(runner)
        self.addCleanup(runner.close)
        return runner

    def start(self, runner, state):
        return runner.start({'id':state['id'],'consent':True})

    def wait(self, runner):
        runner._thread.join(10)
        self.assertFalse(runner.is_running(), 'Bounded background batch did not finish')
        return runner.state()

    def wire(self, wire=None):
        wire = wire or Wire(listing(''.join(card(i) for i in range(1,8))))
        self.mock = patch.object(SafeHTTP, 'public_get', side_effect=wire.public_get)
        self.mock.start();self.addCleanup(self.mock.stop)
        return wire

    def blocked(self):
        wire = Wire(listing(''.join(card(i) for i in range(1,8))))
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        original = wire.public_get
        def fetch(url, **kwargs):
            if url == job_url(1):
                entered.set()
                if not release.wait(10):raise AssertionError('test did not release response')
            return original(url, **kwargs)
        wire.public_get = fetch
        self.addCleanup(release.set)
        return self.wire(wire), entered, release

    def test_batch_finishes_without_any_more_client_steps(self):
        wire=self.wire();state=self.collector.start(data(detail_budget=3));runner=self.runner()
        self.start(runner,state);result=self.wait(runner)
        self.assertEqual(result['status'],'completed');self.assertEqual(result['code'],'finished')
        self.assertEqual(result['task']['saved_detail_count'],3)
        self.assertEqual(wire.calls,['https://www.liepin.com/robots.txt',
            'https://www.liepin.com/career/360321/',job_url(1),job_url(2),job_url(3)])
        self.assertEqual(self.ledger.summary('liepin')['page']['day'],4)
        self.assertEqual(self.ledger.summary('liepin')['request']['day'],5)
        self.assertTrue((self.workspace.root/'reports'/result['task']['report_id']/'dashboard.html').is_file())
        before=len(wire.calls)
        with self.assertRaises(InputError):self.start(runner,result['task'])
        self.assertEqual(len(wire.calls),before)

    def test_pause_saves_current_body_and_resume_only_fetches_remaining(self):
        wire,entered,release=self.blocked();state=self.collector.start(data(detail_budget=3));runner=self.runner()
        self.start(runner,state);self.assertTrue(entered.wait(5))
        self.assertEqual(runner.pause({'id':state['id']})['status'],'pausing')
        release.set();paused=self.wait(runner)
        self.assertEqual(paused['task']['status'],'paused')
        self.assertEqual([r['status'] for r in paused['task']['details']],['ok','pending','pending'])
        first=paused['task']['details'][0].copy()
        self.start(runner,paused['task']);done=self.wait(runner)
        self.assertEqual(done['status'],'completed');self.assertEqual(done['task']['details'][0],first)
        self.assertEqual(wire.calls.count(job_url(1)),1)
        self.assertEqual(wire.calls.count(job_url(2)),1)
        self.assertEqual(wire.calls.count(job_url(3)),1)

    def test_second_instance_observes_inflight_without_recovery_or_mutation(self):
        wire,entered,release=self.blocked();state=self.collector.start(data(detail_budget=2));runner=self.runner()
        self.start(runner,state);self.assertTrue(entered.wait(5))
        before=self.collector._path(state['id']).read_bytes()
        observer=Collector(self.workspace);other=self.runner(observer)
        self.assertTrue(other.state()['owned_elsewhere'])
        self.assertEqual(observer._path(state['id']).read_bytes(),before)
        for action in (lambda:observer.step({'id':state['id']}),lambda:observer.start(data()),
                       lambda:self.start(other,state),lambda:other.pause({'id':state['id']})):
            with self.assertRaises(CollectionBusy):action()
        other.close();self.assertTrue(runner.is_running())
        release.set();self.assertEqual(self.wait(runner)['status'],'completed')
        self.assertEqual(wire.calls.count(job_url(1)),1)

    def test_actual_other_process_cannot_recover_or_step_active_task(self):
        _,entered,release=self.blocked();state=self.collector.start(data(detail_budget=2));runner=self.runner()
        self.start(runner,state);self.assertTrue(entered.wait(5))
        before=self.collector._path(state['id']).read_bytes()
        script='''import socket,sys
from vibe_job_radar.workspace import Workspace
from vibe_job_radar.collection import Collector
from vibe_job_radar.collection_runtime import CollectionBusy
def blocked(*args,**kwargs):raise AssertionError('no network')
socket.socket.connect=blocked
c=Collector(Workspace(sys.argv[1]));s=c.status({'id':sys.argv[2]})
assert s['in_flight']['queue']=='details'
try:c.step({'id':sys.argv[2]})
except CollectionBusy:print('active-owner-preserved')
else:raise AssertionError('ownership was not enforced')
'''
        env=dict(os.environ,PYTHONPATH=str(Path(__file__).resolve().parents[1]/'src'))
        observed=subprocess.run([sys.executable,'-c',script,str(self.workspace.root),state['id']],
                                env=env,capture_output=True,text=True,timeout=8)
        self.assertEqual(observed.returncode,0,observed.stderr)
        self.assertIn('active-owner-preserved',observed.stdout)
        self.assertEqual(self.collector._path(state['id']).read_bytes(),before)
        release.set();self.wait(runner)

    def test_duplicate_start_and_status_do_not_dispatch_again(self):
        wire,entered,release=self.blocked();state=self.collector.start(data(detail_budget=1));runner=self.runner()
        self.start(runner,state);self.assertTrue(entered.wait(5))
        worker=runner._thread
        for _ in range(3):
            self.assertEqual(self.start(runner,state)['id'],state['id'])
            self.assertTrue(runner.state()['active'])
        self.assertIs(runner._thread,worker)
        release.set();self.wait(runner);self.assertEqual(wire.calls.count(job_url(1)),1)

    def test_application_close_does_not_auto_continue_on_new_server(self):
        wire,entered,release=self.blocked();state=self.collector.start(data(detail_budget=2));runner=self.runner()
        self.start(runner,state);self.assertTrue(entered.wait(5))
        closer=threading.Thread(target=runner.close);closer.start()
        self.assertTrue(runner._stop.wait(2));release.set();closer.join(5)
        stopped=self.wait(runner);self.assertEqual(stopped['code'],'application_closed')
        restarted=self.runner(Collector(self.workspace));before=list(wire.calls)
        self.assertFalse(restarted.state()['active']);self.assertEqual(wire.calls,before)
        self.start(restarted,stopped['task']);done=self.wait(restarted)
        self.assertEqual(done['status'],'completed');self.assertEqual(wire.calls.count(job_url(1)),1)

    def test_network_policy_change_stops_before_next_request(self):
        wire,entered,release=self.blocked();state=self.collector.start(data(detail_budget=2));runner=self.runner()
        self.start(runner,state);self.assertTrue(entered.wait(5))
        policy=self.workspace.network_policy()
        with patch.object(self.workspace,'network_policy',return_value=replace(policy,source=policy.source+'-changed')):
            release.set();stopped=self.wait(runner)
        self.assertEqual(stopped['code'],'conditions_changed')
        self.assertEqual(stopped['task']['details'][1]['status'],'pending')
        self.assertNotIn(job_url(2),wire.calls)

    def test_pending_shutdown_keeps_owner_until_request_really_finishes(self):
        _,entered,release=self.blocked();state=self.collector.start(data(detail_budget=2));runner=self.runner()
        self.start(runner,state);self.assertTrue(entered.wait(5))
        with patch.object(runner._thread,'join'):
            runner.close()  # Simulate returning while the bounded join still has a live request.
        self.assertTrue(runner.is_running())
        other=Collector(self.workspace)
        with self.assertRaises(CollectionBusy):other.step({'id':state['id']})
        with self.assertRaises(InputError):self.start(runner,state)
        release.set();stopped=self.wait(runner)
        self.assertEqual(stopped['task']['details'][1]['status'],'pending')
        self.assertEqual(stopped['code'],'application_closed')

    def test_changed_checkpoint_between_steps_is_not_overwritten(self):
        wire=self.wire();state=self.collector.start(data(detail_budget=2));runner=self.runner()
        between,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        step=self.collector.step
        def pause_after_list(data):
            result=step(data)
            if result['category_attempts']==1 and result['detail_attempts']==0:
                between.set();self.assertTrue(release.wait(5))
            return result
        with patch.object(self.collector,'step',side_effect=pause_after_list):
            self.start(runner,state);self.assertTrue(between.wait(5))
            altered=self.collector._load(state['id']);altered['permit_platforms']=[]
            self.collector._save(altered);expected=self.collector._path(state['id']).read_bytes()
            release.set();stopped=self.wait(runner)
        self.assertEqual(stopped['code'],'conditions_changed')
        self.assertEqual(self.collector._path(state['id']).read_bytes(),expected)
        self.assertNotIn(job_url(1),wire.calls)

    def test_unknown_request_failure_recovery_does_not_replay(self):
        wire=self.wire();original=wire.public_get
        def crash(url,**kwargs):
            if url==job_url(1):raise RuntimeError('controlled unknown request outcome')
            return original(url,**kwargs)
        self.mock.stop();self.mock=patch.object(SafeHTTP,'public_get',side_effect=crash);self.mock.start();self.addCleanup(self.mock.stop)
        state=self.collector.start(data(detail_budget=2));runner=self.runner();self.start(runner,state)
        stopped=self.wait(runner);self.assertEqual(stopped['code'],'interrupted_uncertain')
        self.assertIn('in_flight',stopped['task'])
        with self.assertRaises(InputError):self.start(runner,state)
        restarted=self.runner(Collector(self.workspace))
        recovered=restarted.collector.status({'id':state['id']})
        self.assertEqual(recovered['details'][0]['status'],'interrupted_uncertain')
        self.start(restarted,recovered);done=self.wait(restarted)
        self.assertEqual(done['status'],'needs_attention')
        self.assertEqual(done['task']['details'][0]['status'],'interrupted_uncertain')
        self.assertNotIn(job_url(1),wire.calls)

    def test_shared_limit_finishes_partial_report_without_new_batch(self):
        self.ledger.limits=replace(self.ledger.limits,pages_day=2)
        wire=self.wire();state=self.collector.start(data(detail_budget=3));runner=self.runner()
        self.start(runner,state);stopped=self.wait(runner)
        self.assertEqual(stopped['status'],'needs_attention')
        self.assertEqual([r['status'] for r in stopped['task']['details']],['ok','daily_limit','host_stopped'])
        self.assertTrue(stopped['task']['report_id']);self.assertEqual(len(self.collector.list()['runs']),1)
        self.assertNotIn(job_url(2),wire.calls)

    def test_saved_rate_recovery_uses_same_background_path(self):
        self.ledger.limits=replace(self.ledger.limits,pages_day=2)
        wire=self.wire();state=self.collector.start(data(detail_budget=3));runner=self.runner()
        self.start(runner,state);parent=self.wait(runner)['task'];old=self.collector._path(parent['id']).read_bytes()
        plan=self.collector.category_recovery_preview({'id':parent['id']})
        child=self.collector.category_recovery_start(dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True))['task']
        self.now+=86401;self.start(runner,child);done=self.wait(runner)
        self.assertEqual(done['status'],'completed');self.assertEqual(done['task']['saved_detail_count'],3)
        self.assertEqual(self.collector._path(parent['id']).read_bytes(),old)
        self.assertEqual(wire.calls.count(job_url(1)),1)

    def test_input_consent_mode_and_corrupt_metadata_stop_without_requests(self):
        wire=self.wire();state=self.collector.start(data());runner=self.runner()
        for payload in ({'id':state['id']},{'id':state['id'],'consent':False},
                        {'id':state['id'],'consent':True,'url':job_url(7)}):
            with self.assertRaises(InputError):runner.start(payload)
        url=self.collector.start(dict(mode='urls',urls=job_url(1),roles=['architect'],platforms=['liepin'],
            permit_platforms=['liepin'],consent=True,detail_budget=1,rights_note='controlled runtime test'))
        with self.assertRaises(InputError):self.start(runner,url)
        runner.path.write_text('{broken',encoding='utf-8')
        with self.assertRaises(InputError):self.start(runner,state)
        self.assertEqual(runner.path.read_text(encoding='utf-8'),'{broken');self.assertEqual(wire.calls,[])

    def test_finished_owner_releases_original_manual_and_cli_paths(self):
        self.wire();state=self.collector.start(data(detail_budget=1));runner=self.runner()
        self.start(runner,state);self.wait(runner)
        other=Collector(self.workspace);new=other.start(data(detail_budget=1))
        self.assertEqual(other.step({'id':new['id']})['phase'],'detail')

    def test_corrupted_saved_scope_cannot_expand_background_work(self):
        wire=self.wire();runner=self.runner()
        for changes in ({'detail_budget':6},{'phase':'feed'},{'schema_version':2},{'category_attempts':2}):
            state=self.collector.start(data(detail_budget=1))
            saved=self.collector._load(state['id']);saved.update(changes);self.collector._save(saved)
            with self.assertRaises(InputError):self.start(runner,state)
        self.assertEqual(wire.calls,[])
        state=self.collector.start(data(detail_budget=1));self.collector.step({'id':state['id']})
        saved=self.collector._load(state['id']);saved['details'][0]['url']=job_url(7)
        self.collector._save(saved);before=list(wire.calls)
        with self.assertRaises(InputError):self.start(runner,state)
        self.assertEqual(wire.calls,before)

    def test_saved_continuation_revalidates_original_parent_permission(self):
        wire=self.wire();parent=self.finish(self.collector.start(data(detail_budget=1)),wire)
        plan=self.collector.category_next_preview({'id':parent['id']})
        child=self.collector.category_next_start(dict(id=parent['id'],fingerprint=plan['fingerprint'],consent=True))['task']
        saved=self.collector._load(parent['id']);saved['permit_platforms']=[];self.collector._save(saved)
        runner=self.runner();before=list(wire.calls)
        with self.assertRaises(InputError):self.start(runner,child)
        self.assertEqual(wire.calls,before)

    def test_recovery_of_a_later_batch_preserves_its_original_continuation(self):
        wire=self.wire();runner=self.runner()
        first=self.collector.start(data(detail_budget=2));self.start(runner,first)
        first=self.wait(runner)['task']
        plan=self.collector.category_next_preview({'id':first['id']})
        later=self.collector.category_next_start(dict(id=first['id'],fingerprint=plan['fingerprint'],consent=True))['task']
        self.ledger.limits=replace(self.ledger.limits,pages_day=4)
        self.start(runner,later);later=self.wait(runner)['task']
        self.assertEqual([d['status'] for d in later['details']],['ok','daily_limit'])
        plan=self.collector.category_recovery_preview({'id':later['id']})
        child=self.collector.category_recovery_start(dict(id=later['id'],fingerprint=plan['fingerprint'],consent=True))['task']
        self.now+=86401
        self.start(runner,child);done=self.wait(runner)
        self.assertEqual(done['status'],'completed');self.assertEqual(done['task']['saved_detail_count'],2)
        self.assertEqual(done['task']['category_continuation'],later['category_continuation'])
        self.assertEqual(wire.calls.count(job_url(3)),1);self.assertEqual(wire.calls.count(job_url(4)),1)
