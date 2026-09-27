"""Document-only CSP restriction; never relax source CSP/CORS or login gates."""
import copy
import base64
import unittest

from vibe_job_radar.guided.native_documents import (document_response_params,
    continue_document_response, DOCUMENT_SANDBOX, ROBOTS_SANDBOX)
import test_native_acquisition as fixtures


def event(**changes):
    value = {'requestId': 'r', 'resourceType': 'Document',
             'responseStatusCode': 200, 'responseStatusText': 'OK',
             'responseHeaders': [{'name': 'Content-Type', 'value': 'text/html'}]}
    value.update(changes)
    return value


class DocumentPolicyTests(unittest.TestCase):
    def test_append_without_mutating_upstream(self):
        source = event(); before = copy.deepcopy(source)
        result = document_response_params(source, enabled=True)
        self.assertEqual(source, before)
        self.assertEqual(result['responseHeaders'][:-1], source['responseHeaders'])
        self.assertEqual(result['responseHeaders'][-1],
                         {'name': 'Content-Security-Policy', 'value': DOCUMENT_SANDBOX})
        self.assertEqual(result['responseCode'], 200)
        self.assertEqual(result['responsePhrase'], 'OK')
        self.assertNotIn('body', result)

    def test_existing_policies_remain_separate_and_unchanged(self):
        headers = [{'name': 'Content-Security-Policy', 'value': "default-src 'none'; sandbox"},
                   {'name': 'Content-Security-Policy', 'value': 'sandbox allow-popups'},
                   {'name': 'Content-Security-Policy-Report-Only', 'value': "script-src 'none'"}]
        result = document_response_params(event(responseHeaders=headers), enabled=True)
        self.assertEqual(result['responseHeaders'][:-1], headers)
        self.assertEqual(len(result['responseHeaders']), 4)

    def test_cookies_encoding_and_cors_headers_preserved(self):
        headers = [{'name': 'Set-Cookie', 'value': 'fixture_a=1; HttpOnly; Secure'},
                   {'name': 'set-cookie', 'value': 'fixture_b=2; SameSite=Lax'},
                   {'name': 'Content-Encoding', 'value': 'gzip'},
                   {'name': 'Access-Control-Allow-Origin', 'value': 'https://jobs.fixture.test'}]
        self.assertEqual(document_response_params(event(responseHeaders=headers), enabled=True)
                         ['responseHeaders'][:-1], headers)

    def test_cors_and_all_non_document_responses_untouched(self):
        for resource in ('Preflight', 'XHR', 'Fetch', 'Script', 'Stylesheet', 'Image', 'Other'):
            with self.subTest(resource=resource):
                self.assertEqual(document_response_params(event(resourceType=resource), enabled=True),
                                 {'requestId': 'r'})

    def test_non_cors_backend_unchanged(self):
        self.assertEqual(document_response_params(event(), enabled=False), {'requestId': 'r'})

    def test_redirects_not_reconstructed(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status):
                self.assertEqual(document_response_params(event(responseStatusCode=status), enabled=True),
                                 {'requestId': 'r'})

    def test_error_documents_also_sandboxed(self):
        self.assertIn('responseHeaders', document_response_params(event(responseStatusCode=404,
                                  responseStatusText='Not Found'), enabled=True))

    def test_missing_mime_cannot_evade_document_sandbox(self):
        self.assertIn('responseHeaders', document_response_params(event(responseHeaders=[]), enabled=True))

    def test_origin_and_forms_preserved_but_new_windows_not_enabled(self):
        self.assertEqual(DOCUMENT_SANDBOX.split(';')[0].split(),
                         ['sandbox', 'allow-scripts', 'allow-same-origin', 'allow-forms'])
        self.assertIn("frame-src 'none'", DOCUMENT_SANDBOX)
        self.assertNotIn('allow-popups', DOCUMENT_SANDBOX)
        self.assertNotIn('allow-top-navigation', DOCUMENT_SANDBOX)

    def test_browser_network_error_not_converted_into_success(self):
        self.assertEqual(document_response_params(event(responseErrorReason='Failed'), enabled=True),
                         {'requestId': 'r'})


