"""Native contracts/guard/controller tests. Never request a real recruiting site."""
from collections import deque
from dataclasses import replace
import json
from pathlib import Path
import socket
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.adapters import DOMAdapter, builtins, Registry
from vibe_job_radar.guided.contracts import CrawlError
from vibe_job_radar.guided.native_policy import NativeRule, NativeContract, NativeRobots, contract_for
from vibe_job_radar.guided.native_browser import NativeBackend, NativeControl
from vibe_job_radar.guided.native_tunnel import NativeTunnel
from vibe_job_radar.guided.diagnostic_trace import DiagnosticTrace
from vibe_job_radar.guided.rate import RateLedger, Limits, RateLimit
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.network import FetchError
from vibe_job_radar.network_policy import NetworkPolicy, use_policy
from vibe_job_radar.workspace import Workspace, InputError

HOST='jobs.fixture.test'
URL='https://'+HOST
SECRET='NOT-IN-DIAGNOSTICS-fixture-only'


def contract():
    return NativeContract('fixture_v1',(HOST,),(
        NativeRule('pages',HOST,r'/(?:search|next|job/[0-9]+|login|redirect|denied|limited|large|badcert|cross|unknown|origin-auth)',resources=('Document',),role='document'),
        NativeRule('assets',HOST,r'/fixture\.(?:js|css)',resources=('Script','Stylesheet'),role='asset'),
        NativeRule('query_jobs',HOST,r'/api/jobs',methods=('POST',)),
        NativeRule('login_form',HOST,r'/login',methods=('POST',),resources=('Document','Fetch','XHR'),role='login',authentication=True),
    ),bootstrap_only=False)


def adapter():
    return DOMAdapter('fixture','人工原生验收',(HOST,),URL+'/search','q',r'^/job/[0-9]+$',
                      URL+'/login',(HOST,),native_contract=contract())


def robots(text='User-agent: *\nAllow: /\n'):
    return NativeRobots(200,'text/plain; charset=utf-8',text.encode())


