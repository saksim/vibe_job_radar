"""Actual observer/controller failures, with no browser, CA or external I/O."""
from collections import Counter
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_native_acquisition as fixture
from vibe_job_radar.guided.native_browser import NativeBackend

ROOT = Path(__file__).resolve().parents[1]
with patch.object(sys, 'path', [str(ROOT / 'scripts'), *sys.path]):
    from run_native_auth_probe import ObservedBackend
    from run_native_liepin_probe import SearchObserver
    import native_failure_evidence as evidence

SECRET = 'PRIVATE_ACCOUNT_BODY_COOKIE_URL_SHOULD_NEVER_APPEAR'


class NativeFailureEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.helper = fixture.NativeControllerTests()
        self.helper.setUp()
        self.addCleanup(self.helper.doCleanups)
        self.b = self.helper.b
        self.b.__class__ = ObservedBackend
        self.b.probe = {'sent': Counter(), 'acknowledged': Counter(), 'events': Counter(),
            'attachments': Counter(), 'auth': Counter(), 'snapshots': []}
        self.b.tunnel.thread = SimpleNamespace(is_alive=lambda: True)
        self.b.tunnel.connections = 3
        self.b.tunnel._closed = False
        self.b.tunnel._sockets = {SECRET}

    def event(self, error='net::ERR_CONNECTION_CLOSED', **params):
        return {'sessionId': 'session', 'message': json.dumps({
            'method': 'Network.loadingFailed', 'params': {
                'requestId': 'net-1', 'errorText': error, 'type': 'Fetch',
                'canceled': False, **params}})}

    def admit(self):
        self.b._paused('session', self.helper.req())
        self.b._send.reset_mock()

    def test_required_failure_saved_before_record_is_consumed_and_cleanup(self):
        self.admit()
        self.b._received(self.event())
        self.assertEqual(self.b.error, 'network_error')
        self.assertEqual(self.b._requests, {})
        first = self.b.probe['first_fatal']
        row = first['latest_loading_failure']
        self.assertEqual(row['browser_error'], 'ERR_CONNECTION_CLOSED')
        self.assertTrue(row['request_observed'])
        self.assertEqual(row['role'], 'business')
        self.assertTrue(row['epoch_is_current'])
        self.assertEqual(row['connection_facts']['active_requests'], 1)
        self.assertFalse(first['connection_facts']['guard_closed'])
        self.assertTrue(first['connection_facts']['guard_thread_alive'])
        self.assertEqual(first['connection_facts']['guard_connections'], 3)
        self.b._send.assert_not_called()
        self.assertNotIn(SECRET, json.dumps(self.b.probe))

    def test_listener_backlog_fact_is_numeric_and_keeps_failure_unchanged(self):
        self.b.tunnel.server = SimpleNamespace(request_queue_size=16)
        self.admit()
        self.b._received(self.event('net::ERR_PROXY_CONNECTION_FAILED', type='Preflight'))
        first = self.b.probe['first_fatal']
        self.assertEqual(first['connection_facts']['guard_listener_backlog'], 16)
        self.assertEqual(self.b.error, 'local_proxy_connection_failed')
        self.b.tunnel.server.request_queue_size = SECRET
        self.assertIsNone(evidence.facts(self.b)['guard_listener_backlog'])
        self.assertNotIn(SECRET, json.dumps(self.b.probe))

    def test_initial_robots_proxy_failure_keeps_zero_connection_facts(self):
        self.admit()
        self.b._requests['session', 'net-1'].update(role='robots', operation='robots')
        self.b.tunnel.connections = 0
        self.b._received(self.event('net::ERR_PROXY_CONNECTION_FAILED', type='Document'))
        first = self.b.probe['first_fatal']
        self.assertEqual(first['code'], 'local_proxy_connection_failed')
        self.assertEqual(first['latest_loading_failure']['role'], 'robots')
        self.assertEqual(first['latest_loading_failure']['browser_failure_code'], 'local_proxy_connection_failed')
        self.assertEqual(first['connection_facts']['guard_connections'], 0)
        self.b._send.assert_not_called()

    def test_existing_policy_stop_and_first_snapshot_survive_later_noise(self):
        self.b._fatal('http_403')
        before = json.loads(json.dumps(self.b.probe['first_fatal']))
        self.b.tunnel._closed = True
        self.b._received(self.event('net::ERR_ABORTED', canceled=True))
        self.b._fatal('network_error')
        self.assertEqual(self.b.error, 'http_403')
        self.assertEqual(self.b.probe['first_fatal'], before)

    def test_optional_cancelled_and_unowned_events_keep_existing_decisions(self):
        self.admit()
        self.b._requests['session', 'net-1']['role'] = 'asset'
        self.b._received(self.event('net::ERR_ABORTED', canceled=True))
        self.assertIsNone(self.b.error)
        self.admit()
        self.b.cancelled.set()
        self.b._received(self.event())
        self.assertIsNone(self.b.error)
        self.b.cancelled.clear()
        self.admit()
        event = self.event(); event['sessionId'] = 'unowned-' + SECRET
        self.b._received(event)
        self.assertIsNone(self.b.error)
        self.assertIn(('session', 'net-1'), self.b._requests)
        self.assertNotIn('first_fatal', self.b.probe)
        self.b._send.assert_not_called()

    def test_history_is_bounded_while_first_failure_stays_available(self):
        self.admit()
        self.b._received(self.event())
        first = json.loads(json.dumps(self.b.probe['first_fatal']))
        for _ in range(56):
            self.b._received(self.event('net::ERR_ABORTED'))
        self.assertEqual(self.b.probe['loading_failure_count'], 57)
        self.assertEqual(len(self.b.probe['loading_failures']), 32)
        self.assertEqual(self.b.probe['loading_failures'][0]['index'], 26)
        self.assertEqual(self.b.probe['first_fatal'], first)

    def test_arbitrary_event_and_guard_strings_are_never_serialized(self):
        self.admit()
        self.b._requests['session', 'net-1'].update(role=SECRET, operation=SECRET,
            url='https://' + SECRET, context={'keyword': SECRET}, status=SECRET)
        self.b.tunnel.last_error = SECRET
        event = self.event(SECRET, type=SECRET, blockedReason=SECRET,
            corsErrorStatus={'corsError': SECRET, 'failedParameter': SECRET},
            headers={'Authorization': SECRET}, url='https://' + SECRET)
        evidence.loading_failure(self.b, event, json.loads(event['message']))
        evidence.fatal_failure(self.b, SECRET)
        data = json.dumps(self.b.probe)
        self.assertNotIn(SECRET, data)
        row = self.b.probe['loading_failures'][0]
        for key in ('browser_error', 'resource_type', 'blocked_reason', 'cors_error', 'role', 'operation'):
            self.assertEqual(row[key], 'unknown')
        self.assertEqual(self.b.probe['first_fatal']['code'], 'unknown')

    def test_cors_and_blocked_reason_are_fixed_values_without_failed_parameter(self):
        event = self.event('net::ERR_FAILED', blockedReason='csp',
            corsErrorStatus={'corsError': 'MissingAllowOriginHeader', 'failedParameter': SECRET})
        self.b._received(event)
        row = self.b.probe['loading_failures'][0]
        self.assertEqual(row['browser_error'], 'ERR_FAILED')
        self.assertEqual(row['cors_error'], 'MissingAllowOriginHeader')
        self.assertEqual(row['blocked_reason'], 'csp')
        self.assertNotIn(SECRET, json.dumps(row))

    def test_diagnostic_fault_cannot_replace_required_transport_failure(self):
        self.admit()
        with patch.object(self.b.tunnel.thread, 'is_alive', side_effect=RuntimeError(SECRET)):
            self.b._received(self.event('net::ERR_PROXY_CONNECTION_FAILED'))
        self.assertEqual(self.b.error, 'local_proxy_connection_failed')
        self.assertTrue(self.b._halted)
        self.assertEqual(self.b._requests, {})
        self.b._send.assert_not_called()
        self.assertNotIn(SECRET, json.dumps(self.b.probe))

    def test_original_controller_exception_propagates_unchanged(self):
        failure = RuntimeError('original controller exception')
        with patch.object(NativeBackend, '_fatal', side_effect=failure):
            with self.assertRaises(RuntimeError) as caught:
                self.b._fatal('http_403')
        self.assertIs(caught.exception, failure)
        self.assertEqual(self.b.probe['first_fatal']['code'], 'http_403')

    def test_search_probe_inherits_the_real_failure_recording_path(self):
        self.b.__class__ = SearchObserver
        self.admit()
        self.b._received(self.event('net::ERR_CONNECTION_RESET'))
        self.assertEqual(self.b.error, 'network_error')
        self.assertEqual(self.b.probe['first_fatal']['latest_loading_failure']['browser_error'], 'ERR_CONNECTION_RESET')
        self.assertEqual(self.b.probe['events']['Network.loadingFailed'], 1)
        self.b._send.assert_not_called()

    def test_missing_and_non_string_failure_categories_never_copy_event_values(self):
        cases = [
            ({}, 'unknown', None),
            ({'blockedReason': [], 'corsErrorStatus': {'corsError': []}}, 'unknown', 'unknown'),
            ({'blockedReason': {'raw': SECRET},
              'corsErrorStatus': {'corsError': {'raw': SECRET}, 'failedParameter': SECRET}},
             'unknown', 'unknown'),
            ({'blockedReason': None, 'corsErrorStatus': []}, 'unknown', None),
        ]
        for index, (params, blocked, cors) in enumerate(cases, 1):
            with self.subTest(index=index):
                event = self.event(**params)
                evidence.loading_failure(self.b, event, json.loads(event['message']))
                self.assertEqual(self.b.probe['loading_failure_count'], index)
                row = self.b.probe['loading_failures'][-1]
                self.assertEqual(row['blocked_reason'], blocked)
                self.assertEqual(row['cors_error'], cors)
                self.assertNotIn(SECRET, json.dumps(self.b.probe))
