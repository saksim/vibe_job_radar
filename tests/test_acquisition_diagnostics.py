"""D01 metadata tests: artificial pages/transports; never contact job sites."""
from __future__ import annotations

from contextvars import Context
import json
from pathlib import Path
import tempfile
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

import test_guided as fixtures
import test_guided_merge_review as browser_fixtures
from vibe_job_radar.guided.diagnostic_trace import DiagnosticTrace, observe, notify, safe_target
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.adapters import Registry
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.guided.transport import PinnedTransport, WireResponse
from vibe_job_radar.guided.rate import RateLedger, Limits, RateLimit
from vibe_job_radar.workspace import Workspace, InputError

TASK = 'a' * 32
HOST = 'jobs.fixture.test'
SECRET = 'DO-NOT-EXPORT-PRIVATE-987654'


def trace(**kw):
    return DiagnosticTrace(TASK, 'fixture', **kw)


class RecorderTests(unittest.TestCase):
    def test_sensitive_url_parts_are_not_recorded(self):
        value = safe_target(f'https://user:{SECRET}@{HOST}/job/{SECRET}?q={SECRET}&{SECRET}=value#'+SECRET)
        self.assertEqual(value['host'], HOST)
        self.assertEqual(value['path_template'], '/job/:segment')
        self.assertEqual(value['query_names'], [':redacted', 'q'])
        self.assertNotIn(SECRET, json.dumps(value))
        self.assertNotIn('user:', json.dumps(value))

    def test_encoded_secrets_are_not_preserved(self):
        value = safe_target(f'https://{HOST}/%73ecret/%2F/token?token=abc&%73ecret=x')
        self.assertEqual(value['path_template'], '/:segment/:segment/:segment')
        self.assertEqual(value['query_names'], [':redacted'])

    def test_arbitrary_input_is_bounded(self):
        for value in (None, {}, 'file:///secret', 'https://[invalid', 'x'*10000):
            self.assertEqual(safe_target(value)['host'], '')
        value = safe_target('https://'+HOST+'/'+'/'.join(['secret']*200)+'?'+'&'.join(['q=x']*100))
        self.assertLess(len(json.dumps(value)), 500)

    def test_disabled_observer_never_changes_return_or_exception(self):
        with observe(None, 'listing'):
            value = 17
        self.assertEqual(value, 17)
        err = CrawlError('robots_denied')
        with self.assertRaises(CrawlError) as got:
            with observe(None, 'robots'):
                raise err
        self.assertIs(got.exception, err)

    def test_exception_message_and_unknown_code_are_never_saved(self):
        t = trace()
        with self.assertRaises(CrawlError):
            with observe(t, 'detail_parse'):
                raise CrawlError(SECRET)
        value = t.snapshot()
        self.assertEqual(value['first_failure']['code'], 'operation_error')
        self.assertNotIn(SECRET, json.dumps(value))

    def test_first_failure_survives_ring_eviction(self):
        t = trace(max_events=8)
        with self.assertRaises(CrawlError):
            with observe(t, 'route', impact='required_by_backend'):
                raise CrawlError('write_not_allowed')
        for _ in range(30):
            with observe(t, 'http_request'):
                pass
        value = t.snapshot()
        self.assertEqual(len(value['events']), 8)
        self.assertGreater(value['dropped_events'], 0)
        self.assertEqual(value['first_content_candidate']['code'], 'write_not_allowed')
        self.assertEqual(value['first_failure']['policy'], 'post_auth_only')

    def test_optional_resource_does_not_become_proven_business_failure(self):
        t = trace()
        with self.assertRaises(CrawlError):
            with observe(t, 'route', impact='optional'):
                raise CrawlError('http_403')
        self.assertIsNone(t.snapshot()['first_content_candidate'])
        self.assertIsNotNone(t.snapshot()['first_failure'])

    def test_nested_spans_keep_first_lower_layer_failure(self):
        t = trace()
        with self.assertRaises(CrawlError):
            with observe(t, 'listing'):
                with observe(t, 'robots', actor='transport'):
                    raise CrawlError('robots_unavailable')
        events = t.snapshot()['events']
        self.assertEqual(t.first_failure['stage'], 'robots')
        self.assertEqual(events[1]['parent_span_id'], events[0]['span_id'])
        self.assertFalse(t._frames)

    def test_waits_are_not_classified_as_content_failures(self):
        t = trace()
        with self.assertRaises(CrawlError):
            with observe(t, 'http_request'):
                raise CrawlError('http_429')
        self.assertEqual(t.snapshot()['events'][-1]['outcome'], 'waiting')
        self.assertIsNone(t.first_failure)

    def test_snapshots_are_detached(self):
        t = trace()
        with observe(t, 'listing', url='https://'+HOST+'/search?q=x'):
            pass
        a = t.snapshot(); a['events'][0]['query_names'].append(SECRET)
        self.assertNotIn(SECRET, json.dumps(t.snapshot()))

    def test_disable_clears_metadata_and_stops_recording(self):
        t = trace()
        with observe(t, 'listing'):
            pass
        t.disable()
        with observe(t, 'listing'):
            pass
        self.assertEqual(t.snapshot()['events'], [])
        self.assertFalse(t._frames)

    def test_observer_failure_does_not_mask_operation_and_no_stack_leak(self):
        t = trace(clock=lambda: float('nan'))
        for _ in range(40):
            with observe(t, 'listing'):
                pass
        self.assertFalse(t._frames)
        self.assertEqual(t.observer_errors, 40)
        with self.assertRaisesRegex(RuntimeError, 'actual failure'):
            with observe(t, 'listing'):
                raise RuntimeError('actual failure')

    def test_capacity_and_id_validation(self):
        for size in (True, 0, 501, 1.5):
            with self.assertRaises(ValueError): trace(max_events=size)
        with self.assertRaises(ValueError): DiagnosticTrace('../bad', 'fixture')

    def test_metadata_contains_local_code_identity_not_machine_paths(self):
        value = trace().snapshot()
        self.assertEqual(len(value['runtime']['code_sha256']['diagnostic_trace.py']), 64)
        self.assertNotIn(str(Path.home()), json.dumps(value))
        self.assertEqual(value['runtime']['live_verification'], 'not_established_by_diagnostics')


