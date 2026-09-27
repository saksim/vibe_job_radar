"""Artificial job documents. Observed URL shapes are not live site certification."""
from __future__ import annotations
import dataclasses
import json
import tempfile
import unittest
from pathlib import Path
from vibe_job_radar.guided.adapters import builtins, Registry
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.html_parser import parse_job_html, ParseError
from vibe_job_radar.workspace import Workspace
from test_guided import FakeBackend, detail, fixture_adapter

URL='https://www.liepin.com/job/123.shtml'
BODY='负责时间序列预测，使用 Cursor 进行 AI 辅助编程，编写单元测试并进行代码审查。'

def posting(**kw):
    p={'@type':'JobPosting','title':'时间序列算法工程师','description':BODY}
    p.update(kw); return p

def markup(*values):
    return '<html><h1>人工详情</h1>'+''.join('<script type="application/ld+json">'+json.dumps(v,ensure_ascii=False)+'</script>' for v in values)+'</html>'

class SiteShapeTests(unittest.TestCase):
    def test_observed_liepin_families_are_all_discovered(self):
        a=builtins().get('liepin')
        paths=['/job/123.shtml','/a/456.shtml','/lptjob/789']
        cards=a.cards(PageSnapshot(a.search_url('时序'),''.join(f'<a href="{p}">人工算法岗位</a>' for p in paths)))
        self.assertEqual([c.url for c in cards],['https://www.liepin.com'+p for p in paths])
        for c in cards: self.assertIn('Cursor', a.detail(PageSnapshot(c.url,markup(posting(url=c.url))))['text'])
    def test_new_detail_families_are_not_search_recommendations(self):
        a=builtins().get('liepin')
        for p in ('/a/456.shtml','/lptjob/789'):
            with self.subTest(p=p),self.assertRaises(CrawlError):
                a.cards(PageSnapshot('https://www.liepin.com'+p,'<a href="/job/123.shtml">推荐</a>'))
    def test_no_wildcard_admission_for_new_paths(self):
        a=builtins().get('liepin')
        for p in ('/a/not-a-job.shtml','/lptjob/search','/a/456.shtml/send','/lptjob/789/edit','/a/456.json'):
            with self.subTest(p=p),self.assertRaises(CrawlError): a.accept_url('https://www.liepin.com'+p,detail=True)
    def test_new_routes_keep_credential_and_origin_checks(self):
        a=builtins().get('liepin')
        for u in ('http://www.liepin.com/a/1.shtml','https://evil.test/a/1.shtml',
                  'https://www.liepin.com/lptjob/1?token=private','https://www.liepin.com/a/1.shtml?key=private'):
            with self.subTest(u=u),self.assertRaises(CrawlError): a.accept_url(u,detail=True)

