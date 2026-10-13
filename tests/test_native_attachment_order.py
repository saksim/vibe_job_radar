"""Debugger ownership ordering, with no network or real platform requests."""
from unittest import TestCase
from unittest.mock import Mock

from vibe_job_radar.guided.native_browser import NativeBackend
import test_native_acquisition as fixture


class NativeAttachmentOrderTests(TestCase):
    def make_backend(self, reentrant=True):
        b = NativeBackend.__new__(NativeBackend)
        b._closing = False
        b.cancelled = Mock()
        b._sessions = {}
        b._adopting = set()
        b._page_creation = 1
        b._fatal = Mock()
        events = []
        info = {'type': 'page', 'targetId': 'owned-page'}
        def send(method, params):
            events.append(method)
            if method == 'Target.attachToTarget':
                if reentrant:
                    b._attached({'targetInfo': info, 'sessionId': 'legacy'})
                return {'sessionId': 'legacy'}
            return {}
        b._cdp = Mock()
        b._cdp.send.side_effect = send
        def install(session, target):
            events.append('install-controls')
            b._sessions[session] = target['targetId']
        b._install_target = Mock(side_effect=install)
        return b, events, info

    def test_reentrant_attachment_does_not_install_before_automatic_detach(self):
        b, events, info = self.make_backend()
        b._attached({'targetInfo': info, 'sessionId': 'automatic'})
        self.assertEqual(events, ['Target.attachToTarget', 'Target.detachFromTarget', 'install-controls'])
        b._install_target.assert_called_once_with('legacy', info)
        b._fatal.assert_not_called()

    def test_deferred_legacy_event_does_not_adopt_the_target_twice(self):
        b, events, info = self.make_backend(reentrant=False)
        b._attached({'targetInfo': info, 'sessionId': 'automatic'})
        b._attached({'targetInfo': info, 'sessionId': 'legacy'})
        self.assertEqual(events.count('Target.attachToTarget'), 1)
        b._install_target.assert_called_once_with('legacy', info)

    def test_failed_attach_does_not_resume_the_target(self):
        b, events, info = self.make_backend()
        def send(method, params):
            events.append(method)
            if method == 'Target.attachToTarget':
                raise RuntimeError('fixture protocol failure')
            return {}
        b._cdp.send.side_effect = send
        b._attached({'targetInfo': info, 'sessionId': 'automatic'})
        b._install_target.assert_not_called()
        b._fatal.assert_called_once_with('native_protocol_error')
        self.assertEqual(events, ['Target.attachToTarget', 'Target.closeTarget'])
        self.assertEqual(b._adopting, set())

    def test_failed_detach_never_installs_or_releases_unverified_controls(self):
        b, events, info = self.make_backend(reentrant=False)
        def send(method, params):
            events.append(method)
            if method == 'Target.attachToTarget':
                return {'sessionId': 'legacy'}
            if method == 'Target.detachFromTarget':
                raise RuntimeError('fixture detach failure')
            return {}
        b._cdp.send.side_effect = send
        b._attached({'targetInfo': info, 'sessionId': 'automatic'})
        b._install_target.assert_not_called()
        b._fatal.assert_called_once_with('native_protocol_error')
        self.assertIn('Target.closeTarget', events)

    def test_unsupported_target_is_closed_without_any_credentials(self):
        helper = fixture.NativeControllerTests()
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        b = helper.b
        b._cdp.send.return_value = {'sessionId': 'quarantine'}
        b._install_target = Mock()
        b._attached({'targetInfo': {'type': 'worker', 'targetId': 'worker'}, 'sessionId': 'worker-session'})
        b._cdp.send.assert_called_once_with('Target.attachToTarget', {
            'targetId': 'worker', 'flatten': False})
        b._send.assert_called_once_with('quarantine', 'Fetch.enable', {
            'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}],
            'handleAuthRequests': True})
        b._install_target.assert_not_called()
        self.assertTrue(b.cancelled.is_set())
        self.assertTrue(b._halted)
        b._drain_rejected_pages()
        self.assertEqual([call.args[0] for call in b._cdp.send.call_args_list],
                         ['Target.attachToTarget', 'Target.closeTarget'])
        self.assertNotIn('quarantine', b._sessions)