class BrowserTraceTests(unittest.TestCase):
    def make(self, kind='xhr', method='GET'):
        b = browser_fixtures.BrowserReviewTests().backend()
        t = trace(); b.bind_diagnostics(t)
        r = Mock(); r.request.url = f'https://{HOST}/search?q={SECRET}'
        r.request.resource_type = kind; r.request.method = method
        r.request.post_data_buffer = SECRET.encode()
        r.request.all_headers.return_value = {'Cookie': SECRET, 'Authorization': SECRET}
        b.wire.fetch.return_value = WireResponse(200, {}, SECRET.encode())
        return b, t, r

    def test_local_post_block_records_policy_without_sending(self):
        b,t,r = self.make(method='POST')
        Context().run(b._route, r)
        b.wire.fetch.assert_not_called(); r.abort.assert_called_once()
        first=t.snapshot()['first_content_candidate']
        self.assertEqual(first['code'], 'write_not_allowed')
        self.assertTrue(first['local_block'])
        self.assertNotIn(SECRET, json.dumps(t.snapshot()))
        self.assertIsNone(b.error)  # Preserve existing required/optional behavior.

    def test_missing_resource_domain_is_separate_from_publisher_denial(self):
        b,t,r = self.make(); b.wire.allowed_resource.return_value=False
        b._route(r)
        self.assertEqual(t.first_failure['policy'], 'resource_domains')
        b.wire.fetch.assert_not_called()

    def test_publisher_denial_remains_unchanged(self):
        b,t,r = self.make(); b.wire.fetch.side_effect=CrawlError('http_403')
        b._route(r)
        self.assertEqual(b.error, 'http_403')
        self.assertFalse(t.first_failure['local_block'])
        r.abort.assert_called_once(); b.wire.fetch.assert_called_once()

    def test_optional_denial_does_not_poison_task(self):
        b,t,r = self.make(kind='image'); b.wire.fetch.side_effect=CrawlError('http_401')
        b._route(r)
        self.assertIsNone(b.error)
        self.assertIsNone(t.first_content_candidate)

    def test_success_does_not_capture_body_or_headers(self):
        b,t,r = self.make(); b._route(r)
        r.fulfill.assert_called_once()
        self.assertEqual(t.snapshot()['events'][-1]['status'], 200)
        self.assertNotIn(SECRET,json.dumps(t.snapshot()))

    def test_observer_failure_does_not_change_browser_result(self):
        b,t,r = self.make()
        with patch.object(t, 'begin', side_effect=RuntimeError(SECRET)):
            b._route(r)
        r.fulfill.assert_called_once(); self.assertIsNone(b.error)


class TransportTraceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.wire=PinnedTransport(fixtures.fixture_adapter(),
            RateLedger(Path(self.tmp.name)/'rates.sqlite',Limits(request_interval=0,page_interval=0)),threading.Event())
        self.trace=trace(); self.wire._diagnostics=self.trace

    def test_missing_file_is_distinct_from_unreachable_and_does_not_expose_body(self):
        response = WireResponse(404, {'content-type':'text/html'}, SECRET.encode())
        with patch.object(self.wire, 'fetch', return_value=response):
            self.wire.ensure_robots('https://' + HOST + '/search')
        snapshot = self.trace.snapshot()
        self.assertIn('robots_file_absent', {e['code'] for e in snapshot['events']})
        self.assertNotIn(SECRET, json.dumps(snapshot))
        self.assertIsNone(self.trace.first_failure)

    def test_robots_html_categorized_without_relaxing_decision(self):
        with patch.object(self.wire,'fetch',return_value=WireResponse(200,{'content-type':'text/html'},SECRET.encode())) as fetch:
            with self.assertRaises(CrawlError) as got:self.wire.ensure_robots('https://'+HOST+'/search')
        self.assertEqual(got.exception.code,'robots_unavailable');self.assertEqual(fetch.call_count,1)
        codes={e['code'] for e in self.trace.snapshot()['events']}
        self.assertIn('robots_response_html',codes);self.assertNotIn(SECRET,json.dumps(self.trace.snapshot()))

    def test_robots_denial_is_unchanged(self):
        with patch.object(self.wire,'fetch',return_value=WireResponse(200,{},b'User-agent: *\nDisallow: /')):
            with self.assertRaises(CrawlError) as got:self.wire.ensure_robots('https://'+HOST+'/job/1')
        self.assertEqual(got.exception.code,'robots_denied')
        self.assertEqual(self.trace.first_failure['policy'],'robots_rules')

    def test_extensions_are_observed_not_new_permissions(self):
        response=WireResponse(200,{},b'User-agent: *\nDisallow: /*?secret$\n')
        with patch.object(self.wire,'fetch',return_value=response):
            self.wire.ensure_robots('https://'+HOST+'/search')
        self.assertIn('robots_extensions_observed',{e['code'] for e in self.trace.snapshot()['events']})
        self.assertNotIn('secret',json.dumps(self.trace.snapshot()))

    def test_dns_failure_is_not_publisher_denial(self):
        with patch('socket.getaddrinfo',side_effect=socket.gaierror(SECRET)),patch('vibe_job_radar.guided.transport.PinnedHTTPSConnection') as conn:
            with self.assertRaises(CrawlError):self.wire.fetch('https://'+HOST+'/search')
        conn.assert_not_called()
        self.assertEqual(self.trace.first_failure['code'],'dns_error')
        self.assertIsNone(self.trace.first_failure['local_block'])

    def test_tls_failure_preserves_certificate_checks_and_original_error(self):
        import ssl
        with patch('vibe_job_radar.guided.transport.validate_public_url',return_value=(HOST,'93.184.216.34','/search')), \
             patch('vibe_job_radar.guided.transport.PinnedHTTPSConnection') as conn:
            conn.return_value.request.side_effect=ssl.SSLCertVerificationError(SECRET)
            with self.assertRaises(CrawlError) as got:self.wire.fetch('https://'+HOST+'/search')
        self.assertEqual(got.exception.code,'tls_verification_failed')
        self.assertEqual(self.trace.first_failure['code'],'tls_verification_failed')
        self.assertNotIn(SECRET,json.dumps(self.trace.snapshot()))

    def test_429_status_and_cooldown_preserved_without_retry(self):
        with patch('vibe_job_radar.guided.transport.validate_public_url',return_value=(HOST,'93.184.216.34','/search')), \
             patch('vibe_job_radar.guided.transport.PinnedHTTPSConnection') as conn:
            response=conn.return_value.getresponse.return_value;response.status=429
            response.getheaders.return_value=[('Retry-After','600')]
            with self.assertRaises(RateLimit):self.wire.fetch('https://'+HOST+'/search')
        conn.assert_called_once();conn.return_value.close.assert_called_once()
        self.assertEqual(self.trace.snapshot()['events'][-1]['status'],429)
        with self.assertRaises(RateLimit):self.wire.ledger.reserve('fixture','request')


