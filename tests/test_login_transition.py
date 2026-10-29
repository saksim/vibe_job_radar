"""No accounts: prevent resubmitting while the owned login callback is pending."""
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from test_login_landing import ready_backend, event, FLAG, LOGIN, SMS, MAIN, ADAPTER
from vibe_job_radar.guided.login_landing import observe_post_login, cancel_transition_request, peek, offer
from vibe_job_radar.guided.manual_login_progress import ManualLoginProgress
from vibe_job_radar.guided.native_pacing import NativeRequestPacer, PausedRequest
from vibe_job_radar.guided.rate import RateLimit
from vibe_job_radar.guided.diagnostic_trace import DiagnosticTrace


def arm(backend, *, session='owned', change=None):
    e = event(FLAG, method='OPTIONS', resource='XHR')
    e['request']['headers'] = {'Origin':'https://www.liepin.com',
                              'Access-Control-Request-Method':'GET',
                              'Access-Control-Request-Headers':'x-client-type'}
    if change: change(e)
    rule = backend.contract.match(FLAG, 'OPTIONS', 'XHR', authentication=True,
                                  headers=e['request']['headers'])
    rule.validate_headers('OPTIONS', e['request']['headers'])
    observe_post_login(backend, session, e, rule)


def record(url=LOGIN, method='POST'):
    return {'url':url, 'method':method, 'resource':'xhr', 'role':'login',
            'operation':'liepin_sms_qr_www', 'login_frame':'main', 'epoch':1}


class LoginTransitionTests(unittest.TestCase):
    def test_postprocessing_is_feedback_only_and_does_not_offer_a_return(self):
        b = ready_backend(); arm(b)
        self.assertEqual(b._manual_login_progress.snapshot()['stage'], 'post_login_pending')
        self.assertIsNone(peek(b))
        b._send.assert_not_called(); b.wire.reserve.assert_not_called()

    def test_exact_duplicates_are_cancelled_without_consuming_the_original_response(self):
        for url in (LOGIN, SMS):
            for method in ('OPTIONS', 'POST'):
                b = ready_backend(); arm(b)
                p = b._manual_login_progress
                self.assertTrue(cancel_transition_request(b, 'owned', 'duplicate', record(url, method)))
                self.assertEqual(b._send.call_args.args[2]['errorReason'], 'Aborted')
                self.assertEqual((p.submits, p.sends, p.duplicate_cancellations), (1, 0, 1))
                self.assertEqual(p.last_login_response, p.sequence)
                self.assertIsNone(b.error)
                self.assertIsNone(peek(b))
                self.assertTrue(offer(b, 'owned', event()))
                self.assertIsNotNone(peek(b))
                b.wire.reserve.assert_not_called()

    def test_no_callback_or_changed_owner_cannot_suppress_a_request(self):
        for change in ('no_callback', 'no_reply', 'new_submit', 'rejection', 'cancel', 'expiry',
                       'policy', 'error', 'mode', 'tracker', 'epoch', 'document', 'frame', 'session'):
            b = ready_backend(); arm(b); r = record(); session = 'owned'
            p = b._manual_login_progress
            if change == 'no_callback': del b._login_transition
            elif change in ('no_reply', 'rejection'): p.last_login_response = None
            elif change == 'new_submit': p.requested('next', LOGIN, 'POST')
            elif change == 'cancel': b.cancelled.set()
            elif change == 'expiry': p.clock = lambda: 611
            elif change == 'policy': b.policy_check = lambda: False
            elif change == 'error': b.error = 'http_403'
            elif change == 'mode': b.auth_mode = False
            elif change == 'tracker': b._manual_login_progress = ManualLoginProgress()
            elif change == 'epoch': b._epoch += 1
            elif change == 'document':
                identity = b.document_identity.return_value
                b.document_identity.return_value = (*identity[:3], replace(identity[-1], sequence=2))
            elif change == 'frame': r['login_frame'] = 'child'
            else: session = 'other'
            with self.subTest(change=change):
                self.assertFalse(cancel_transition_request(b, session, 'r', r))
                b._send.assert_not_called()

    def test_other_operations_keep_their_normal_policy(self):
        for update in ({'url':FLAG}, {'url':LOGIN+'?private=x'}, {'method':'GET'},
                       {'resource':'document'}, {'operation':'liepin_sms_qr_c'},
                       {'role':'business'}, {'url':LOGIN.replace('tel-sms-login','qr.ack')}):
            b = ready_backend(); arm(b)
            self.assertFalse(cancel_transition_request(b, 'owned', 'r', {**record(), **update}))
            b._send.assert_not_called()

    def test_callback_requires_current_reply_and_exact_owned_main_document(self):
        for change in ('no_reply', 'frame', 'session', 'path', 'query', 'method', 'resource', 'document'):
            b = ready_backend(); session = 'owned'; update = None
            if change == 'no_reply': b._manual_login_progress.last_login_response = None
            elif change == 'frame': update = lambda e: e.update(frameId='child')
            elif change == 'session': session = 'foreign'
            elif change == 'path': update = lambda e: e['request'].update(url=FLAG+'/other')
            elif change == 'query': update = lambda e: e['request'].update(url=FLAG+'?private=x')
            elif change == 'method': update = lambda e: e['request'].update(method='POST')
            elif change == 'resource': update = lambda e: e.update(resourceType='Document')
            else:
                identity = b.document_identity.return_value
                b.document_identity.return_value = (*identity[:3], replace(identity[-1], url=ADAPTER.search_base))
            arm(b, session=session, change=update)
            self.assertNotIn('_login_transition', b.__dict__)

    def test_already_queued_duplicate_is_retired_before_quota_or_nested_pump(self):
        b = ready_backend()
        b._requests = {}; b._sessions = {'owned':'target'}
        b.browser = SimpleNamespace(connection=Mock())
        b._admit_request = Mock(); b._continue_request = Mock()
        b.wire.max_inline_wait = 10
        b.wire.reserve_request_nowait.side_effect = RateLimit(.5)
        b._diagnostics = DiagnosticTrace('0'*32, 'liepin')
        pacer = NativeRequestPacer(b)
        item = PausedRequest('owned', 'fetch', ('owned','network'),
            'https://api-passport.liepin.com', record(), False, True)
        pacer.submit(item)
        self.assertEqual(len(pacer.queue), 1)
        arm(b)
        b._send.side_effect = lambda *args: self.assertFalse(pacer.queue)
        pacer.pump()
        self.assertFalse(pacer.queue); self.assertEqual(pacer.bytes, 0)
        b.wire.reserve_request_nowait.assert_called_once()
        b._continue_request.assert_not_called()
        self.assertIsNone(b.error)
        self.assertTrue(any(e['code']=='login_transition_duplicate_cancelled'
                            for e in b._diagnostics.snapshot()['events']))


if __name__ == '__main__':
    unittest.main()
