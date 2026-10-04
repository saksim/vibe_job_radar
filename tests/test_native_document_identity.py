"""Owned CDP document identity works without private SDK/page attributes."""
import json
import unittest
from unittest.mock import Mock

import test_native_acquisition as fixtures
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshotChanged

BASE='https://fixture.test/zhaopin/'
QUERY=BASE+'?key=private-fixture-keyword'


class PlainPage:
    def __init__(self,url):
        self.value=url;self.closed=False;self.on_url=None
    @property
    def url(self):
        callback,self.on_url=self.on_url,None
        if callback:callback()
        return self.value
    def is_closed(self):return self.closed


class NativeDocumentIdentityTests(unittest.TestCase):
    def setUp(self):
        helper=fixtures.NativeControllerTests();helper.setUp();self.addCleanup(helper.doCleanups)
        self.backend=helper.b;self.page=PlainPage(BASE);self.client=Mock()
        self.backend.page=self.page;self.backend._bound_pages={self.page:'session'}
        self.backend._page_sessions={'session':self.client}
    def event(self,method,data,session='session'):
        self.backend._received({'sessionId':session,'message':json.dumps({'method':method,'params':data})})
    def commit(self,url=BASE):
        self.page.value=url;self.event('Page.frameNavigated',{'frame':{'id':'frame','url':url}})
    def history(self,url=QUERY):
        self.page.value=url;self.event('Page.navigatedWithinDocument',{'frameId':'frame','url':url})
    def test_owned_page_without_committed_event_is_not_authorized_by_page_attributes(self):
        self.page.document_url=BASE;self.page.navigation=1;self.page.ident='frame'
        with self.assertRaises(PageSnapshotChanged):self.backend.document_identity()
    def test_history_uses_committed_document_with_an_sdk_shaped_page(self):
        self.commit();before=self.backend.document_identity();self.history();after=self.backend.document_identity()
        self.assertFalse(hasattr(self.page,'navigation'));self.assertFalse(hasattr(self.page,'ident'))
        self.assertEqual(after[3].document_url,BASE);self.assertEqual(after[3].url,QUERY)
        self.assertEqual(after[3].sequence,before[3].sequence+1);self.assertNotEqual(before,after)
    def test_real_navigation_even_to_same_url_replaces_document_identity(self):
        self.commit();self.history();before=self.backend.document_identity();self.commit(QUERY);after=self.backend.document_identity()
        self.assertEqual(after[3].document_url,QUERY);self.assertGreater(after[3].sequence,before[3].sequence)
        self.assertNotEqual(before,after)
    def test_child_frame_and_unmatched_history_cannot_change_main_identity(self):
        self.commit();before=self.backend.document_identity()
        self.event('Page.frameNavigated',{'frame':{'id':'child','parentId':'frame','url':'https://other.test/'}})
        self.event('Page.navigatedWithinDocument',{'frameId':'child','url':'https://other.test/?a=1'})
        self.assertEqual(self.backend.document_identity(),before)
    def test_unknown_session_event_does_not_create_document_identity(self):
        self.event('Page.frameNavigated',{'frame':{'id':'frame','url':BASE}},session='other')
        with self.assertRaises(PageSnapshotChanged):self.backend.document_identity()
    def test_retiring_page_clears_identity_and_late_events_cannot_restore_it(self):
        self.commit();self.backend._detached({'sessionId':'session'})
        self.event('Page.frameNavigated',{'frame':{'id':'frame','url':BASE}})
        self.assertNotIn('session',self.backend.__dict__.get('_page_documents',{}))
        with self.assertRaises(PageSnapshotChanged):self.backend.document_identity()
    def test_unbound_target_or_client_cannot_reuse_committed_identity(self):
        self.commit()
        for part in ('bound','client','target'):
            with self.subTest(part=part):
                if part=='bound':self.backend._bound_pages.clear()
                elif part=='client':self.backend._page_sessions.clear()
                else:self.backend._sessions['session']='another-target'
                with self.assertRaises(PageSnapshotChanged):self.backend.document_identity()
                self.backend._bound_pages={self.page:'session'};self.backend._page_sessions={'session':self.client};self.backend._sessions['session']='frame'
    def test_url_changed_without_owned_event_is_rejected(self):
        self.commit();self.page.value=QUERY
        with self.assertRaises(PageSnapshotChanged):self.backend.document_identity()
    def test_same_url_navigation_during_url_read_is_rejected(self):
        self.commit();self.page.on_url=lambda:self.commit()
        with self.assertRaises(PageSnapshotChanged):self.backend.document_identity()
    def test_page_or_client_replaced_during_url_read_is_rejected(self):
        self.commit()
        self.page.on_url=lambda:setattr(self.backend,'page',PlainPage(BASE))
        with self.assertRaises(PageSnapshotChanged):self.backend.document_identity()
        self.backend.page=self.page;self.page.on_url=lambda:self.backend._page_sessions.update(session=Mock())
        with self.assertRaises(PageSnapshotChanged):self.backend.document_identity()
    def test_closed_cancelled_and_prior_native_refusal_keep_original_failures(self):
        self.commit();self.page.closed=True
        with self.assertRaisesRegex(CrawlError,'browser_closed'):self.backend.document_identity()
        self.page.closed=False;self.backend.cancelled.set()
        with self.assertRaisesRegex(CrawlError,'paused'):self.backend.document_identity()
        self.backend.cancelled.clear();self.backend.error='http_403';self.page.closed=True
        with self.assertRaisesRegex(CrawlError,'http_403'):self.backend.document_identity()
    def test_identity_repr_does_not_include_private_query_values(self):
        self.commit();self.history();self.assertNotIn('private-fixture-keyword',repr(self.backend.document_identity()))
    def test_owned_public_session_registers_navigation_events_and_enables_page_domain(self):
        b=self.backend;b._page_creation=1;page,client,b.context=Mock(url='about:blank'),Mock(),Mock()
        b.context.new_cdp_session.return_value=client;client.send.return_value={'targetInfo':{'targetId':'frame'}}
        b._bound_pages.clear();b._page_sessions.clear();b._native_user_agent='Actual/1.0 VibeJobRadar/0.1'
        b._bind_page(page)
        methods=[c.args[0] for c in client.on.call_args_list]
        self.assertIn('Page.frameNavigated',methods);self.assertIn('Page.navigatedWithinDocument',methods)
        calls=b._send.call_args_list;page_call=next(i for i,c in enumerate(calls) if c.args[1]=='Page.enable')
        resume=next(i for i,c in enumerate(calls) if c.args[1]=='Runtime.runIfWaitingForDebugger')
        self.assertLess(page_call,resume)