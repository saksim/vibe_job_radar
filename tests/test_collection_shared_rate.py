"""Original default Collector factory, real ledger, artificial one-hop HTTP only."""
from dataclasses import replace
import tempfile
import unittest
from unittest.mock import patch

from vibe_job_radar.collection import Collector, TERMINAL
from vibe_job_radar.guided.rate import Limits, RateLedger, RateLimit
from vibe_job_radar.network import FetchError, Response, SafeHTTP
from vibe_job_radar.workspace import Workspace, InputError
from test_public_category import Wire, data, job_url


class CollectionSharedRateTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.workspace=Workspace(tmp.name);self.now=1_000_000.0
        self.ledger=RateLedger(self.workspace.root/'guided/rates.sqlite',
            Limits(page_interval=0,request_interval=0),clock=lambda:self.now)
        self.factory=patch('vibe_job_radar.collection_rate.RateLedger',return_value=self.ledger)
        self.factory_mock=self.factory.start();self.addCleanup(self.factory.stop)
        self.no_network=patch('socket.socket.connect',side_effect=AssertionError('No real network in quota regression'))
        self.no_network.start();self.addCleanup(self.no_network.stop)
        self.collector=Collector(self.workspace)

    def finish(self,state,wire):
        with patch.object(SafeHTTP,'public_get',side_effect=wire.public_get):
            for _ in range(12):
                state=self.collector.step({'id':state['id']})
                if state['status'] in TERMINAL:return state
        self.fail('Bounded original batch did not finish')

    def url_task(self,url=None):
        return self.collector.start(dict(mode='urls',urls=url or job_url(1),roles=['architect'],
            platforms=['liepin'],permit_platforms=['liepin'],consent=True,detail_budget=1,
            rights_note='Artificial shared-rate regression only.'))

    def test_guided_cooldown_blocks_default_category_and_url_after_restart(self):
        self.ledger.cool('liepin',3600);wire=Wire()
        category=self.finish(self.collector.start(data(detail_budget=1)),wire)
        self.assertEqual(category['category_outcomes'][0]['status'],'cooldown')
        self.assertEqual(category['category_outcomes'][0]['fetch_diagnostic']['http_attempts'],0)
        self.assertEqual(category['category_outcomes'][0]['retry_after_seconds'],3600)
        self.collector=Collector(self.workspace)
        direct=self.finish(self.url_task(),wire)
        self.assertEqual(direct['details'][0]['status'],'cooldown')
        self.assertEqual(wire.calls,[])
        self.assertEqual(self.ledger.summary('liepin')['page']['day'],0)
        self.assertEqual(self.ledger.summary('liepin')['request']['day'],0)
        self.factory_mock.assert_called_with(self.workspace.root/'guided/rates.sqlite')

    def test_default_gate_uses_original_browser_limits_and_same_file(self):
        self.factory.stop()
        from vibe_job_radar.collection_rate import SharedSiteRate
        gate=SharedSiteRate(self.workspace.root,'liepin')
        actual=gate._transport().ledger
        self.assertEqual(actual.limits,Limits())
        self.assertEqual(actual.path,self.ledger.path)
        self.assertEqual(actual.summary('liepin')['request']['day'],0)

    def test_category_details_count_pages_and_all_requests_but_cache_does_not(self):
        wire=Wire();first=self.finish(self.collector.start(data(detail_budget=1)),wire)
        self.assertEqual(first['status'],'completed')
        self.assertEqual(self.ledger.summary('liepin')['page']['day'],2)
        self.assertEqual(self.ledger.summary('liepin')['request']['day'],3)
        self.assertEqual(len(wire.calls),3)
        before=self.ledger.summary('liepin');old_report=first['report_id']
        self.ledger.cool('liepin',3600)
        cached=self.finish(self.url_task(),wire)
        self.assertEqual(cached['details'][0]['status'],'fresh_reused')
        self.assertEqual(cached['detail_attempts'],0)
        self.assertEqual(self.ledger.summary('liepin'),before)
        self.assertEqual(len(wire.calls),3)
        self.assertNotEqual(cached['report_id'],old_report)

    def test_response_refusals_persist_for_browser_and_new_advanced_tasks(self):
        from vibe_job_radar.public_category import URL
        for status in (401,403,429):
            with self.subTest(status=status):
                self.now+=10_000
                wire=Wire(statuses={URL:status})
                original=wire.public_get
                def get(url):
                    response=original(url)
                    if response.status==429:response.headers['retry-after']='1200'
                    return response
                wire.public_get=get
                self.collector=Collector(self.workspace)
                result=self.finish(self.collector.start(data(detail_budget=1)),wire)
                self.assertEqual(result['category_outcomes'][0]['status'],f'http_{status}')
                shared=RateLedger(self.ledger.path,self.ledger.limits,clock=lambda:self.now)
                with self.assertRaises(RateLimit) as error:shared.reserve('liepin','page')
                self.assertEqual(error.exception.code,'cooldown')
                self.assertEqual(error.exception.wait,1200 if status==429 else 300)
                before=len(wire.calls)
                self.collector=Collector(self.workspace)
                next_task=self.finish(self.url_task(),wire)
                self.assertEqual(next_task['details'][0]['status'],'cooldown')
                self.assertEqual(len(wire.calls),before)

    def test_real_transport_exception_also_persists_cooldown(self):
        wire=Wire();original=wire.public_get
        def get(url):
            if not url.endswith('/robots.txt'):raise FetchError('http_429',retry_after=1800)
            return original(url)
        wire.public_get=get
        result=self.finish(self.url_task(),wire)
        self.assertEqual(result['details'][0]['status'],'http_429')
        self.assertEqual(result['details'][0]['retry_after_seconds'],1800)
        with self.assertRaises(RateLimit) as error:self.ledger.reserve('liepin','request')
        self.assertEqual(error.exception.code,'cooldown')

    def test_each_redirect_hop_is_a_request_without_an_extra_page_budget(self):
        wire=Wire();original=wire.public_get
        def get(url):
            if url==job_url(1):
                wire.calls.append(url)
                return Response(302,{'location':job_url(2)},b'',url)
            return original(url)
        wire.public_get=get
        result=self.finish(self.url_task(),wire)
        self.assertEqual(wire.calls,['https://www.liepin.com/robots.txt',job_url(1),job_url(2)])
        self.assertEqual(result['details'][0]['fetch_diagnostic']['http_attempts'],3)
        self.assertEqual(self.ledger.summary('liepin')['request']['day'],3)
        self.assertEqual(self.ledger.summary('liepin')['page']['day'],1)

    def test_stricter_publisher_window_survives_new_task_and_weaker_robots(self):
        wire=Wire(robots='User-agent: *\nDisallow:\nCrawl-delay: 61\nRequest-rate: 1/120\n')
        result=self.finish(self.url_task(),wire)
        self.assertEqual(result['details'][0]['status'],'publisher_wait')
        self.assertEqual(wire.calls,['https://www.liepin.com/robots.txt'])
        self.assertEqual(self.ledger.summary('liepin')['request']['day'],1)
        self.now+=121;self.collector=Collector(self.workspace)
        weaker=Wire()
        result=self.finish(self.url_task(),weaker)
        self.assertEqual(result['details'][0]['status'],'publisher_wait')
        self.assertEqual(weaker.calls,['https://www.liepin.com/robots.txt'])
        self.assertEqual(self.ledger.summary('liepin')['request']['day'],2)

    def test_shared_daily_limit_is_not_reset_by_a_new_task_or_collector(self):
        self.ledger.limits=replace(self.ledger.limits,pages_day=1)
        self.ledger.reserve('liepin','page')
        wire=Wire()
        for _ in range(2):
            self.collector=Collector(self.workspace)
            result=self.finish(self.collector.start(data(detail_budget=1)),wire)
            self.assertEqual(result['category_outcomes'][0]['status'],'daily_limit')
        self.assertEqual(wire.calls,[])
        self.assertEqual(self.ledger.summary('liepin')['page']['day'],1)
        self.now+=86401
        state=self.collector.start(data(detail_budget=1))
        with patch.object(SafeHTTP,'public_get',side_effect=wire.public_get):
            state=self.collector.step({'id':state['id']})
        self.assertEqual(state['category_outcomes'][0]['status'],'ok')
        self.assertEqual(len(wire.calls),2)

    def test_clock_rollback_stops_before_http(self):
        self.ledger.reserve('liepin','request');self.now-=300
        wire=Wire();result=self.finish(self.url_task(),wire)
        self.assertEqual(result['details'][0]['status'],'clock_rollback')
        self.assertEqual(wire.calls,[])

    def test_partial_success_and_unattempted_items_survive_a_shared_limit(self):
        self.ledger.limits=replace(self.ledger.limits,pages_day=2)
        wire=Wire();result=self.finish(self.collector.start(data(detail_budget=3)),wire)
        self.assertEqual(result['status'],'needs_attention')
        self.assertEqual([r['status'] for r in result['details']],['ok','daily_limit','host_stopped'])
        self.assertEqual(result['detail_attempts'],2)
        self.assertEqual(result['saved_detail_count'],1)
        self.assertTrue(result['report_id'])
        self.assertEqual(len(wire.calls),3)
        self.assertEqual(self.ledger.summary('liepin')['page']['day'],2)
        with self.assertRaises(InputError):self.collector.category_next_preview({'id':result['id']})

    def test_corrupted_ledger_does_not_fall_back_to_unmetered_transport(self):
        self.ledger.path.write_bytes(b'Artificial invalid ledger bytes')
        wire=Wire();result=self.finish(self.url_task(),wire)
        self.assertEqual(result['details'][0]['status'],'rate_storage_error')
        self.assertEqual(wire.calls,[])
        self.assertEqual(self.ledger.path.read_bytes(),b'Artificial invalid ledger bytes')

    def test_cooldown_storage_error_still_preserves_the_actual_upstream_refusal(self):
        from vibe_job_radar.guided.contracts import CrawlError
        for raised in (False,True):
            with self.subTest(raised=raised):
                wire=Wire(statuses={job_url(1):429});original=wire.public_get
                def get(url):
                    if raised and url==job_url(1):raise FetchError('http_429',retry_after=900)
                    return original(url)
                wire.public_get=get
                with patch.object(self.ledger,'cool',side_effect=CrawlError('rate_storage_error')):
                    result=self.finish(self.url_task(),wire)
                self.assertEqual(result['details'][0]['status'],'rate_storage_error')
                self.assertEqual(result['details'][0]['fetch_diagnostic']['http_status'],429)
                self.assertFalse(result['report_id'])

    def test_removed_live_ledger_is_not_recreated_for_the_next_detail(self):
        wire=Wire();state=self.collector.start(data(detail_budget=1))
        with patch.object(SafeHTTP,'public_get',side_effect=wire.public_get):
            state=self.collector.step({'id':state['id']})
        self.assertEqual(len(wire.calls),2)
        self.ledger.path.unlink()
        result=self.finish(state,wire)
        self.assertEqual(result['details'][0]['status'],'rate_storage_error')
        self.assertEqual(len(wire.calls),2)
        self.assertFalse(self.ledger.path.exists())

    def test_preview_and_task_creation_do_not_consume_a_visit(self):
        from vibe_job_radar.collection_guidance import preview
        before=self.ledger.summary('liepin')
        self.assertTrue(preview(self.workspace,data(detail_budget=1))['ready'])
        self.collector.start(data(detail_budget=1));self.url_task()
        self.assertEqual(self.ledger.summary('liepin'),before)
        self.factory_mock.assert_not_called()