class ControllerDocumentPolicyTests(unittest.TestCase):
    # Reuse setup helpers, not the base class's test collection.
    setUp = fixtures.NativeControllerTests.setUp
    req = fixtures.NativeControllerTests.req
    response = fixtures.NativeControllerTests.response

    def test_robots_error_document_is_inert_even_without_a_cors_contract(self):
        self.b._native_cors = False
        self.b._loading_robots = True
        self.b._robots_url = fixtures.URL + '/robots.txt'
        self.b._paused('session', self.req('/robots.txt', 'GET', 'Document'))
        self.b._paused('session', self.response(404, {'content-type': 'text/html'},
            path='/robots.txt', method='GET', kind='Document'))
        args = self.b._send.call_args.args
        self.assertEqual(args[1], 'Fetch.getResponseBody')
        raw = '<html><script>fetch("/apply")</script>Not Found</html>'
        args[3]({'body': raw})
        delivery = self.b._send.call_args.args
        self.assertEqual(delivery[1], 'Fetch.fulfillRequest')
        self.assertEqual(delivery[2]['responseCode'], 404)
        self.assertEqual(base64.b64decode(delivery[2]['body']), raw.encode())
        self.assertIn({'name': 'Content-Security-Policy', 'value': ROBOTS_SANDBOX},
                      delivery[2]['responseHeaders'])
        self.assertIsNone(self.b.error)

    def test_cors_document_runs_through_existing_response_validation(self):
        self.b._native_cors = True
        self.b._paused('session', self.req('/search', 'GET', 'Document'))
        self.b._paused('session', self.response(200, {'content-type': 'text/html'},
                                             path='/search', method='GET', kind='Document'))
        args = self.b._send.call_args.args
        self.assertEqual(args[1], 'Fetch.getResponseBody')
        self.assertEqual(args[2], {'requestId': 'fetch-1'})
        self.assertIsNone(self.b.error)

    def test_source_denial_still_aborts_not_sandboxed_success(self):
        self.b._native_cors = True
        self.b._paused('session', self.req('/search', 'GET', 'Document'))
        self.b._paused('session', self.response(403, path='/search', method='GET', kind='Document'))
        self.assertEqual(self.b.error, 'http_403')
        self.assertEqual(self.b._send.call_args.args[1], 'Fetch.failRequest')

    def test_protocol_failure_must_not_fall_back_to_unprotected_response(self):
        self.b._native_cors = True
        self.b._paused('session', self.req('/search', 'GET', 'Document'))
        calls = []
        def send(session, method, params, callback=None):
            calls.append(method)
            if method == 'Fetch.getResponseBody':
                raise RuntimeError('unsupported document policy')
        self.b._send.side_effect = send
        self.b._paused('session', self.response(200, path='/search', method='GET', kind='Document'))
        self.assertEqual(self.b.error, 'native_protocol_error')
        self.assertEqual(calls, ['Fetch.getResponseBody', 'Fetch.failRequest'])


class NativeDocumentDeliveryTests(unittest.TestCase):
    setUp = fixtures.NativeControllerTests.setUp

    def prepare(self, **changes):
        self.b._native_cors = True
        e = event(**changes)
        continue_document_response(self.b, 'session', e)
        args = self.b._send.call_args.args
        self.assertEqual(args[1:3], ('Fetch.getResponseBody', {'requestId': 'r'}))
        return e, args[3]

    def test_native_bytes_delivered_unchanged_with_all_publisher_policies(self):
        headers = [{'name': 'Content-Type', 'value': 'text/html; charset=gbk'},
                   {'name': 'Content-Encoding', 'value': 'gzip'},
                   {'name': 'Content-Length', 'value': '99'},
                   {'name': 'Transfer-Encoding', 'value': 'chunked'},
                   {'name': 'Set-Cookie', 'value': 'fixture_a=1; HttpOnly; Secure'},
                   {'name': 'Set-Cookie', 'value': 'fixture_b=2; Secure'},
                   {'name': 'Content-Security-Policy', 'value': "object-src 'none'"}]
        e, deliver = self.prepare(responseHeaders=headers)
        original = copy.deepcopy(e)
        raw = '<h1>合成页面</h1>'.encode('gbk')
        deliver({'body': base64.b64encode(raw).decode(), 'base64Encoded': True})
        args = self.b._send.call_args.args
        self.assertEqual(args[1], 'Fetch.fulfillRequest')
        self.assertEqual(base64.b64decode(args[2]['body']), raw)
        self.assertEqual(args[2]['responseHeaders'], [headers[0], *headers[4:],
            {'name':'Content-Security-Policy', 'value':DOCUMENT_SANDBOX},
            {'name':'Content-Length', 'value':str(len(raw))}])
        self.assertEqual(e, original)

    def test_plain_text_protocol_result_is_utf8_encoded(self):
        _, deliver = self.prepare()
        deliver({'body': '合成页面', 'base64Encoded': False})
        self.assertEqual(base64.b64decode(self.b._send.call_args.args[2]['body']), '合成页面'.encode())

    def test_retired_target_cannot_receive_late_body(self):
        _, deliver = self.prepare()
        self.b._sessions.clear(); self.b._send.reset_mock()
        deliver({'body': 'irrelevant'})
        self.b._send.assert_not_called()

    def test_cancel_or_policy_change_never_delivers_content(self):
        from vibe_job_radar.guided.contracts import CrawlError
        for case in ('cancel', 'policy', 'halt'):
            with self.subTest(case=case):
                self.b.cancelled.clear(); self.b._halted=False; self.b.policy_check=lambda:True
                _, deliver = self.prepare()
                self.b._send.reset_mock()
                if case == 'cancel': self.b.cancelled.set()
                elif case == 'policy': self.b.policy_check=lambda:False
                else: self.b._halted=True; self.b.error='http_403'
                with self.assertRaises(CrawlError): deliver({'body': 'unusable'})
                self.b._send.assert_not_called()

    def test_invalid_or_oversized_body_is_not_fulfilled(self):
        from vibe_job_radar.guided.contracts import CrawlError
        for response in ({'body': None}, {'body':'*notbase64*','base64Encoded':True},
                         {'body':'x'*5_000_001}, {'body':'A'*6_700_001}):
            with self.subTest(kind=type(response['body']).__name__):
                _, deliver = self.prepare(); self.b._send.reset_mock()
                with self.assertRaises(CrawlError): deliver(response)
                self.b._send.assert_not_called()

    def test_bodyless_status_remains_native(self):
        self.b._native_cors=True
        for status in (204,205):
            continue_document_response(self.b, 'session', event(responseStatusCode=status))
            self.assertEqual(self.b._send.call_args.args[1:],
                             ('Fetch.continueResponse', {'requestId':'r'}))
