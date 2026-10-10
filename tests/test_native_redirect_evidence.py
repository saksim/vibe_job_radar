
"""Artificial native replies; preserve refusal, accounting and optional observation."""
import json
import sqlite3
import unittest
from unittest.mock import patch

import test_native_acquisition as fixture
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.diagnostic_trace import DiagnosticTrace, notify, observe

SECRET = 'PRIVATE-REDIRECT-DIAGNOSTIC-SECRET'
DESTINATION = 'https://user:' + SECRET + '@wow.liepin.com/t1234567/pc.html?token=' + SECRET + '#' + SECRET


class NativeRedirectEvidenceTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.socket.connect', side_effect=AssertionError('No network in these cases'))
        self.connect = guard.start()
        self.addCleanup(guard.stop)

    def tearDown(self):
        self.assertEqual(self.connect.call_count, 0)

    def backend(self):
        f = fixture.NativeControllerTests('test_native_request_not_replayed')
        f.setUp()
        self.addCleanup(f.doCleanups)
        return f

    def refuse(self, f, location, hops=0):
        b = f.b
        b._paused('session', f.req('/redirect', 'GET', 'Document'))
        b._hops[('session', 'net-1')] = hops
        b._paused('session', f.response(302, {'location': location}, path='/redirect',
                                       method='GET', kind='Document'))
        self.assertEqual(b.error, 'redirect_requires_attention')
        self.assertEqual(b.native_counts['document'], 1)
        self.assertEqual(b.native_counts['responses'], 0)
        self.assertEqual(b._send.call_args.args[1], 'Fetch.failRequest')
        with sqlite3.connect(b.wire.ledger.path) as db:
            self.assertEqual(dict(db.execute('SELECT kind,COUNT(*) FROM visits GROUP BY kind')),
                             {'page': 1, 'request': 1})

    def test_cross_origin_failure_keeps_safe_destination_and_original_source(self):
        f = self.backend()
        self.refuse(f, DESTINATION)
        snapshot = f.b._diagnostics.snapshot()
        event = snapshot['first_failure']
        self.assertEqual(event['status'], 302)
        self.assertEqual(event['host'], fixture.HOST)
        self.assertEqual(event['redirect'], {
            'reason': 'cross_origin', 'prior_hops': 0,
            'target': {'host': 'wow.liepin.com', 'path_template': '/:segment/:segment',
                       'query_names': [':redacted']}})
        self.assertEqual(snapshot['first_backend_stop']['redirect'], event['redirect'])
        self.assertNotIn(SECRET, json.dumps(snapshot))
        self.assertNotIn('t1234567', json.dumps(snapshot))

    def test_hop_limit_uses_same_failure_code_but_preserves_its_distinct_reason(self):
        f = self.backend()
        self.refuse(f, '/search', 5)
        self.assertEqual(f.b._hops[('session', 'net-1')], 6)
        self.assertEqual(f.b._diagnostics.snapshot()['first_failure']['redirect'], {
            'reason': 'hop_limit', 'prior_hops': 5,
            'target': {'host': fixture.HOST, 'path_template': '/search', 'query_names': []}})

    def test_existing_authority_comparison_still_refuses_an_explicit_port(self):
        f = self.backend()
        self.refuse(f, fixture.URL + ':443/search')
        self.assertEqual(f.b._diagnostics.snapshot()['first_failure']['redirect']['reason'], 'cross_origin')

    def test_allowed_same_origin_redirect_is_not_recorded_as_a_refusal(self):
        f = self.backend()
        f.b._paused('session', f.req('/redirect', 'GET', 'Document'))
        f.b._paused('session', f.response(302, {'location': '/search'}, path='/redirect',
                                         method='GET', kind='Document'))
        f.b._send.assert_called_with('session', 'Fetch.continueResponse', {'requestId': 'fetch-1'})
        self.assertIsNone(f.b.error)
        self.assertEqual(f.b.native_counts['responses'], 1)
        self.assertTrue(all('redirect' not in row for row in f.b._diagnostics.snapshot()['events']))

    def test_http_refusal_remains_separate(self):
        f = self.backend()
        f.b._paused('session', f.req('/redirect', 'GET', 'Document'))
        f.b._paused('session', f.response(403, {'location': DESTINATION}, path='/redirect',
                                         method='GET', kind='Document'))
        self.assertEqual(f.b.error, 'http_403')
        self.assertNotIn('redirect', f.b._diagnostics.snapshot()['first_failure'])

    def test_not_modified_response_is_not_relabelled_as_a_redirect(self):
        f = self.backend()
        f.b._paused('session', f.req('/redirect', 'GET', 'Document'))
        f.b._paused('session', f.response(304, {'location': DESTINATION}, path='/redirect',
                                         method='GET', kind='Document'))
        self.assertEqual(f.b.error, 'native_unaccounted_response')
        self.assertNotIn('redirect', f.b._diagnostics.snapshot()['first_failure'])

    def test_missing_observer_does_not_change_refusal_or_charges(self):
        f = self.backend()
        f.b._diagnostics = None
        self.refuse(f, DESTINATION)

    def test_disabled_observer_retains_no_redirect_information(self):
        f = self.backend()
        f.b._diagnostics.disable()
        self.refuse(f, DESTINATION)
        snapshot = f.b._diagnostics.snapshot()
        self.assertEqual(snapshot['events'], [])
        self.assertIsNone(snapshot['first_failure'])
        self.assertIsNone(snapshot['first_backend_stop'])

    def test_observer_failure_does_not_change_business_error_or_charges(self):
        f = self.backend()
        with patch.object(DiagnosticTrace, 'redirect', side_effect=RuntimeError(SECRET)):
            self.refuse(f, DESTINATION)
        snapshot = f.b._diagnostics.snapshot()
        self.assertEqual(snapshot['observer_errors'], 1)
        self.assertEqual(snapshot['first_failure']['code'], 'redirect_requires_attention')
        self.assertNotIn(SECRET, json.dumps(snapshot))


