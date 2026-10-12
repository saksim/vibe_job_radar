"""Redirect policy, exact batch results and honest real-example behavior (offline)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from vibe_job_radar.network import FetchError, Response, SafeHTTP, SiteFetcher
from vibe_job_radar.redirect_policy import redirect_target
from vibe_job_radar.collection import Collector, TERMINAL
from vibe_job_radar.public_example import PublicExample, parse_public_job, API_URL, SOURCE_URL, JOB_ID
from vibe_job_radar.workspace import Workspace, InputError
from vibe_job_radar.store import Store

BASE = 'https://www.zhipin.com'
A, B = BASE+'/job_detail/a.html', BASE+'/job_detail/b.html'
HTML = '<h1>架构师</h1><div class="job-sec-text">要求熟练使用 Cursor 进行 AI 辅助编程，开发服务并编写单元测试。</div>'


def response(status=200, content=HTML, location=None, mime='text/html', url=''):
    return Response(status, {'content-type':mime, **({'location':location} if location is not None else {})},content.encode(),url)


def robots(rules='User-agent: *\nAllow: /'):
    return response(content=rules,mime='text/plain')


class Wire:
    interval=0
    def __init__(self, *responses):self.responses=iter(responses);self.calls=[]
    def request(self, url):
        self.calls.append(url)
        item=next(self.responses)
        if isinstance(item,Exception):raise item
        return item


class RedirectTests(unittest.TestCase):
    def test_relative_redirect_reuses_robots_and_keeps_final_url(self):
        wire=Wire(robots(),response(302,location='/job_detail/b.html'),response())
        client=SiteFetcher({'zhipin.com'},wire)
        result=client.fetch(A)
        self.assertEqual(result.url,B);self.assertEqual(wire.calls,[BASE+'/robots.txt',A,B])
        self.assertEqual(client.last_diagnostic['http_attempts'],3)
        self.assertEqual(result.redirect_trace[0]['status'],302)

    def test_all_supported_anonymous_get_redirect_statuses(self):
        for code in (301,302,303,307,308):
            with self.subTest(code=code):self.assertEqual(redirect_target(A,'b.html',{'zhipin.com'},status=code),B)

    def test_auth_redirect_never_requests_or_logs_credential_target(self):
        wire=Wire(robots(),response(302,location='/web/user/?token=DO-NOT-STORE'))
        client=SiteFetcher({'zhipin.com'},wire)
        with self.assertRaises(FetchError) as err:client.fetch(A)
        self.assertEqual(err.exception.code,'redirect_login_required');self.assertEqual(len(wire.calls),2)
        self.assertNotIn('DO-NOT-STORE',json.dumps(client.last_diagnostic))
        self.assertEqual(client.last_diagnostic['phase'],'detail')

    def test_challenge_redirect_is_not_a_normalization_redirect(self):
        with self.assertRaisesRegex(FetchError,'redirect_verification_required'):
            redirect_target(A,'/wapi/zppass/verify?token=secret',{'zhipin.com'},status=302)

    def test_credentials_on_normal_redirect_are_refused(self):
        with self.assertRaisesRegex(FetchError,'redirect_credentials_blocked'):
            redirect_target(A,'/job_detail/b.html?access_token=secret',{'zhipin.com'},status=302)

    def test_private_http_unapproved_port_and_domain_targets_are_blocked(self):
        for url in ('http://www.zhipin.com/x','https://www.zhipin.com:8888/x',
                    'https://evil.test/job/1','https://127.0.0.1/x','https://u:p@www.zhipin.com/x'):
            with self.subTest(url=url),self.assertRaises(FetchError):
                redirect_target(A,url,{'zhipin.com'},status=302)

    def test_invalid_location_or_unsupported_status_never_followed(self):
        for target in (None,'','/x\nHost: evil','\\evil.test/a'):
            with self.subTest(target=target),self.assertRaises(FetchError):redirect_target(A,target,{'zhipin.com'},status=302)
        with self.assertRaisesRegex(FetchError,'redirect_unsupported_status'):
            redirect_target(A,'/b',{'zhipin.com'},status=304)

    def test_same_origin_redirect_rechecks_robots_for_new_path(self):
        wire=Wire(robots('User-agent: *\nDisallow: /private'),response(302,location='/private/job'))
        with self.assertRaisesRegex(FetchError,'robots_denied'):SiteFetcher({'zhipin.com'},wire).fetch(A)
        self.assertEqual(len(wire.calls),2)

    def test_new_subdomain_gets_own_robots(self):
        dest='https://jobs.zhipin.com/job_detail/b.html'
        wire=Wire(robots(),response(302,location=dest),robots(),response())
        self.assertEqual(SiteFetcher({'zhipin.com'},wire).fetch(A).url,dest)
        self.assertIn('https://jobs.zhipin.com/robots.txt',wire.calls)

    def test_robots_redirect_is_distinguished_and_same_origin_only(self):
        wire=Wire(response(301,location='/static/robots.txt'),robots(),response())
        client=SiteFetcher({'zhipin.com'},wire);client.fetch(A)
        self.assertEqual(client.last_diagnostic['redirects'][0]['phase'],'robots')
        wire=Wire(response(301,location='https://cdn.zhipin.com/robots.txt'))
        with self.assertRaisesRegex(FetchError,'robots_redirect_cross_origin'):SiteFetcher({'zhipin.com'},wire).fetch(A)
        self.assertEqual(len(wire.calls),1)

    def test_robots_redirect_accepts_equivalent_https_origins(self):
        for job, destination in ((A, BASE + ':443/static/robots.txt'),
                                 (BASE + ':443/job_detail/a.html', BASE + '/static/robots.txt')):
            with self.subTest(job=job, destination=destination):
                wire = Wire(response(301, location=destination), robots(), response())
                client = SiteFetcher({'zhipin.com'}, wire)
                result = client.fetch(job)
                origin = job.rsplit('/job_detail/', 1)[0]
                self.assertEqual(wire.calls, [origin + '/robots.txt', destination, job])
                self.assertEqual(result.url, job)
                self.assertEqual(client.last_diagnostic['http_attempts'], 3)
                self.assertEqual(client.last_diagnostic['redirects'][0]['result'], 'followed')

    def test_equivalent_robots_origin_still_applies_the_returned_rules(self):
        destination = BASE + ':443/static/robots.txt'
        wire = Wire(response(301, location=destination), robots('User-agent: *\nDisallow: /'))
        with self.assertRaisesRegex(FetchError, 'robots_denied'):
            SiteFetcher({'zhipin.com'}, wire).fetch(A)
        self.assertEqual(wire.calls, [BASE + '/robots.txt', destination])

    def test_equivalent_robots_origin_cannot_turn_html_into_rules(self):
        destination = BASE + ':443/static/robots.txt'
        wire = Wire(response(301, location=destination), response(content='<html><div id="app"></div></html>'))
        with self.assertRaisesRegex(FetchError, 'robots_unavailable'):
            SiteFetcher({'zhipin.com'}, wire).fetch(A)
        self.assertEqual(wire.calls, [BASE + '/robots.txt', destination])

    def test_loop_and_total_hop_budget(self):
        wire=Wire(robots(),response(302,location=A))
        with self.assertRaisesRegex(FetchError,'redirect_loop'):SiteFetcher({'zhipin.com'},wire).fetch(A)
        wire=Wire(robots(),response(302,location=B),response(302,location='/job_detail/c.html'))
        with self.assertRaisesRegex(FetchError,'redirect_limit'):SiteFetcher({'zhipin.com'},wire,max_redirects=1).fetch(A)
        self.assertEqual(len(wire.calls),3)

    def test_accepted_redirect_cannot_evade_dns_on_second_hop(self):
        class Raw:
            def __init__(self,status,headers,body):self.status=status;self.headers=headers;self.body=body
            def getheaders(self):return self.headers
            def read(self,n):return self.body
        raws=iter([Raw(200,[('content-type','text/plain')],b'User-agent: *\nAllow: /'),
                   Raw(302,[('Location',B)],b'')])
        class Conn:
            def __init__(self,*a):pass
            def request(self,*a,**k):pass
            def getresponse(self):return next(raws)
            def close(self):pass
        addresses=lambda ip:[(2,1,6,'',(ip,443))]
        with patch('socket.getaddrinfo',side_effect=[addresses('8.8.8.8'),addresses('8.8.8.8'),addresses('127.0.0.1')]),patch('vibe_job_radar.network.PinnedHTTPSConnection',Conn):
            with self.assertRaisesRegex(FetchError,'non_public_address'):
                SiteFetcher({'zhipin.com'},SafeHTTP({'zhipin.com'},interval=0)).fetch(A)

    def test_unused_captcha_script_is_not_visible_challenge(self):
        wire=Wire(robots(),response(content=HTML+'<script>const captcha = true;</script>'))
        self.assertEqual(SiteFetcher({'zhipin.com'},wire).fetch(A).status,200)

    def test_bad_redirect_limit_rejected(self):
        for value in (True,-1,6):
            with self.assertRaises(ValueError):SiteFetcher({'zhipin.com'},max_redirects=value)


class BatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.w=Workspace(self.tmp.name);self.c=Collector(self.w)
    def start(self,**kw):
        return self.c.start({'mode':'urls','platforms':['boss'],'permit_platforms':['boss'],'roles':['architect'],
            'urls':A,'detail_budget':1,'rights_note':'人工测试来源','consent':True,**kw})
    def finish(self,state):
        for _ in range(20):
            state=self.c.step({'id':state['id']})
            if state['status'] in TERMINAL:return state
        self.fail('did not finish')
    def test_redirect_success_consumes_one_job_attempt_not_one_per_hop(self):
        client=SiteFetcher({'zhipin.com'},Wire(robots(),response(302,location=B),response()))
        with patch('vibe_job_radar.collection.SiteFetcher',return_value=client):result=self.finish(self.start())
        self.assertEqual(result['detail_attempts'],1)
        with Store(self.w.db) as store:self.assertEqual(store.records()[0].url,B)
        self.assertEqual(result['details'][0]['final_url'],B)
        self.assertTrue(result['report_id'])
    def test_zero_new_records_cannot_reuse_unrelated_history_as_result(self):
        self.w.add_job({'title':'架构师','text':'熟练使用Cursor进行编程','url':B,'platform':'boss',
                       'rights_note':'unit test','full_text_confirmed':True})
        client=SiteFetcher({'zhipin.com'},Wire(robots(),response(302,location='/login')))
        with patch('vibe_job_radar.collection.SiteFetcher',return_value=client):result=self.finish(self.start())
        self.assertEqual(result['report_id'],'');self.assertEqual(result['saved_detail_count'],0)
    def test_report_contains_only_successful_selected_batch(self):
        self.w.add_job({'title':'架构师','text':'熟练使用Codex编程','url':BASE+'/job_detail/history.html','platform':'boss','rights_note':'unit test','full_text_confirmed':True})
        client=SiteFetcher({'zhipin.com'},Wire(robots(),response()))
        with patch('vibe_job_radar.collection.SiteFetcher',return_value=client):result=self.finish(self.start())
        self.assertEqual(self.w.report(result['report_id'])['manifest']['stats']['full_text_job_groups'],1)
    def test_budget_skips_and_redirect_failure_explained_separately(self):
        client=SiteFetcher({'zhipin.com'},Wire(robots(),response(302,location='https://outside.test/x')))
        with patch('vibe_job_radar.collection.SiteFetcher',return_value=client):result=self.finish(self.start(urls=A+'\n'+B))
        self.assertEqual(result['budget_skipped_count'],1)
        self.assertIn('未执行',result['details'][1]['status_message'])
        self.assertIn('HTTP',result['route_label'])
    def test_old_redirect_log_is_not_reinterpreted_as_login(self):
        state=self.start();state['details'][0]['status']='redirect_not_followed'
        self.c._save(state);view=self.c.status({'id':state['id']})
        self.assertIn('不能判断',view['details'][0]['next_action'])
        self.assertNotIn('fetch_diagnostic',view['details'][0])


def fixture_payload():
    return {'id':JOB_ID,'absolute_url':SOURCE_URL,'title':'Technical Architect',
        'location':{'name':'ARTIFICIAL UNIT TEST'},'updated_at':'2026-09-14T00:00:00Z',
        'content':'<p>ARTIFICIAL TEST NOT REAL JOB.</p><p>Must use Claude Code for AI-assisted coding. Build maintainable services, write tests, and review generated code.</p>'}


class PublicExampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.w=Workspace(self.tmp.name)
    def test_public_contract_mapping_does_not_invent_date_or_title(self):
        record=parse_public_job(fixture_payload());self.assertEqual(record.title,'Technical Architect')
        self.assertEqual(record.published_at,'');self.assertEqual(record.url,SOURCE_URL)
        self.assertFalse(record.is_synthetic);self.assertEqual(record.evidence_level,'full_text')
    def test_wrong_identity_truncated_content_or_wrong_role_rejected(self):
        for change in ({'id':0},{'absolute_url':'https://other.test'},{'content':''},{'title':'Accountant'}):
            with self.subTest(change=change),self.assertRaises(FetchError):parse_public_job({**fixture_payload(),**change})
    def test_explicit_case_through_real_store_and_report_is_isolated(self):
        with patch.object(SafeHTTP,'json',return_value=fixture_payload()) as fetch:
            service=PublicExample(self.w);result=service.run({'consent':True})
            again=service.run({'consent':True})
        self.assertTrue(result['success'],result);self.assertEqual(fetch.call_count,1)
        self.assertTrue(again['cache_reused']);self.assertEqual(again['network_requests_this_click'],0)
        self.assertEqual(result['collected_at'],again['collected_at'])
        self.assertEqual(result['stats']['full_text_job_groups'],1)
        self.assertGreater(result['stats']['requirement_rows'],0)
        self.assertIn('public_source.json',result['report']['files'])
    def test_actual_provider_failure_never_falls_back_to_synthetic(self):
        with patch.object(SafeHTTP,'json',side_effect=FetchError('http_404')):
            result=PublicExample(self.w).run({'consent':True})
        self.assertFalse(result['success']);self.assertEqual(result['report_id'],'')
        self.assertFalse(self.w.db.exists())
        self.assertEqual(result['code'],'http_404')
    def test_cache_metadata_survives_process_restart(self):
        with patch.object(SafeHTTP,'json',return_value=fixture_payload()) as fetch:
            first=PublicExample(self.w).run({'consent':True});second=PublicExample(self.w).run({'consent':True})
        self.assertEqual(fetch.call_count,1);self.assertEqual(first['report_id'],second['report_id'])
    def test_no_secret_or_arbitrary_endpoint_parameters(self):
        service=PublicExample(self.w)
        for data in ({},{'consent':False},{'consent':True,'url':'https://127.0.0.1'},{'consent':True,'key':'abc'}):
            with self.subTest(data=data),self.assertRaises(InputError):service.run(data)
    def test_failures_still_count_against_persistent_rate_limit(self):
        with patch.object(SafeHTTP,'json',side_effect=FetchError('http_404')) as fetch:
            PublicExample(self.w).run({'consent':True})
            with self.assertRaises(InputError):PublicExample(self.w).run({'consent':True})
        self.assertEqual(fetch.call_count,1)


class UserCommandTests(unittest.TestCase):
    def test_explicit_user_command_keeps_downloadable_report(self):
        import contextlib, io, runpy
        command=runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/run_real_example.py'))['main']
        with tempfile.TemporaryDirectory() as tmp, patch.object(SafeHTTP,'json',return_value=fixture_payload()) as fetch, patch('webbrowser.open') as open_browser, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(command(['--yes','--workspace',tmp]),0)
            self.assertEqual(len(list((Path(tmp)/'reports').glob('*/requirements_zh.csv'))),1)
            self.assertIn('Technical Architect',output.getvalue())
            self.assertTrue(open_browser.call_args.args[0].startswith('file:'))
            fetch.assert_called_once()

    def test_cancel_command_never_calls_upstream_or_opens_browser(self):
        import contextlib, io, runpy
        command=runpy.run_path(str(Path(__file__).resolve().parents[1]/'scripts/run_real_example.py'))['main']
        with tempfile.TemporaryDirectory() as tmp, patch('builtins.input',return_value='n'), patch.object(SafeHTTP,'json') as fetch, patch('webbrowser.open') as open_browser, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(command(['--workspace',tmp]),0)
        fetch.assert_not_called();open_browser.assert_not_called()

    def test_late_report_error_cannot_return_success_true(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(SafeHTTP,'json',return_value=fixture_payload()):
            ws=Workspace(tmp)
            with patch.object(ws,'report',side_effect=ValueError('fixture read failure')):
                result=PublicExample(ws).run({'consent':True})
            self.assertFalse(result['success']);self.assertEqual(result['report_id'],'')


if __name__=='__main__':unittest.main()