class StructuredTargetTests(unittest.TestCase):
    def parse(self,*p,source=URL): return parse_job_html(markup(*p),source_url=source)
    def test_exact_target_can_be_selected_among_explicit_recommendations(self):
        r=self.parse(posting(url=URL),posting(url='/job/other.shtml',title='推荐岗位',description='推荐的完整职位描述，不属于选中的目标岗位，不应进入报告。'))
        self.assertEqual(r['title'],'时间序列算法工程师');self.assertEqual(r['text'],BODY)
    def test_main_entity_page_is_an_explicit_target_reference(self):
        for ref in (URL, {'@id':URL}, {'url':URL}):
            with self.subTest(ref=ref):
                self.assertEqual(self.parse(posting(mainEntityOfPage=ref),posting(url='/job/other.shtml'))['text'],BODY)
    def test_fragment_id_can_bind_the_document_without_changing_source(self):
        self.assertEqual(self.parse(posting(**{'@id':URL+'#job'}),posting(url='/job/other.shtml'))['text'],BODY)
    def test_identity_tracking_normalizes_without_following_links(self):
        self.assertEqual(self.parse(posting(url=URL+'?utm_source=ad#job'),posting(url='/job/other.shtml'))['text'],BODY)
    def test_multiple_without_identity_remains_ambiguous(self):
        with self.assertRaises(ParseError): self.parse(posting(),posting(title='另一个职位'))
    def test_only_other_job_is_not_saved_under_current_url(self):
        with self.assertRaises(ParseError): self.parse(posting(url='/job/other.shtml'))
    def test_identity_conflict_on_same_posting_fails_closed(self):
        with self.assertRaises(ParseError): self.parse(posting(url=URL,mainEntityOfPage='/job/other.shtml'))
    def test_two_conflicting_documents_for_target_are_not_arbitrarily_picked(self):
        with self.assertRaises(ParseError): self.parse(posting(url=URL),posting(url=URL,title='另一个版本'))
    def test_single_without_identity_retains_legacy_behavior(self):
        self.assertEqual(self.parse(posting())['text'],BODY)
    def test_identical_scripts_do_not_create_ambiguity(self):
        self.assertEqual(self.parse(posting(url=URL),posting(url=URL))['text'],BODY)
    def test_unknown_parameters_remain_part_of_identity(self):
        with self.assertRaises(ParseError): self.parse(posting(url=URL+'?jobId=other'))
    def test_foreign_domain_cannot_bind_by_path(self):
        with self.assertRaises(ParseError): self.parse(posting(url='https://elsewhere.test/job/123.shtml'))
    def test_nonstring_posting_content_is_not_stringified_to_fulltext(self):
        for v in ([BODY], {'text':BODY}, True):
            with self.subTest(value=v),self.assertRaises(ParseError): self.parse(posting(description=v))
    def test_script_library_name_is_not_visible_challenge(self):
        r=parse_job_html(markup(posting())+'<script src="/captcha-sdk.js"></script>',source_url=URL)
        self.assertEqual(r['text'],BODY)
    def test_visible_challenge_with_hidden_posting_still_stops(self):
        with self.assertRaises(ParseError): parse_job_html('<h1>请完成安全验证</h1>'+markup(posting(url=URL)),source_url=URL)
    def test_duplicate_json_keys_not_accepted_as_reliable_identity(self):
        raw='<script type="application/ld+json">{"@type":"JobPosting","url":"/job/other.shtml","url":"'+URL+'","title":"工程师","description":"'+BODY+'"}</script>'
        with self.assertRaises(ParseError): parse_job_html(raw,source_url=URL)

class BatchOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ws=Workspace(self.tmp.name);self.a=fixture_adapter()
        self.s=GuidedService(self.ws,registry=Registry([self.a]),backend_factory=FakeBackend)
        self.addCleanup(self.s.close)
    def run_batch(self, pages):
        state={'id':'d'*32,'schema_version':1,'platform':self.a.key,'roles':['time_series'],'selection':['a','b'],
               'keyword':'时间序列','search_url':self.a.search_url('时间序列'), 'max_pages':1,'max_jobs':2,'pages_seen':[],
               'rights_note':'人工测试，不是真实授权','code':'new','phase':'collect','status':'ready','report_id':'',
               'cards':[{'id':k,'title':'待取得','url':'https://jobs.fixture.test/job/'+k,'source_url':self.a.search_url('时间序列'),'status':'discovered','record_id':'','resolved_url':''} for k in ('a','b')]}
        class Backend:
            def open(_,url):
                p=pages[url.rsplit('/',1)[-1]]
                if isinstance(p,Exception): raise p
                return PageSnapshot(url,p)
        self.s._save(state);self.s._collect(state,Backend(),self.a)
        return self.s._load(state['id'])
    def test_success_has_target_and_ai_counts_from_exact_batch(self):
        state=self.run_batch({'a':detail('').html,'b':detail('').html})
        result=state['outcome'];self.assertEqual(result['status'],'ready');self.assertEqual(result['saved'],2)
        self.assertEqual(result['target_jobs'],2);self.assertEqual(result['selected'],2)
    def test_all_failed_is_not_usable_even_when_task_finishes(self):
        state=self.run_batch({'a':'<h1>只有摘要</h1>','b':'<h1>没有正文</h1>'})
        self.assertEqual(state['outcome']['status'],'no_data');self.assertEqual(state['report_id'],'')
        self.assertEqual(state['outcome']['failed'],2)
    def test_off_target_report_is_explicit_not_useful_research(self):
        page='<h1>收银员</h1><div class="job-description">负责门店收银核对账目和每日盘点工作，并为顾客提供日常服务。</div>'
        state=self.run_batch({'a':page,'b':page})
        self.assertEqual(state['outcome']['status'],'no_target');self.assertEqual(state['outcome']['target_jobs'],0)
        self.assertEqual(state['outcome']['saved'],2)
    def test_no_ai_requirement_is_not_fabricated_or_mislabelled_network_failure(self):
        page='<h1>时间序列算法工程师</h1><div class="job-description">负责时间序列预测，使用统计模型完成研究工作并评估精度，协助业务上线。</div>'
        state=self.run_batch({'a':page,'b':page})
        self.assertEqual(state['outcome']['status'],'no_ai_evidence');self.assertEqual(state['outcome']['target_jobs'],2)
    def test_one_invalid_record_does_not_abort_the_following_job(self):
        long='<h1>时间序列算法工程师</h1><div class="job-description">'+('正文'*80000)+'</div>'
        state=self.run_batch({'a':long,'b':detail('').html})
        self.assertEqual(state['cards'][0]['status'],'invalid_job_data');self.assertEqual(state['cards'][1]['status'],'ok')
        self.assertEqual(state['outcome']['status'],'partial');self.assertEqual(state['outcome']['failed'],1)
    def test_report_has_same_outcome_and_per_selection_audit(self):
        state=self.run_batch({'a':'<h1>无正文</h1>','b':detail('').html})
        report=self.ws.report(state['report_id']);p=self.ws.report_file(state['report_id'],'guided_acquisition.json')
        audit=json.loads(p.read_text(encoding='utf-8'))
        self.assertEqual(audit['outcome'],state['outcome']);self.assertEqual(len(audit['items']),2)
        self.assertNotIn('rights_note',audit);self.assertNotIn('text',audit)
        import hashlib
        self.assertEqual(hashlib.sha256(p.read_bytes()).hexdigest(),report['manifest']['output_files_sha256'][p.name])