class ServiceTraceTests(unittest.TestCase):
    def setUp(self):
        self.helper=fixtures.ServiceTests();self.helper.setUp();self.addCleanup(self.helper.doCleanups)
        self.service=self.helper.service

    def test_default_off_and_preview_does_not_touch_backend(self):
        ident=self.helper.create()
        with patch.object(self.service,'_backend',side_effect=AssertionError('no browser')):
            value=self.service.diagnostics({'id':ident})
        self.assertFalse(value['enabled']);self.assertEqual(value['events'],[])

    def test_opt_in_full_pipeline_shares_id_and_no_private_material(self):
        ident=self.helper.create(diagnostics=True,rights_note=SECRET)
        state=self.helper.job(ident)
        self.service.action({'id':ident,'action':'collect','selected':[state['cards'][0]['id']]});self.helper.wait()
        value=self.service.diagnostics({'id':ident})
        self.assertEqual(value['trace_id'],ident)
        stages={e['stage'] for e in value['events']}
        self.assertTrue({'task','browser_session','listing','list_parse','detail_parse','persist','report'} <= stages)
        self.assertNotIn(SECRET,json.dumps(value));self.assertNotIn('Cursor',json.dumps(value))
        self.assertEqual(self.helper.job(ident)['status'],'completed')
        self.assertTrue(self.helper.job(ident)['report_id'])

    def test_enable_then_disable_does_not_delete_jobs_or_reset_quotas(self):
        ident=self.helper.create(diagnostics=True)
        before=self.helper.job(ident)['cards']
        self.service.diagnostics({'id':ident,'enabled':False})
        self.assertFalse(self.service.diagnostics({'id':ident})['enabled'])
        self.assertEqual(self.helper.job(ident)['cards'],before)
        value=self.service.diagnostics({'id':ident,'enabled':True})
        self.assertTrue(value['enabled']);self.assertEqual(value['events'],[])

    def test_preview_rejects_secrets_unknown_fields_and_invalid_id(self):
        ident=self.helper.create()
        for data in ({'id':ident,'cookie':SECRET},{'id':ident,'enabled':'true'},{'id':'../secret'}):
            with self.assertRaises(InputError):self.service.diagnostics(data)

    def test_config_cannot_change_during_active_operation(self):
        ident=self.helper.create()
        self.service._busy=True
        try:
            with self.assertRaises(InputError):self.service.diagnostics({'id':ident,'enabled':True})
        finally:self.service._busy=False

    def test_service_memory_is_bounded(self):
        for i in range(40):
            self.service._trace_for({'id':f'{i:032x}','platform':'fixture','diagnostics_enabled':True})
        self.assertEqual(len(self.service._traces),30)

    def test_restart_loses_recording_not_prior_task(self):
        ident=self.helper.create(diagnostics=True)
        old=self.service.diagnostics({'id':ident})
        self.service.close()  # Restart means the previous owner has exited.
        other=GuidedService(self.helper.workspace,registry=Registry([fixtures.fixture_adapter()]),backend_factory=fixtures.FakeBackend)
        self.addCleanup(other.close)
        new=other.diagnostics({'id':ident})
        self.assertNotEqual(old['recording_id'],new['recording_id']);self.assertEqual(new['events'],[])
        self.assertEqual(other._load(ident)['cards'],self.helper.job(ident)['cards'])

    def test_non_boolean_optin_is_rejected(self):
        with self.assertRaises(InputError):self.helper.create(diagnostics='true')


class DiagnosticHTTPTests(unittest.TestCase):
    def setUp(self):
        self.helper=fixtures.GuidedHTTPTests();self.helper.setUp();self.addCleanup(self.helper.doCleanups)

    def test_preview_still_requires_local_token_and_same_origin(self):
        call=self.helper.call
        self.assertEqual(call('/api/guided/diagnostics',{'id':TASK},authorized=False)[0],403)
        self.assertEqual(call('/api/guided/diagnostics',{'id':TASK},origin='https://evil.test')[0],403)

    def test_preview_reaches_service_without_network(self):
        server=self.helper.server
        with patch.object(server.guided,'diagnostics',return_value={'enabled':False,'events':[]}) as read:
            status,_=self.helper.call('/api/guided/diagnostics',{'id':TASK})
        self.assertEqual(status,200);read.assert_called_once_with({'id':TASK})

    def test_ui_has_optin_preview_and_snapshot_only_download(self):
        _,html=self.helper.call('/guided',authorized=False)
        _,js=self.helper.call('/guided.js',authorized=False)
        self.assertIn(b'name="diagnostics" type="checkbox"',html)
        for name in (b'enable-acquisition-trace',b'preview-acquisition-trace',b'download-acquisition-trace',b'disable-acquisition-trace'):
            self.assertIn(name,html);self.assertIn(name,js)
        self.assertNotIn(b'innerHTML',js)
        self.assertIn(b'tracePreview.id!==active()',js)


if __name__=='__main__':unittest.main()
