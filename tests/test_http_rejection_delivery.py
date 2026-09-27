"""Refusals survive unread POST data; framing/authentication remain unchanged."""
from email.message import Message
import http.client
import io
import json
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.workbench import Handler, LocalServer, MAX_BODY
from vibe_job_radar.workspace import Workspace


class DiscardBoundsTests(unittest.TestCase):
    def handler(self, lengths=('8',), transfer=None, body=b'12345678NEXT'):
        handler = object.__new__(Handler); handler.headers = Message()
        for length in lengths: handler.headers['Content-Length'] = length
        if transfer: handler.headers['Transfer-Encoding'] = transfer
        handler.rfile = io.BytesIO(body); handler.connection = Mock()
        handler.connection.gettimeout.return_value = 15
        return handler

    def test_discards_only_one_declared_body_not_a_following_request(self):
        handler = self.handler(); handler._discard_rejected_body()
        self.assertEqual(handler.rfile.read(), b'NEXT')
        handler.connection.settimeout.assert_called_with(15)

    def test_ambiguous_transfer_or_large_frame_is_not_consumed(self):
        for lengths, transfer in [((),None), (('8','8'),None), (('8','9'),None),
                (('-1',),None), (('eight',),None), ((str(MAX_BODY+1),),None), (('8',),'chunked')]:
            handler = self.handler(lengths, transfer)
            handler._discard_rejected_body()
            self.assertEqual(handler.rfile.tell(), 0)
            handler.connection.settimeout.assert_not_called()

    def test_already_read_invalid_json_is_not_read_twice(self):
        handler = self.handler(); handler._body_consumed = True
        handler._discard_rejected_body(); self.assertEqual(handler.rfile.tell(), 0)

    def test_absolute_deadline_does_not_extend_for_each_small_chunk(self):
        handler = self.handler(('100',)); handler.rfile = Mock()
        handler.rfile.read1.return_value = b'x'
        with patch('vibe_job_radar.workbench.time.monotonic', side_effect=[0, .01, .11, .21]):
            handler._discard_rejected_body()
        self.assertEqual(handler.rfile.read1.call_count, 2)
        handler.connection.settimeout.assert_called_with(15)

    def test_socket_timeout_is_bounded_and_original_timeout_restored(self):
        handler = self.handler(); handler.rfile = Mock()
        handler.rfile.read1.side_effect = TimeoutError()
        handler._discard_rejected_body()
        handler.connection.settimeout.assert_called_with(15)


class RefusalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Workspace(self.tmp.name); self.server = LocalServer(self.workspace, public_client=None)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join(3); self.tmp.cleanup()

    def test_repeated_unauthorized_and_cross_origin_posts_receive_403_without_dispatch(self):
        body = json.dumps({'mode':'fake_ip_doh','consent':True,'revision':0}).encode()
        with patch.object(self.workspace, 'network_preferences', side_effect=AssertionError('must not dispatch')):
            for index in range(40):
                conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
                try:
                    headers = {'Content-Type':'application/json'}
                    if index % 2:
                        headers.update({'X-Radar-Token':self.server.token, 'Origin':'https://outside.test'})
                    conn.request('POST','/api/network/preferences',body,headers)
                    response = conn.getresponse()
                    self.assertEqual(response.status, 403)
                    self.assertEqual(response.getheader('Connection'), 'close')
                    self.assertIn('error', json.loads(response.read()))
                finally: conn.close()
        self.assertFalse((self.workspace.root/'network-preferences.json').exists())

    def test_authorized_body_still_executes_once_and_returns_json(self):
        conn = http.client.HTTPConnection(*self.server.server_address, timeout=3)
        try:
            conn.request('POST','/api/network/preferences',json.dumps({'mode':'system','consent':False,'revision':0}),
                {'Content-Type':'application/json','X-Radar-Token':self.server.token})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertEqual(json.loads(response.read())['revision'], 1)
        finally: conn.close()