class TargetAdversarialTests(unittest.TestCase):
    def test_graph_selects_target_without_importing_other_job(self):
        r=parse_job_html(markup({'@context':'https://schema.org','@graph':[
            posting(url='/job/other.shtml',title='推荐岗位'), posting(url=URL)]}),source_url=URL)
        self.assertEqual(r['title'],'时间序列算法工程师')
    def test_malformed_identity_types_rejected_not_coerced(self):
        for value in (True, 12, {'bad':'value'}, [URL], URL+'?token=secret'):
            with self.subTest(value=value),self.assertRaises(ParseError):
                parse_job_html(markup(posting(url=value)),source_url=URL)
    def test_conflicting_refs_do_not_fall_through_to_dom(self):
        raw=markup(posting(url='/job/other.shtml'))+'<h1>工程师</h1><div class="job-description">'+BODY+'</div>'
        with self.assertRaises(ParseError): parse_job_html(raw,source_url=URL)
    def test_different_url_shape_not_assumed_to_be_same_job(self):
        with self.assertRaises(ParseError):
            parse_job_html(markup(posting(url='https://www.liepin.com/a/123.shtml')),source_url=URL)
    def test_different_query_order_is_same_stable_identity(self):
        r=parse_job_html(markup(posting(url=URL+'?jobId=123&region=x')),
                         source_url=URL+'?region=x&jobId=123')
        self.assertEqual(r['text'],BODY)
    def test_outcome_pending_is_not_silently_counted_as_failed_or_saved(self):
        state={'selection':['a','b','c'],'cards':[{'id':'a','status':'ok'},
            {'id':'b','status':'manual_required'},{'id':'c','status':'discovered'}]}
        m={'status':'completed','stats':{'full_text_job_groups':1,'vibe_evidence_job_groups':1}}
        o=GuidedService._outcome(state,m)
        self.assertEqual((o['saved'],o['failed'],o['pending']),(1,0,2))
        self.assertEqual(o['status'],'partial')
    def test_outcome_excludes_unselected_saved_and_failed_rows(self):
        state={'selection':['a'],'cards':[{'id':'a','status':'ok'},{'id':'b','status':'ok'},{'id':'c','status':'http_403'}]}
        o=GuidedService._outcome(state,{'status':'completed','stats':{'full_text_job_groups':1,'vibe_evidence_job_groups':1}})
        self.assertEqual(o['selected'],1);self.assertEqual(o['saved'],1);self.assertEqual(o['failed'],0)
    def test_incomplete_analysis_never_reports_ready(self):
        state={'selection':['a'],'cards':[{'id':'a','status':'ok'}]}
        o=GuidedService._outcome(state,{'status':'incomplete','stats':{'full_text_job_groups':1,'vibe_evidence_job_groups':1}})
        self.assertEqual(o['status'],'analysis_incomplete')

if __name__=='__main__': unittest.main()