class NativePolicyTests(unittest.TestCase):
    def test_bootstrap_is_not_a_claim_of_live_api(self):
        c=contract_for(builtins().get('liepin'))
        self.assertFalse(c.bootstrap_only)
        self.assertEqual({r.key for r in c.rules if r.role=='business'},
                         {'liepin_search', 'liepin_search_preflight', 'liepin_search_filters', 'liepin_filters_preflight',
                          'liepin_regions', 'liepin_regions_preflight', 'liepin_search_suggest', 'liepin_suggest_preflight'})
        self.assertEqual({r.key for r in c.rules if r.role == 'login'},
                         {'liepin_password_login', 'liepin_login_preflight'})
        self.assertTrue(all(r.authentication for r in c.rules if r.role == 'login'))
        for site in ('boss','51job'):
            with self.assertRaises(CrawlError):contract_for(builtins().get(site))
    def test_reviewed_read_post_is_allowed(self):
        self.assertEqual(contract().match(URL+'/api/jobs','POST','Fetch').role,'business')
    def test_unknown_get_and_post_are_not_assumed_readonly(self):
        for method in ('GET','POST','PUT','DELETE'):
            with self.assertRaises(CrawlError):contract().match(URL+'/apply',method,'Fetch')
    def test_login_is_only_explicit_mode(self):
        with self.assertRaises(CrawlError):contract().match(URL+'/login','POST','Fetch')
        self.assertEqual(contract().match(URL+'/login','POST','Fetch',authentication=True).role,'login')
    def test_host_boundary_and_port(self):
        for url in ('http://'+HOST+'/search',URL+':444/search','https://'+HOST+'.evil.test/search',
                    'https://user:pass@'+HOST+'/search','https://127.0.0.1/search'):
            with self.subTest(url=url), self.assertRaises(CrawlError):contract().match(url,'GET','Document')
    def test_normalized_path_smuggling(self):
        for path in ('/a/../search','/%2e%2e/search','/%252e%252e/search','/foo\\search','/foo%5csearch','/search\n'):
            with self.subTest(path=path),self.assertRaises(CrawlError):contract().match(URL+path,'GET','Document')
    def test_methods_and_rules_checked_at_construction(self):
        with self.assertRaises(ValueError):NativeRule('apply',HOST,'/',methods=('DELETE',))
        with self.assertRaises(ValueError):NativeContract('fixture',(HOST,),(NativeRule('data','other.test','/'),))
    def test_exact_host_only_no_wildcard_host(self):
        with self.assertRaises(ValueError):NativeContract('fixture',('*.test',),())
    def test_html_non200_and_empty_fail_closed(self):
        for status,mime,body in ((200,'text/html',b'User-agent: *'),(503,'text/plain',b''),
                                (200,'text/plain',b''),(200,'text/plain',b'<html>')):
            with self.subTest(status=status,mime=mime),self.assertRaises(CrawlError):NativeRobots(status,mime,body)
    def test_robots_encoding_size(self):
        for b in (b'\xff',b'x'*524289):
            with self.assertRaises(CrawlError):NativeRobots(200,'text/plain',b)
    def test_longest_match_and_allow_tie(self):
        r=robots('User-agent: *\nDisallow: /\nAllow: /search\nDisallow: /search/private\nAllow: /search/private\n')
        self.assertTrue(r.allowed(URL+'/search/private'));self.assertFalse(r.allowed(URL+'/job/1'))
    def test_wildcard_and_end_anchor(self):
        r=robots('User-agent: *\nDisallow: /job/*?*\nDisallow: /login$\n')
        self.assertFalse(r.allowed(URL+'/job/1?q=secret'));self.assertTrue(r.allowed(URL+'/job/1'))
        self.assertFalse(r.allowed(URL+'/login'));self.assertTrue(r.allowed(URL+'/login/ok'))
    def test_specific_groups_merge_and_override_wildcard(self):
        r=robots('User-agent: *\nDisallow: /\nUser-agent: vibeJobRadar\nAllow: /search\nUser-agent: VIBEJOBRADAR\nDisallow: /private\n')
        self.assertTrue(r.allowed(URL+'/search'));self.assertFalse(r.allowed(URL+'/private'))
    def test_percent_octets(self):
        r=robots('User-agent: *\nDisallow: /中文\nDisallow: /a%2fb\nDisallow: /%7euser\n')
        self.assertFalse(r.allowed(URL+'/%E4%B8%AD%E6%96%87'))
        self.assertFalse(r.allowed(URL+'/a%2Fb'));self.assertTrue(r.allowed(URL+'/a/b'))
        self.assertFalse(r.allowed(URL+'/~user'))
    def test_publisher_extensions_validated(self):
        r=robots('User-agent: *\nCrawl-delay: 45\nRequest-rate: 2/60\n')
        self.assertEqual(r.delay,45);self.assertEqual(r.windows,[(2,60)])
        for v in ('nan','-3','1e999'):
            with self.assertRaises(CrawlError):robots('User-agent: *\nCrawl-delay: '+v)
    def test_hostile_wildcards_do_not_backtrack(self):
        r=robots('User-agent: *\nDisallow: /'+'a*'*30+'b$\n')
        self.assertTrue(r.allowed(URL+'/'+'a'*8000))
    def test_robots_is_not_regex(self):
        r=robots('User-agent: *\nDisallow: /a[0-9]+\n')
        self.assertTrue(r.allowed(URL+'/a123'))


