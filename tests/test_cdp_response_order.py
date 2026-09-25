"""A command waits for preceding events, not unbounded work after its reply."""
import json
import unittest
from unittest.mock import patch

from test_cdp_connection import Socket
from vibe_job_radar.guided.cdp_connection import CDPConnection
from vibe_job_radar.guided.cdp_page import CDPPage, Frame
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshotChanged


class ResponseOrderTests(unittest.TestCase):
    def setUp(self):
        self.socket=Socket()
        self.connection=CDPConnection(self.socket)
        self.addCleanup(self.connection.close)
        self.session=self.connection.session('page')
        self.seen=[]

    def event(self, name):
        return json.dumps({'sessionId':'page','method':'Fetch.requestPaused','params':{'requestId':name}})

    def configure(self, *, error=False):
        def respond(command):
            if command['method']=='Runtime.evaluate':
                reply={'id':command['id'],'result':{'value':42}}
                if error:reply={'id':command['id'],'error':{'message':'PRIVATE-EXPRESSION'}}
                self.socket.incoming.extend([self.event('before'),json.dumps(reply),
                    self.event('after-1'),self.event('after-2')])
            else:
                self.socket.incoming.append(json.dumps({'id':command['id'],'result':{'ack':True}}))
        self.socket.handle=respond

    def callback(self, event):
        self.seen.append(event['requestId'])
        self.assertEqual(self.session.send('Fetch.continueRequest',event),{'ack':True})

    def test_events_received_after_reply_remain_for_later_fifo_pumping(self):
        self.configure()
        self.session.on('Fetch.requestPaused',self.callback)
        self.assertEqual(self.session.send('Runtime.evaluate'),{'value':42})
        self.assertEqual(self.seen,['before'])
        self.assertEqual(len(self.connection._events),2)
        self.assertGreater(self.connection._event_bytes,0)
        self.connection.pump(0)
        self.connection.pump(0)
        self.assertEqual(self.seen,['before','after-1','after-2'])
        self.assertFalse(self.connection._events)
        self.assertEqual(self.connection._event_bytes,0)
        self.assertFalse(self.connection.pending)
        self.assertFalse(self.connection.responses)
        self.assertFalse(self.connection._response_order)

    def test_later_event_work_cannot_timeout_a_reply_already_received_in_budget(self):
        self.configure()
        clock=[0.0]
        def callback(event):
            self.callback(event)
            clock[0]+=.1 if event['requestId']=='before' else 1.1
        self.session.on('Fetch.requestPaused',callback)
        with patch('vibe_job_radar.guided.cdp_connection.time.monotonic',side_effect=lambda:clock[0]):
            self.assertEqual(self.session.send('Runtime.evaluate',timeout=1),{'value':42})
        self.assertEqual(self.seen,['before'])
        self.assertEqual(clock[0],.1)
        self.assertEqual(len(self.connection._events),2)

    def test_preceding_work_still_uses_the_original_deadline(self):
        clock=[0.0]
        def respond(command):
            if command['method']=='Runtime.evaluate':
                self.socket.incoming.extend([self.event('before-1'),self.event('before-2')])
            self.socket.incoming.append(json.dumps({'id':command['id'],'result':{}}))
        def callback(event):
            self.seen.append(event['requestId'])
            self.session.send('Fetch.continueRequest',event)
            clock[0]=2.0
        self.socket.handle=respond
        self.session.on('Fetch.requestPaused',callback)
        with patch('vibe_job_radar.guided.cdp_connection.time.monotonic',side_effect=lambda:clock[0]):
            with self.assertRaisesRegex(CrawlError,'native_protocol_error'):
                self.session.send('Runtime.evaluate',timeout=1)
        self.assertEqual(self.seen,['before-1'])
        self.assertFalse(self.connection.pending)
        self.assertFalse(self.connection.responses)

    def test_protocol_error_keeps_later_events_and_never_exposes_raw_message(self):
        self.configure(error=True)
        self.session.on('Fetch.requestPaused',self.callback)
        with self.assertRaises(CrawlError) as caught:
            self.session.send('Runtime.evaluate')
        self.assertEqual(str(caught.exception),'native_protocol_error')
        self.assertEqual(self.seen,['before'])
        self.assertEqual(len(self.connection._events),2)
        self.assertFalse(self.connection.responses)
        self.assertFalse(self.connection.pending)

    def test_next_command_observes_preexisting_events_before_its_own_reply(self):
        self.configure()
        self.session.on('Fetch.requestPaused',self.callback)
        self.session.send('Runtime.evaluate')
        self.assertEqual(self.seen,['before'])
        self.assertEqual(self.session.send('Page.getFrameTree'),{'ack':True})
        self.assertEqual(self.seen,['before','after-1','after-2'])
        self.assertFalse(self.connection._events)

    def test_close_discards_later_events_without_dispatching_them(self):
        self.configure()
        self.session.on('Fetch.requestPaused',self.callback)
        self.session.send('Runtime.evaluate')
        self.connection.close()
        self.assertEqual(self.seen,['before'])
        self.assertFalse(self.connection._events)
        self.assertEqual(self.connection._event_bytes,0)
        self.assertFalse(self.connection._response_order)

    def test_later_navigation_invalidates_the_next_dom_read(self):
        page=CDPPage.__new__(CDPPage)
        page.closed=False;page.navigation=0;page.timeout=6000;page.client=self.session
        page.main_frame=Frame(page,'page');page.loader='old';page.callbacks={}
        page._emit=lambda *_:None
        self.session.on('Page.frameNavigated',page._navigated)
        self.session.on('Fetch.requestPaused',self.callback)
        evaluations=0
        def respond(command):
            nonlocal evaluations
            if command['method']=='Runtime.evaluate':
                evaluations+=1
                if evaluations==1:self.socket.incoming.append(self.event('before'))
                self.socket.incoming.append(json.dumps({'id':command['id'],'result':{'result':{'value':42}}}))
                if evaluations==1:self.socket.incoming.append(json.dumps({'sessionId':'page',
                    'method':'Page.frameNavigated','params':{'frame':{'id':'page','url':'https://fixture.test/new','loaderId':'new'}}}))
            else:self.socket.incoming.append(json.dumps({'id':command['id'],'result':{'ack':True}}))
        self.socket.handle=respond
        self.assertEqual(page.evaluate('21*2'),42)
        self.assertEqual(page.navigation,0)
        with self.assertRaises(PageSnapshotChanged):page.evaluate('21*2')
        self.assertEqual(page.navigation,1)
        self.assertEqual(page.url,'https://fixture.test/new')
        self.assertFalse(self.connection._events)
        self.assertFalse(self.connection._response_order)