class RedirectRecorderEvidenceTests(unittest.TestCase):
    def failed(self, trace, **fields):
        with self.assertRaises(CrawlError):
            with observe(trace, 'http_request', url=fixture.URL + '/redirect'):
                notify(trace, 'mark', status=302)
                notify(trace, 'redirect', **fields)
                raise CrawlError('redirect_requires_attention')
        return trace.snapshot()

    def test_metadata_is_bounded_and_unknown_arguments_are_not_copied(self):
        for url in (None, {}, 'file:///private/' + SECRET, 'https://[invalid', 'x' * 10000):
            with self.subTest(url_type=type(url).__name__):
                t = DiagnosticTrace('b' * 32, 'fixture')
                snapshot = self.failed(t, url=url, reason=[SECRET], prior_hops=True)
                self.assertEqual(snapshot['first_failure']['redirect'], {
                    'reason': 'unknown', 'prior_hops': None,
                    'target': {'host': '', 'path_template': '', 'query_names': []}})
                self.assertNotIn(SECRET, json.dumps(snapshot))

    def test_first_failure_survives_eviction_and_caller_snapshot_mutation(self):
        t = DiagnosticTrace('b' * 32, 'fixture', max_events=8)
        snapshot = self.failed(t, url=DESTINATION, reason='cross_origin', prior_hops=0)
        snapshot['first_failure']['redirect']['target']['query_names'].append(SECRET)
        for _ in range(20):
            with observe(t, 'http_request', url=fixture.URL + '/search'):
                pass
        later = t.snapshot()
        self.assertGreater(later['dropped_events'], 0)
        self.assertEqual(later['first_failure']['redirect']['reason'], 'cross_origin')
        self.assertNotIn(SECRET, json.dumps(later))
        self.assertTrue(all('redirect' not in e for e in later['events']))

    def test_target_is_kept_only_on_the_current_span(self):
        t = DiagnosticTrace('b' * 32, 'fixture')
        with observe(t, 'collection'):
            self.failed(t, url=DESTINATION, reason='cross_origin', prior_hops=2)
        snapshot = t.snapshot()
        self.assertEqual(snapshot['first_failure']['parent_stage'], 'collection')
        self.assertNotIn('redirect', snapshot['events'][-1])
        notify(t, 'redirect', url=DESTINATION, reason='cross_origin', prior_hops=0)
        self.assertEqual(t.snapshot(), snapshot)

    def test_disable_clears_the_nested_evidence_and_prevents_later_recording(self):
        t = DiagnosticTrace('b' * 32, 'fixture')
        self.failed(t, url=DESTINATION, reason='cross_origin', prior_hops=0)
        t.disable()
        with observe(t, 'http_request', url=fixture.URL):
            notify(t, 'redirect', url=DESTINATION, reason='cross_origin', prior_hops=0)
        snapshot = t.snapshot()
        self.assertEqual(snapshot['events'], [])
        self.assertIsNone(snapshot['first_failure'])
        self.assertNotIn('wow.liepin.com', json.dumps(snapshot))