class TunnelTests(unittest.TestCase):
    def setUp(self):
        self.cancel=threading.Event();self.guard=NativeTunnel((HOST,),NetworkPolicy(),self.cancel,timeout=1)
    def tearDown(self):self.guard.close()
    def send(self, target=None, auth=True, extra=''):
        sock=socket.create_connection(self.guard.server.server_address,2)
        with sock:
            msg=f'CONNECT {target or HOST+":443"} HTTP/1.1\r\nHost: {HOST}:443\r\n'
            if auth:msg+='Proxy-Authorization: '+self.guard._authorization+'\r\n'
            sock.sendall((msg+extra+'\r\n').encode());return sock.recv(4096)
    def test_unauthenticated_connection_does_not_resolve(self):
        with patch.object(self.guard,'_open') as dial:
            self.assertIn(b'407',self.send(auth=False));dial.assert_not_called()
    def test_unapproved_authority_never_dials(self):
        with patch.object(self.guard,'_open') as dial:
            for target in ('127.0.0.1:443',HOST+'.evil:443',HOST+':80','user@'+HOST+':443'):
                self.assertIn(b'403',self.send(target));dial.assert_not_called()
    def test_duplicate_proxy_auth_is_rejected(self):
        with patch.object(self.guard,'_open') as dial:
            self.assertIn(b'400',self.send(extra='Proxy-Authorization: x\r\n'));dial.assert_not_called()
    def test_cancel_before_connect(self):
        self.cancel.set()
        with patch.object(self.guard,'_open') as dial:
            self.assertIn(b'503',self.send());dial.assert_not_called()
    def test_private_or_mixed_dns_never_dials(self):
        for ips in (['10.0.0.1'],['93.184.216.34','127.0.0.1']):
            answers=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443)) for ip in ips]
            with patch('socket.getaddrinfo',return_value=answers),patch('socket.create_connection') as dial:
                with self.assertRaises(FetchError):self.guard._open(HOST)
                dial.assert_not_called()
    def test_validated_ip_is_not_resolved_again(self):
        addresses=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('93.184.216.34',443))]
        with patch('socket.getaddrinfo',return_value=addresses) as dns,patch('socket.create_connection',return_value=Mock()) as dial:
            self.guard._open(HOST)
            dns.assert_called_once();self.assertEqual(dial.call_args.args[0],('93.184.216.34',443))
    def test_selected_proxy_failure_has_no_direct_fallback(self):
        from vibe_job_radar.loopback_proxy import LoopbackProxy,LocalProxyError
        self.guard.policy=NetworkPolicy('explicit_application',LoopbackProxy('127.0.0.1',1234))
        with patch('socket.getaddrinfo',return_value=[(2,1,6,'',('93.184.216.34',443))]),patch.object(LoopbackProxy,'open_tunnel',side_effect=LocalProxyError('local_proxy_connection_failed')) as upstream,patch('socket.create_connection') as direct:
            with self.assertRaises(FetchError):self.guard._open(HOST)
            direct.assert_not_called();upstream.assert_called_once()
    def test_close_removes_listener(self):
        address=self.guard.server.server_address;self.guard.close()
        with self.assertRaises(OSError):socket.create_connection(address,.1)
        self.guard.close=lambda:None


class NativeControllerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        b=NativeBackend.__new__(NativeBackend);self.b=b
        b.adapter=adapter();b.contract=contract();b.cancelled=threading.Event()
        b.error=b.wait_error=None;b._halted=b._closing=b._loading_robots=False
        b._page_sessions={};b._bound_pages={};b._page_creation=0
        b._robots_url='';b._sessions={'session':'frame'};b._pending={};b._requests={};b._hops={}
        b._auth_attempts=set();b._epoch=1;b._observations=deque(maxlen=20);b._observed_bytes=0
        b._pagination_page=None;b.auth_mode=False;b._send=Mock();b._cdp=Mock()
        b._diagnostics=DiagnosticTrace('a'*32,'fixture')
        b.tunnel=SimpleNamespace(last_error='',endpoint='http://127.0.0.1:54321',username='radar',password=SECRET)
        b.wire=NativeControl(b.adapter,RateLedger(Path(self.tmp.name)/'rate.sqlite',Limits(page_interval=0,request_interval=0)),b.cancelled)
        b.wire.install_robots(URL,200,'text/plain',b'User-agent: *\nAllow: /\n')
        b.native_counts={k:0 for k in ('document','business','asset','robots','login','blocked','responses')}
    def req(self,path='/api/jobs',method='POST',kind='Fetch',**extra):
        return {'requestId':'fetch-1','networkId':'net-1','frameId':'frame','resourceType':kind,
                'request':{'url':URL+path,'method':method,'postData':SECRET},**extra}
    def response(self,status=200,headers=None,**kw):
        event=self.req(**kw);event.update(responseStatusCode=status,responseHeaders=[{'name':k,'value':v} for k,v in (headers or {}).items()]);return event
    def test_owned_target_installs_actual_identified_agent_before_running(self):
        self.b._native_user_agent='ActualBrowser/1.0 VibeJobRadar/0.1'
        self.b._page_sessions['new-session']=Mock()
        self.b._install_target('new-session', {'targetId':'new-target'})
        calls=self.b._send.call_args_list
        override=[c for c in calls if c.args[1]=='Network.setUserAgentOverride']
        self.assertEqual(override[0].args[2],{'userAgent':self.b._native_user_agent})
        self.assertLess(calls.index(override[0]),next(i for i,c in enumerate(calls) if c.args[1]=='Runtime.runIfWaitingForDebugger'))

    def test_native_request_not_replayed(self):
        self.b._paused('session',self.req())
        self.b._send.assert_called_with('session','Fetch.continueRequest',{'requestId':'fetch-1'})
        self.assertEqual(self.b.native_counts['business'],1)
        with self.assertRaises(RuntimeError):self.b.wire.fetch(URL)
    def test_unknown_operation_is_visible_failure(self):
        self.b._paused('session',self.req('/apply'))
        self.assertEqual(self.b.error,'native_operation_unreviewed')
        self.assertEqual(self.b._send.call_args.args[1],'Fetch.failRequest')
    def test_cancelled_request_not_continued(self):
        self.b.cancelled.set();self.b._paused('session',self.req())
        self.assertEqual(self.b.error,'paused')
    def test_policy_revocation_stops_pooled_request(self):
        self.b.policy_check=lambda:False;self.b._paused('session',self.req())
        self.assertEqual(self.b.error,'native_policy_changed')
    def test_same_origin_redirect_stays_native(self):
        self.b._paused('session',self.req('/redirect','GET','Document'))
        self.b._paused('session',self.response(302,{'location':'/search'},path='/redirect',method='GET',kind='Document'))
        self.b._send.assert_called_with('session','Fetch.continueResponse',{'requestId':'fetch-1'})
    def test_each_redirect_hop_is_checked_and_counted(self):
        self.b._paused('session',self.req('/redirect','GET','Document'))
        self.b._paused('session',self.response(302,{'location':'/search'},path='/redirect',method='GET',kind='Document'))
        self.b._paused('session',self.req('/search','GET','Document',requestId='fetch-2'))
        self.assertEqual(self.b.native_counts['document'],2)
    def test_redirect_to_unreviewed_path_cannot_send(self):
        self.b._paused('session',self.req('/search','GET','Document'))
        self.b._paused('session',self.req('/apply','GET','Document',requestId='fetch-2'))
        self.assertEqual(self.b._send.call_args.args[1],'Fetch.failRequest')
    def test_cross_origin_redirect_is_stopped(self):
        self.b._paused('session',self.req());self.b._paused('session',self.response(302,{'location':'https://evil.test/'}))
        self.assertEqual(self.b.error,'redirect_requires_attention')
    def test_response_compression_headers_untouched(self):
        self.b._paused('session',self.req());self.b._paused('session',self.response(200,{'content-encoding':'gzip','set-cookie':SECRET}))
        self.b._send.assert_called_with('session','Fetch.continueResponse',{'requestId':'fetch-1'})
        self.assertNotIn(SECRET,json.dumps(self.b._diagnostics.snapshot()))
    def test_http_429_persists_cooldown(self):
        self.b._paused('session',self.req());self.b._paused('session',self.response(429,{'retry-after':'999'}))
        self.assertEqual(self.b.error,'http_429')
        with self.assertRaises(RateLimit):self.b.wire.ledger.reserve('fixture','request')
    def test_403_is_not_network_error(self):
        self.b._paused('session',self.req());self.b._paused('session',self.response(403))
        self.assertEqual(self.b.error,'http_403')
    def test_unaccounted_response_rejected(self):
        self.b._paused('session',self.response());self.assertEqual(self.b.error,'native_unaccounted_response')
    def test_oversized_response_stops(self):
        self.b._paused('session',self.req());self.b._paused('session',self.response(200,{'content-length':'5000001'}))
        self.assertEqual(self.b.error,'response_too_large')
    def test_iframe_surface_stops_before_request(self):
        self.b._paused('session',self.req(frameId='not-main-frame'))
        self.assertEqual(self.b.error,'native_surface_unsupported')
    def test_only_current_business_json_is_observed(self):
        self.b._paused('session',self.req());self.b._paused('session',self.response(200,{'content-type':'application/json'}))
        self.b._finished('session',{'requestId':'net-1'})
        callback=self.b._send.call_args.args[3];callback({'body':'{"jobs": []}'})
        self.assertEqual(self.b.observations()[0].payload,{'jobs':[]})
        self.assertNotIn('payload=',repr(self.b.observations()[0]))
    def test_late_old_epoch_body_is_ignored(self):
        self.b._paused('session',self.req());self.b._paused('session',self.response(200,{'content-type':'application/json'}))
        self.b._finished('session',{'requestId':'net-1'});callback=self.b._send.call_args.args[3]
        self.b._epoch+=1;callback({'body':'{"jobs": []}'})
        self.assertEqual(self.b.observations(),())
    def test_local_proxy_secret_not_given_to_origin_auth(self):
        self.b._authenticate('session',{'requestId':'fetch-1','authChallenge':{'source':'Server','origin':URL}})
        self.assertEqual(self.b._send.call_args.args[2]['authChallengeResponse'],{'response':'CancelAuth'})
    def test_proxy_challenge_only_exact_endpoint(self):
        self.b._authenticate('session',{'requestId':'fetch-1','authChallenge':{'source':'Proxy','origin':self.b.tunnel.endpoint}})
        self.assertEqual(self.b._send.call_args.args[2]['authChallengeResponse']['response'],'ProvideCredentials')
        self.b._authenticate('session',{'requestId':'fetch-2','authChallenge':{'source':'Proxy','origin':'http://127.0.0.1:9999'}})
        self.assertEqual(self.b._send.call_args.args[2]['authChallengeResponse']['response'],'CancelAuth')
    def test_native_and_bridge_share_durable_quota(self):
        from vibe_job_radar.guided.transport import PinnedTransport
        path=Path(self.tmp.name)/'shared.sqlite';limits=Limits(requests_hour=1,request_interval=0)
        ledger=RateLedger(path,limits);native=NativeControl(adapter(),ledger,threading.Event())
        native.reserve('request',origin=URL)
        legacy=PinnedTransport(adapter(),RateLedger(path,limits),threading.Event())
        with self.assertRaises(RateLimit):legacy.reserve('request',origin=URL)


class NativeServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.service=GuidedService(Workspace(Path(self.tmp.name)),registry=Registry([adapter()]))
        self.addCleanup(self.service.close)
        self.service._submit=Mock()
        self.query={'platform':'fixture','keyword':'人工','roles':['time_series'],'max_jobs':1,'consent':True,'rights_note':'人工来源测试'}
    def test_old_create_defaults_to_bridge(self):
        task=self.service.create(self.query);self.assertEqual(self.service._load(task['id'])['backend'],'bridge')
    def test_native_needs_explicit_opt_in(self):
        with self.assertRaises(InputError):self.service.create({**self.query,'backend':'native'})
    def test_native_mode_is_saved_in_task(self):
        task=self.service.create({**self.query,'backend':'native','native_consent':True})
        self.assertEqual(self.service._load(task['id'])['backend'],'native')
    def test_task_cannot_switch_backend(self):
        task=self.service.create(self.query)
        with self.assertRaises(InputError):self.service.action({'id':task['id'],'action':'search','backend':'native'})
    def test_invalid_mode_not_treated_as_default(self):
        for mode in ([],True,'direct','CDP'):
            with self.assertRaises(InputError):self.service.create({**self.query,'backend':mode})
    def test_capabilities_are_honest_about_bootstrap(self):
        s=GuidedService(Workspace(Path(self.tmp.name)/'builtin'));self.addCleanup(s.close)
        sites={s['key']:s['native'] for s in s.state()['sites']}
        self.assertFalse(sites['liepin']['bootstrap_only']);
        self.assertEqual(sites['liepin']['certification'], 'not_live_verified');self.assertFalse(sites['boss']['available'])


if __name__=='__main__': unittest.main()
