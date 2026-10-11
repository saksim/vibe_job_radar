"""Authored protocol streams test correlation, loss reporting and non-disclosure."""
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from portable_blank_protocol import BlankNavigationTrace, EVENT_LIMIT, LINE_LIMIT, bounded_lines
from vibe_job_radar.guided.browser_health import OFFLINE_CHECK_URL


def line(message, *, sent=False):
    direction = 'SEND \u25ba' if sent else '\u25c0 RECV'
    return ('2026-10-11T00:00:00Z pw:protocol ' + direction + ' ' +
            json.dumps(message) + ' +2ms\n').encode('utf8')


def navigate(session='PRIVATE_SESSION', frame='PRIVATE_FRAME', command=41, url=OFFLINE_CHECK_URL):
    return line({'id': command, 'sessionId': session, 'method': 'Page.navigate',
                 'params': {'frameId': frame, 'url': url}}, sent=True)


def event(method, params, *, session='PRIVATE_SESSION'):
    return line({'sessionId': session, 'method': method, 'params': params})


class BlankProtocolTests(unittest.TestCase):
    def test_same_document_events_bind_even_when_reply_arrives_after_commit(self):
        trace = BlankNavigationTrace()
        trace.feed(navigate())
        trace.feed(event('Page.frameNavigated', {'frame': {
            'id': 'PRIVATE_FRAME', 'url': OFFLINE_CHECK_URL, 'loaderId': 'PRIVATE_LOADER'}}))
        trace.feed(event('Page.lifecycleEvent', {'frameId': 'PRIVATE_FRAME',
            'loaderId': 'PRIVATE_LOADER', 'name': 'DOMContentLoaded'}))
        trace.feed(line({'sessionId': 'PRIVATE_SESSION', 'id': 41, 'result': {
            'frameId': 'PRIVATE_FRAME', 'loaderId': 'PRIVATE_LOADER'}}))
        trace.finish()
        result = trace.snapshot()
        self.assertEqual([x['event'] for x in result['events']],
                         ['navigate_sent', 'inline_committed', 'lifecycle_dom', 'navigate_returned'])
        self.assertEqual(result['events'][2]['loader_relation'], 'navigation')
        self.assertTrue(result['stream_ended'])
        self.assertFalse(result['historical_cause_confirmed'])
        self.assertTrue(all(type(x['received_ms']) is int and x['received_ms'] >= 0 for x in result['events']))
        value = json.dumps(result)
        for private in ('PRIVATE_', 'data:text', 'Vibe Radar browser check', 'http'):
            self.assertNotIn(private, value)

    def test_foreign_frames_and_sessions_do_not_become_target_lifecycle(self):
        trace = BlankNavigationTrace(); trace.feed(navigate())
        for session, frame in [('FOREIGN', 'PRIVATE_FRAME'), ('PRIVATE_SESSION', 'FOREIGN')]:
            trace.feed(event('Page.lifecycleEvent', {'frameId': frame, 'name': 'DOMContentLoaded',
                'loaderId': 'PRIVATE_OLD'}, session=session))
        trace.feed(event('Page.lifecycleEvent', {'frameId': 'PRIVATE_FRAME',
            'name': 'DOMContentLoaded', 'loaderId': 'PRIVATE_OLD'}))
        trace.feed(line({'sessionId': 'PRIVATE_SESSION', 'id': 41, 'result': {'loaderId': 'PRIVATE_NEW'}}))
        self.assertEqual([x['event'] for x in trace.snapshot()['events']],
                         ['navigate_sent', 'lifecycle_dom', 'navigate_returned'])
        self.assertEqual(trace.snapshot()['events'][1]['loader_relation'], 'other')

    def test_network_documents_never_arm_observer_and_later_inline_never_replaces_first(self):
        trace = BlankNavigationTrace()
        trace.feed(navigate(url='https://PRIVATE/secret?token=PRIVATE'))
        trace.feed(event('Page.domContentEventFired', {'timestamp': 99}))
        self.assertFalse(trace.snapshot()['navigation_observed'])
        trace.feed(navigate())
        trace.feed(navigate(session='SECOND', frame='SECOND', command=42))
        trace.feed(event('Page.domContentEventFired', {}, session='SECOND'))
        self.assertEqual([x['event'] for x in trace.snapshot()['events']],
                         ['navigate_sent', 'additional_inline_navigation'])

    def test_navigation_error_text_and_arbitrary_context_payloads_are_not_retained(self):
        trace = BlankNavigationTrace(); trace.feed(navigate())
        trace.feed(event('Runtime.executionContextCreated', {'context': {
            'name': 'PRIVATE', 'origin': 'https://PRIVATE', 'auxData': {
                'frameId': 'PRIVATE_FRAME', 'isDefault': True}}}))
        trace.feed(line({'sessionId': 'PRIVATE_SESSION', 'id': 41,
                        'error': {'message': 'password=PRIVATE'}}))
        trace.feed(event('Runtime.consoleAPICalled', {'args': [{'value': 'PRIVATE'}]}))
        result = trace.snapshot()
        self.assertEqual([x['event'] for x in result['events']],
                         ['navigate_sent', 'main_context_created', 'navigate_error'])
        self.assertNotIn('PRIVATE', json.dumps(result))
        self.assertEqual(result['events'][-1]['loader_relation'], 'unknown')

    def test_malformed_protocol_and_changed_format_are_observations_not_exceptions(self):
        trace = BlankNavigationTrace()
        trace.feed(b'pw:protocol SEND \xe2\x96\xba {bad PRIVATE}\n')
        trace.feed(navigate(frame=None))
        trace.feed(b'pw:protocol UNKNOWN {"private":"PRIVATE"}\n')
        trace.feed(b'ordinary PRIVATE output\n')
        trace.feed(navigate())
        trace.feed(event('Page.lifecycleEvent', None))
        result = trace.snapshot()
        self.assertEqual(result['malformed_lines'], 3)
        self.assertTrue(result['navigation_observed'])
        self.assertNotIn('PRIVATE', json.dumps(result))

    def test_overlong_line_is_dropped_whole_before_next_valid_record(self):
        stream = io.BytesIO(b'PRIVATE' * LINE_LIMIT + navigate() + navigate())
        records = list(bounded_lines(stream))
        self.assertEqual(records, [None, navigate()])
        trace = BlankNavigationTrace()
        for raw in records: trace.feed(raw)
        self.assertTrue(trace.snapshot()['navigation_observed'])
        self.assertEqual(trace.snapshot()['overlong_lines'], 1)
        self.assertNotIn('PRIVATE', json.dumps(trace.snapshot()))

    def test_event_budget_and_incomplete_reader_are_explicit(self):
        trace = BlankNavigationTrace(); trace.feed(navigate())
        for _ in range(EVENT_LIMIT + 5):
            trace.feed(event('Page.domContentEventFired', {}))
        result = trace.snapshot()
        self.assertEqual(len(result['events']), EVENT_LIMIT)
        self.assertEqual(result['events_dropped'], 6)
        self.assertFalse(result['stream_ended'])
        trace.finish(reader_failed=True)
        self.assertFalse(trace.snapshot()['stream_ended'])
        self.assertTrue(trace.snapshot()['reader_failed'])

    def test_cleanup_has_own_event_and_cannot_be_a_crash(self):
        trace = BlankNavigationTrace(); trace.feed(navigate())
        trace.feed(line({'method': 'Target.disposeBrowserContext', 'id': 50,
                         'params': {'browserContextId': 'PRIVATE'}}, sent=True))
        self.assertEqual([x['event'] for x in trace.snapshot()['events']],
                         ['navigate_sent', 'cleanup_command'])
        result = trace.snapshot()
        result['events'].clear()
        self.assertEqual(len(trace.snapshot()['events']), 2)

    def test_owned_application_alone_gets_debug_and_reader_preserves_entry(self):
        from verify_windows_portable import RunningApp
        import subprocess
        entry = b'http://127.0.0.1:12345/#token=PRIVATE_TEST_TOKEN\n'
        child = MagicMock(); child.stdout = io.BytesIO(entry + navigate())
        child.poll.return_value = 0
        original = {'PATH': 'PRIVATE_PATH'}
        trace = BlankNavigationTrace()
        with patch.object(subprocess, 'CREATE_NO_WINDOW', 0, create=True), patch(
                'verify_windows_portable.subprocess.Popen', return_value=child) as launch:
            app = RunningApp('ARTIFICIAL_EXE', 'ARTIFICIAL_WS', 'ARTIFICIAL_CWD', original,
                             blank_trace=trace)
            self.assertEqual(app.port, 12345)
            self.assertEqual(launch.call_args.kwargs['env']['DEBUG'], 'pw:protocol')
            self.assertEqual(launch.call_args.kwargs['env']['DEBUG_COLORS'], '0')
            self.assertNotIn('PWDEBUG', launch.call_args.kwargs['env'])
            app.close()
        self.assertEqual(original, {'PATH': 'PRIVATE_PATH'})
        self.assertTrue(trace.snapshot()['stream_ended'])
        self.assertTrue(trace.snapshot()['navigation_observed'])
        self.assertNotIn('PRIVATE', json.dumps(trace.snapshot()))

    def test_other_application_processes_do_not_enable_diagnostics(self):
        from verify_windows_portable import RunningApp
        import subprocess
        child = MagicMock(); child.stdout = io.BytesIO(b'http://127.0.0.1:12345/#token=PRIVATE\n')
        child.poll.return_value = 0
        original = {'PATH': 'PRIVATE_PATH'}
        with patch.object(subprocess, 'CREATE_NO_WINDOW', 0, create=True), patch(
                'verify_windows_portable.subprocess.Popen', return_value=child) as launch:
            app = RunningApp('ARTIFICIAL_EXE', 'ARTIFICIAL_WS', 'ARTIFICIAL_CWD', original)
            self.assertIs(launch.call_args.kwargs['env'], original)
            app.close()


if __name__ == '__main__':
    unittest.main()
