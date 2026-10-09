"""Independently written synthetic text in a published, recorded Liepin layout.

No third-party JD, browser state or live network access. Provenance and the exact
limitations of the external HTML observation are in LIEPIN_RECORDED_LAYOUT.md.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import time
import unittest

from vibe_job_radar.guided.adapters import DOMAdapter, Registry, builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.liepin import _jsonld_whitespace, semantic_detail
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.store import Store
from vibe_job_radar.workspace import Workspace

URL = 'https://www.liepin.com/job/123.shtml'
TITLE = '时间序列算法工程师'
BODY = ('岗位职责：负责时间序列预测与离线评估。\n'
        '任职要求：熟悉 Python，使用 AI 编程工具辅助开发，'
        '对生成代码编写单元测试并进行代码审查。')

NUMBERED = ('综合薪资：按能力面议。\n'
            '1.本科及以上，计算机相关专业，能够编写接口和自动测试。\n'
            '2.掌握时间序列建模，熟练使用Python和数据库。\n'
            '3.了解能源业务，具备模型评估和误差分析经验。\n'
            '4.具备代码审查能力，核对生成代码的正确性。')

def posting(**updates):
    return {'@context': 'https://schema.org', '@type': 'JobPosting',
            'title': TITLE, 'description': BODY, 'url': URL,
            'hiringOrganization': {'@type': 'Organization', 'name': '合成测试企业'},
            'datePosted': '2026-01-01', **updates}


def script(value, raw_breaks=True):
    content = json.dumps(value, ensure_ascii=False)
    if raw_breaks:
        content = content.replace(r'\n', '\n').replace(r'\r', '\r').replace(r'\t', '\t')
    return '<script type="application/ld+json">' + content + '</script>'


def markup(value=None, body=BODY, *, raw_breaks=True, anchor=True, attrs=''):
    # Real observed node/metadata shape; all words, identities and dates synthetic.
    head = script({'title': '合成百度索引元数据，不是职位标题'}, False)
    head += script(posting() if value is None else value, raw_breaks)
    intro = ('<section class="job-intro-container"><dl class="paragraph">'
             '<dt>职位介绍</dt><dd data-selector="job-intro-content" ' + attrs + '>'
             + body + '</dd></dl></section>') if anchor else ''
    return '<html><head>' + head + '</head><body>' + intro + '</body></html>'


class RecordedLayoutTests(unittest.TestCase):
    def setUp(self):
        self.adapter = builtins().get('liepin')

    def parse(self, html=None, url=URL):
        return self.adapter.detail(PageSnapshot(url, markup() if html is None else html))

    def test_baseline_generic_parser_reproduces_zero_data(self):
        with self.assertRaises(CrawlError) as error:
            DOMAdapter.detail(self.adapter, PageSnapshot(URL, markup()))
        self.assertEqual(error.exception.code, 'structure_changed')

    def test_literal_newline_and_missing_h1_now_preserve_complete_body(self):
        result = self.parse()
        self.assertEqual(result['title'], TITLE)
        self.assertEqual(result['text'], BODY)
        self.assertEqual(result['company'], '合成测试企业')
        self.assertEqual(result['published_at'], '2026-01-01')
        self.assertEqual(result['parser'], 'liepin:job_intro_jsonld:v1')

    def test_valid_json_with_dd_also_does_not_need_h1(self):
        self.assertEqual(self.parse(markup(raw_breaks=False))['text'], BODY)

    def test_unheaded_numbered_qualifications_require_complete_structured_agreement(self):
        for breaks in (False, True):
            with self.subTest(raw_breaks=breaks):
                result = self.parse(markup(posting(description=NUMBERED), body=NUMBERED, raw_breaks=breaks))
                self.assertEqual(result['text'], NUMBERED)
                self.assertEqual(result['title'], TITLE)
                self.assertEqual(result['parser'], 'liepin:job_intro_jsonld:v1')

    def test_unheaded_prefix_or_absent_visible_intro_does_not_gain_new_acceptance(self):
        for html in (markup(posting(description=NUMBERED[:35]), body=NUMBERED),
                     markup(posting(description=NUMBERED), body=NUMBERED, anchor=False),
                     '<h1>'+TITLE+'</h1><dl><dt>职位介绍</dt><dd>'+NUMBERED+'</dd></dl>'):
            with self.subTest(html=html[:40]), self.assertRaises(CrawlError):
                self.parse(html)

    def test_ordinary_incomplete_or_nonconsecutive_lists_are_not_qualifications(self):
        for body in (NUMBERED.replace('2.掌握', '3.掌握'), NUMBERED.replace('3.了解', '5.了解'),
                     '\n'.join(NUMBERED.splitlines()[:3]), NUMBERED.replace('4.具备', '4.公司成立于'),
                     '1.公司不断发展，持续提供优质服务。\n2.业务遍布各地，客户群体不断扩大。\n3.品牌持续创新，产品覆盖多个市场。'):
            with self.subTest(body=body[:30]), self.assertRaises(CrawlError):
                self.parse(markup(posting(description=body), body=body))

    def test_numbered_format_keeps_identity_truncation_and_foreign_content_rejections(self):
        for data, body in ((posting(description=NUMBERED,url=URL.replace('123','456')), NUMBERED),
                           (posting(description=NUMBERED+'\n展开全部'), NUMBERED+'\n展开全部'),
                           (posting(description=NUMBERED+'\n公司简介：其他内容'), NUMBERED+'\n公司简介：其他内容')):
            with self.subTest(body=body[-20:]), self.assertRaises(CrawlError):
                self.parse(markup(data, body=body))

    def test_repaired_structured_description_without_dd(self):
        parsed = self.parse(markup(anchor=False))
        self.assertEqual(parsed['text'], BODY)
        self.assertEqual(parsed['parser'], 'liepin:jsonld_string_whitespace:v1')

    def test_existing_valid_json_path_keeps_parser(self):
        parsed = self.parse(markup(raw_breaks=False, anchor=False))
        self.assertEqual(parsed['parser'], 'json_ld_jobposting')

    def test_ordinary_escaped_newlines_do_not_change_valid_json(self):
        raw = json.dumps(posting(), ensure_ascii=False)
        self.assertEqual(_jsonld_whitespace(raw), (raw, False))

    def test_literal_cr_lf_tab_only_repaired_inside_strings(self):
        raw = '{\r\n"x":"a\r\nb\tc"\t}'
        result, repaired = _jsonld_whitespace(raw)
        self.assertTrue(repaired)
        self.assertEqual(json.loads(result), {'x': 'a\r\nb\tc'})
        self.assertTrue(result.startswith('{\r\n'))

    def test_quotes_and_backslashes_remain_exact(self):
        title = '算法 "预测" \\ 模型'
        self.assertEqual(self.parse(markup(posting(title=title)))['title'], title)

    def test_single_posting_without_identity_is_compatible(self):
        data = posting(); del data['url']
        self.assertEqual(self.parse(markup(data))['text'], BODY)

    def test_query_variants_are_same_entity_not_false_conflict(self):
        data = posting(mainEntityOfPage={'@id': URL + '?d_sfrom=metadata'})
        self.assertEqual(self.parse(markup(data), URL+'?d_sfrom=selected')['text'], BODY)

    def test_headhunter_url_family_supported_but_never_aliased(self):
        url = URL.replace('/job/', '/a/')
        self.assertEqual(self.parse(markup(posting(url=url)), url)['text'], BODY)
        with self.assertRaises(CrawlError):
            self.parse(markup(posting(url=url)))

    def test_recommendation_first_does_not_override_selected_identity(self):
        other = posting(url=URL.replace('123','456'), title='推荐的其他岗位')
        self.assertEqual(self.parse(markup([other, posting()]))['title'], TITLE)

    def test_graph_and_duplicate_identical_blocks(self):
        data = {'@graph': [posting(), posting()]}
        self.assertEqual(self.parse(markup(data))['text'], BODY)

    def test_array_type_jobposting_is_supported(self):
        self.assertEqual(self.parse(markup(posting(**{'@type': ['Thing','JobPosting']})))['text'], BODY)

    def test_different_jobs_without_explicit_match_fail(self):
        for data in [posting(url=URL.replace('123','456')),
                     [posting(url=None), posting(url=None, title='歧义岗位')],
                     posting(mainEntityOfPage=URL.replace('123','456'))]:
            with self.subTest(data=data), self.assertRaises(CrawlError):
                self.parse(markup(data))

    def test_repaired_identity_conflict_cannot_fall_back_to_dom(self):
        with self.assertRaises(CrawlError):
            self.parse(markup(posting(url=URL.replace('123','456'))) + '<h1>伪标题</h1><div class="job-description">'+BODY+'</div>')

    def test_duplicate_json_field_is_not_silently_dropped(self):
        html = markup().replace('"title": "'+TITLE+'"', '"title": "冲突", "title": "'+TITLE+'"')
        with self.assertRaises(CrawlError): self.parse(html)

    def test_other_raw_control_truncated_json_and_nan_fail(self):
        for replacement in ['"description": "\x00'+BODY+'"',
                            '"description": NaN', '"description": "unterminated']:
            raw = '{"@type":"JobPosting","title":"测试",'+replacement+'}'
            html = '<script type="application/ld+json">'+raw+'</script>' + markup().split('<body>')[1]
            with self.subTest(replacement=replacement), self.assertRaises(CrawlError): self.parse(html)

    def test_escaped_control_is_also_not_saved(self):
        with self.assertRaises(CrawlError):
            self.parse(markup(posting(description=BODY+'\x00'), anchor=False))

    def test_multiple_introductions_rejected(self):
        extra='<dd data-selector="job-intro-content">'+BODY+'</dd>'
        with self.assertRaises(CrawlError): self.parse(markup()+extra)

    def test_hidden_only_anchor_does_not_use_invisible_full_body(self):
        for attrs in ['hidden', 'aria-hidden="true"', 'style="display:none"']:
            with self.subTest(attrs=attrs), self.assertRaises(CrawlError): self.parse(markup(attrs=attrs))

    def test_anchor_inside_recommendation_or_hidden_parent_rejected(self):
        for tag in ['aside', 'template']:
            html=markup().replace('<section class="job-intro-container">', '<'+tag+'>').replace('</section>', '</'+tag+'>')
            with self.subTest(tag=tag), self.assertRaises(CrawlError): self.parse(html)

    def test_unexpanded_or_login_description_rejected(self):
        for marker in ['展开全部', '登录后查看完整职位', '查看完整职位']:
            body=BODY+marker
            with self.subTest(marker=marker), self.assertRaises(CrawlError):
                self.parse(markup(posting(description=body), body))

    def test_unrelated_metadata_body_does_not_mix(self):
        body='岗位职责：销售汽车和机械产品，维护客户关系，开展市场推广和商务合作。任职要求：三年以上行业销售经验。'
        with self.assertRaises(CrawlError) as error: self.parse(markup(body=body))
        self.assertEqual(error.exception.code, 'jd_incomplete')

    def test_full_visible_description_wins_over_structured_prefix(self):
        value=posting(description=BODY[:35])
        self.assertEqual(self.parse(markup(value))['text'], BODY)

    def test_unexpanded_structured_prefix_is_not_full_body(self):
        body=BODY+'展开更多'
        with self.assertRaises(CrawlError): self.parse(markup(posting(description=BODY),body))

    def test_unrelated_sections_not_in_body(self):
        html=markup()+'<aside><h1>推荐岗位</h1>推荐职位：不同工作</aside><footer>公司信息</footer>'
        self.assertEqual(self.parse(html)['text'],BODY)

    def test_zero_ai_body_remains_successful_acquisition(self):
        body='岗位职责：负责时间序列预测模型与离线评估。\n任职要求：熟悉Python、统计学和回归分析，能编写测试和维护实验记录。'
        self.assertEqual(self.parse(markup(posting(description=body),body))['text'],body)

    def test_observed_work_functions_and_qualifications_labels_in_structured_intro(self):
        for label in ('工作职能', '任职资格'):
            body=label+'：负责合成系统的模块设计，熟悉数据库建模、软件测试和版本管理，能够维护人工回归项目的技术文档。'
            with self.subTest(label=label):
                self.assertEqual(self.parse(markup(posting(description=body),body))['text'],body)

    def test_observed_labels_also_work_in_bounded_semantic_intro(self):
        body='工作职能：负责合成系统的模块设计和测试。任职资格：熟悉软件建模和版本管理，能够维护人工回归项目的技术文档。'
        html='<h1>合成架构师</h1><dl><dt>职位介绍</dt><dd>'+body+'</dd></dl>'
        self.assertEqual(semantic_detail(html)['text'],body)

    def test_generic_qualification_word_does_not_make_promotional_text_a_job(self):
        body='企业资格认证与品牌推广活动，欢迎了解本公司的发展历史和文化。'*4
        with self.assertRaises(CrawlError):self.parse(markup(posting(description=body),body))
        with self.assertRaises(CrawlError):
            semantic_detail('<h1>合成架构师</h1><dl><dt>职位介绍</dt><dd>'+body+'</dd></dl>')

    def test_company_information_department_is_a_duty_but_information_panel_stays_rejected(self):
        body='岗位职责：与公司信息部协作，完成合成系统的模块设计和测试。任职要求：熟悉软件建模和版本管理，维护人工项目技术文档。'
        self.assertEqual(self.parse(markup(posting(description=body),body))['text'],body)
        self.assertEqual(semantic_detail('<h1>合成架构师</h1><dl><dt>职位介绍</dt><dd>'+body+'</dd></dl>')['text'],body)
        for extra in ('公司信息：宣传资料', '推荐职位：其他岗位'):
            other=body+extra
            with self.subTest(extra=extra), self.assertRaises(CrawlError):
                self.parse(markup(posting(description=other),other))
            with self.subTest(extra=extra), self.assertRaises(CrawlError):
                semantic_detail('<h1>合成架构师</h1><dl><dt>职位介绍</dt><dd>'+other+'</dd></dl>')

    def test_missing_title_cannot_be_guessed_from_page_title(self):
        with self.assertRaises(CrawlError): self.parse(markup(posting(title=''))+'<title>伪标题</title>')

    def test_canonical_mismatch_is_still_rejected_before_new_parser(self):
        with self.assertRaises(CrawlError): self.parse(markup()+'<link rel="canonical" href="/job/456.shtml">')

    def test_size_and_nesting_limits(self):
        with self.assertRaises(CrawlError): self.parse('x'*5_000_001)
        with self.assertRaises(CrawlError): self.parse('<script type="application/ld+json">'+(' '*1_000_001)+'</script>')


class RecordedLayoutPipelineTests(unittest.TestCase):
    def test_selected_complete_body_and_rejected_item_reach_original_batch_report(self):
        class Backend:
            def __init__(self,*args): self.page=None
            def open(self,url,authentication=False):
                if '/zhaopin/' in url:
                    html='<a href="/job/456.shtml">歧义正文</a><a href="/job/123.shtml">'+TITLE+'</a>'
                elif '456' in url:
                    html=markup(posting(url=url), '不属于该岗位的正文内容。'*10)
                else: html=markup()
                self.page=PageSnapshot(url,html); return self.page
            def snapshot(self): return self.page
            def next_page(self): return False
            def pump(self): pass
            def close(self): pass
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Workspace(Path(tmp))
            service=GuidedService(workspace,registry=Registry([builtins().get('liepin')]),backend_factory=Backend)
            def wait():
                deadline=time.monotonic()+10
                while service.state()['busy'] and time.monotonic()<deadline: time.sleep(.01)
                self.assertFalse(service.state()['busy'])
            try:
                ident=service.create({'platform':'liepin','keyword':TITLE,'roles':['time_series'],
                    'consent':True,'rights_note':'独立合成回归，非实站认证','max_pages':1,'max_jobs':2})['id']
                wait(); state=service._load(ident)
                service.action({'id':ident,'action':'collect','selected':[c['id'] for c in state['cards']]})
                wait(); state=service._load(ident)
                self.assertEqual([c['status'] for c in state['cards']],['jd_incomplete','ok'])
                self.assertEqual(state['outcome']['saved'],1)
                self.assertEqual(state['outcome']['failed'],1)
                self.assertTrue(state['report_id'])
                with Store(workspace.db) as store: records=store.records()
                self.assertEqual(len(records),1); self.assertEqual(records[0].text,BODY)
                self.assertEqual(records[0].title,TITLE)
                audit=json.loads(workspace.report_file(state['report_id'],'guided_acquisition.json').read_text(encoding='utf-8'))
                item=next(x for x in audit['items'] if x['status']=='ok')
                self.assertEqual(item['parser'],'liepin:job_intro_jsonld:v1')
                self.assertEqual(item['body_sha256'],hashlib.sha256(BODY.encode()).hexdigest())
                self.assertEqual(state['certification'],'not_live_verified')
            finally: service.close()


    def test_observed_labels_and_department_preserve_full_body_in_original_pipeline(self):
        body = ('工作职能：与公司信息部协作，负责合成时间序列系统的设计与自动测试。'
                '任职资格：熟悉数据库建模和版本管理，使用Cursor辅助开发并审查生成代码。')
        for representation, html, parser in (
                ('structured', markup(posting(description=body), body), 'liepin:job_intro_jsonld:v1'),
                ('semantic', '<h1>'+TITLE+'</h1><dl><dt>职位介绍</dt><dd>'+body+'</dd></dl>', 'liepin:semantic_intro:v1')):
            with self.subTest(representation=representation), tempfile.TemporaryDirectory() as tmp:
                class Backend:
                    def __init__(self, *args): self.page = None
                    def open(self, url, authentication=False):
                        content = '<a href="/job/123.shtml">'+TITLE+'</a>' if '/zhaopin/' in url else html
                        self.page = PageSnapshot(url, content)
                        return self.page
                    def snapshot(self): return self.page
                    def next_page(self): return False
                    def pump(self): pass
                    def close(self): pass
                workspace = Workspace(Path(tmp))
                service = GuidedService(workspace, registry=Registry([builtins().get('liepin')]), backend_factory=Backend)
                def wait():
                    deadline = time.monotonic()+10
                    while service.state()['busy'] and time.monotonic() < deadline: time.sleep(.01)
                    self.assertFalse(service.state()['busy'])
                try:
                    ident = service.create({'platform':'liepin','keyword':TITLE,'roles':['time_series'],
                        'consent':True,'rights_note':'独立人工正文；不联网、不作实站认证','max_pages':1,'max_jobs':1})['id']
                    wait(); state = service._load(ident)
                    service.action({'id':ident,'action':'collect','selected':[state['cards'][0]['id']]})
                    wait(); state = service._load(ident)
                    self.assertEqual(state['status'], 'completed')
                    self.assertEqual(state['cards'][0]['status'], 'ok')
                    self.assertEqual(state['outcome']['saved'], 1)
                    self.assertEqual(state['outcome']['failed'], 0)
                    with Store(workspace.db) as store: records = store.records()
                    self.assertEqual(len(records), 1)
                    self.assertEqual(records[0].text, body)
                    self.assertEqual(records[0].title, TITLE)
                    report = workspace.report(state['report_id'])
                    self.assertEqual(report['manifest']['stats']['full_text_job_groups'], 1)
                    audit = json.loads(workspace.report_file(state['report_id'],'guided_acquisition.json').read_text(encoding='utf-8'))
                    item = next(x for x in audit['items'] if x['status']=='ok')
                    self.assertEqual(item['parser'], parser)
                    self.assertEqual(item['body_sha256'], hashlib.sha256(body.encode()).hexdigest())
                    self.assertEqual(state['certification'], 'not_live_verified')
                finally: service.close()



    def test_observed_labels_continue_saved_category_list_into_separate_original_report(self):
        from unittest.mock import patch
        from vibe_job_radar.collection import Collector, TERMINAL
        from vibe_job_radar.network import SiteFetcher
        from test_public_category import Wire, card, data, listing, job_url
        body = ('工作职能：与公司信息部协作，负责软件架构与数据库系统设计和自动测试。'
                '任职资格：熟悉分布式系统与版本管理，使用Cursor辅助开发并审查生成代码。')
        title = '软件架构师人工样本2'
        url = job_url(2)
        for representation, html, parser in (
                ('structured', markup(posting(url=url, title=title, description=body), body), 'liepin:job_intro_jsonld:v1'),
                ('semantic', '<h1>'+title+'</h1><dl><dt>职位介绍</dt><dd>'+body+'</dd></dl>', 'liepin:semantic_intro:v1')):
            with self.subTest(representation=representation), tempfile.TemporaryDirectory() as tmp:
                workspace = Workspace(Path(tmp))
                collector = Collector(workspace)
                def finish(state, wire):
                    collector.clients[(state['id'], 'liepin')] = SiteFetcher({'liepin.com'}, transport=wire)
                    for _ in range(12):
                        state = collector.step({'id':state['id']})
                        if state['status'] in TERMINAL:
                            return state
                    self.fail('artificial category batch did not reach a terminal state')
                parent = finish(collector.start(data(detail_budget=1)), Wire(listing(card(1)+card(2))))
                self.assertEqual(parent['status'], 'completed')
                parent_bytes = collector._path(parent['id']).read_bytes()
                original = workspace.root/'reports'/parent['report_id']
                before = {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in original.iterdir() if p.is_file()}
                with patch('vibe_job_radar.collection.SiteFetcher', side_effect=AssertionError('preview/start must not fetch')):
                    preview = collector.category_next_preview({'id':parent['id']})
                    self.assertEqual([item['position'] for item in preview['items']], [2])
                    self.assertEqual(preview['external_network_requests'], 0)
                    result = collector.category_next_start(dict(id=parent['id'], fingerprint=preview['fingerprint'], consent=True))
                wire = Wire(details={url:html})
                child = finish(result['task'], wire)
                self.assertEqual(child['status'], 'completed')
                self.assertEqual(child['category_attempts'], 0)
                self.assertEqual(child['detail_attempts'], 1)
                self.assertEqual(wire.calls, ['https://www.liepin.com/robots.txt', url])
                self.assertEqual([item['status'] for item in child['details']], ['ok'])
                self.assertNotEqual(child['report_id'], parent['report_id'])
                with Store(workspace.db) as store:
                    records = store.records()
                self.assertEqual(len(records), 2)
                current = next(record for record in records if record.url==url)
                self.assertEqual(current.title, title)
                self.assertEqual(current.text, body)
                self.assertEqual(current.parser, 'liepin_public_detail_v1:'+parser)
                self.assertEqual(current.record_id, child['details'][0]['record_id'])
                report = workspace.report(child['report_id'])
                self.assertEqual(report['manifest']['stats']['full_text_job_groups'], 1)
                self.assertEqual(report['manifest']['stats']['current_source_records'], 1)
                audit = json.loads(workspace.report_file(child['report_id'], 'collection_manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(audit['category_outcomes'][0]['selected_positions'], [2])
                self.assertEqual(audit['details'][0]['record_id'], current.record_id)
                self.assertEqual(collector._path(parent['id']).read_bytes(), parent_bytes)
                self.assertEqual(before, {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in original.iterdir() if p.is_file()})



class StructuredJobLocationTests(unittest.TestCase):
    """Only the selected JobPosting supplies location; all examples authored."""
    def parse_location(self, value, mode='recorded', url=URL):
        from vibe_job_radar.html_parser import parse_job_html
        if mode == 'generic':
            return parse_job_html(markup(value, raw_breaks=False, anchor=False), source_url=url)
        return builtins().get('liepin').detail(PageSnapshot(
            url, markup(value, raw_breaks=mode == 'recorded')))

    @staticmethod
    def place(city, region='人工省', country='CN', **extra):
        return {'@type':'Place', 'address':{'@type':'PostalAddress',
            'addressCountry':country, 'addressRegion':region,
            'addressLocality':city, **extra}}


    def test_surrogate_optional_location_does_not_discard_complete_jd(self):
        from vibe_job_radar.html_parser import parse_job_html
        from vibe_job_radar.models import JobRecord
        for mode in ('generic','recorded'):
            for bad in ('\ud800','\udfff'):
                for value in (self.place(bad), {'address':bad}):
                    with self.subTest(mode=mode,code_point=ord(bad),address_kind=type(value['address']).__name__), tempfile.TemporaryDirectory() as tmp:
                        html=markup(posting(jobLocation=value),raw_breaks=False,anchor=mode=='recorded')
                        html=html.replace(bad,'\\u'+format(ord(bad),'04x'))
                        parsed=(parse_job_html(html,source_url=URL) if mode=='generic' else
                                builtins().get('liepin').detail(PageSnapshot(URL,html)))
                        record=JobRecord(**parsed,url=URL)
                        with Store(Path(tmp)/'jobs.sqlite') as store:
                            store.add(record)
                            saved=store.records()[0]
                        self.assertEqual(saved.location,'')
                        self.assertEqual(saved.text,BODY)

    def test_equivalent_multiplace_case_and_width_share_existing_fingerprint(self):
        from vibe_job_radar.models import JobRecord
        variants=[['alpha','Beta'],['Alpha','beta'],['ＡＬＰＨＡ','Ｂｅｔａ'],
                  ['Beta','alpha','ＡＬＰＨＡ','beta']]
        for mode in ('generic','recorded'):
            with self.subTest(mode=mode):
                records=[]
                for names in variants:
                    value=posting(jobLocation=[{'address':name} for name in names])
                    records.append(JobRecord(**self.parse_location(value,mode),url=URL))
                self.assertEqual(len({r.fingerprint for r in records}),1)
                self.assertTrue(all(len(r.location.split('; '))==2 for r in records))

    def test_address_fields_retained_in_all_structured_representations(self):
        data=posting(jobLocation=self.place('人工城市', streetAddress='合成路1号', postalCode='000001'))
        for mode in ('generic', 'recorded', 'valid_intro'):
            with self.subTest(mode=mode):
                parsed=self.parse_location(data, mode)
                self.assertEqual(parsed['location'], 'CN / 人工省 / 人工城市 / 合成路1号 / 000001')
                self.assertEqual(parsed['title'], TITLE)
                self.assertEqual(parsed['text'], BODY)

    def test_selected_identity_location_never_comes_from_recommendation(self):
        selected=posting(jobLocation=self.place('甲市'))
        recommendation=posting(url=URL.replace('123','456'), jobLocation=self.place('乙市'))
        for mode in ('generic', 'recorded'):
            with self.subTest(mode=mode):
                self.assertEqual(self.parse_location([recommendation, selected], mode)['location'],
                                 'CN / 人工省 / 甲市')

    def test_missing_location_is_not_inferred_from_other_metadata(self):
        data=posting(jobLocationType='TELECOMMUTE',
            applicantLocationRequirements={'@type':'Country','name':'CN'},
            hiringOrganization={'name':'合成测试企业','address':self.place('总部')['address']})
        other=posting(url=URL.replace('123','456'), jobLocation=self.place('乙市'))
        for mode in ('generic', 'recorded'):
            with self.subTest(mode=mode):
                self.assertEqual(self.parse_location([other, data], mode).get('location',''), '')

    def test_multiple_places_have_canonical_order_and_no_repeated_place(self):
        a,b=self.place('甲市'),self.place('乙市')
        for mode in ('generic', 'recorded'):
            with self.subTest(mode=mode):
                first=self.parse_location(posting(jobLocation=[a,b,a]), mode)
                second=self.parse_location(posting(jobLocation=[b,a]), mode)
                self.assertEqual(first['location'], '; '.join(sorted(['CN / 人工省 / 甲市','CN / 人工省 / 乙市'])))
                self.assertEqual(first['location'], second['location'])

    def test_explicit_text_address_and_named_country_region_are_supported(self):
        values=[
            ({'@type':'Place','address':' 合成地区 \n 合成街道 '}, '合成地区 合成街道'),
            (self.place('人工城市',region={'@type':'AdministrativeArea','name':'合成省'},
                        country={'@type':'Country','name':'人工国'}), '人工国 / 合成省 / 人工城市'),
            (self.place('甲市',region='甲市',country=''), '甲市')]
        for value,expected in values:
            with self.subTest(value=value):
                self.assertEqual(self.parse_location(posting(jobLocation=value))['location'],expected)

    def test_invalid_or_unbounded_optional_location_stays_unknown(self):
        values=[None,[],True,'甲市',{}, {'address':True},
            self.place(['甲市']),self.place('甲市\u0000'),self.place('a'*513),
            [self.place(str(i)) for i in range(17)],
            self.place('a'*512,region='b'*512,country='c'*512,streetAddress='d'*512,postalCode='e'*512)]
        for mode in ('generic','recorded'):
            for value in values:
                with self.subTest(mode=mode,value_type=type(value).__name__):
                    parsed=self.parse_location(posting(jobLocation=value), mode)
                    self.assertEqual(parsed.get('location',''),'')
                    self.assertEqual(parsed['text'],BODY)

    def test_partial_place_list_never_silently_discards_unknown_member(self):
        for unknown in ({},None,{'address':{'addressLocality':123}}):
            with self.subTest(unknown=unknown):
                self.assertEqual(self.parse_location(posting(jobLocation=[self.place('甲市'),unknown])).get('location',''),'')

    def test_location_does_not_relax_selected_identity_check(self):
        from vibe_job_radar.html_parser import ParseError
        value=posting(url=URL.replace('123','456'),jobLocation=self.place('甲市'))
        with self.assertRaises(ParseError):
            self.parse_location(value,'generic')
        with self.assertRaises(CrawlError):
            self.parse_location(value,'recorded')

    def test_distinct_cities_remain_separate_in_original_store_and_report(self):
        from vibe_job_radar.models import JobRecord
        from vibe_job_radar.pipeline import analyze
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Workspace(Path(tmp))
            records=[]
            for number,city in ((123,'甲市'),(456,'乙市'),(789,'甲市')):
                url=URL.replace('123',str(number))
                parsed=self.parse_location(posting(url=url,jobLocation=self.place(city)),url=url)
                records.append(JobRecord(**parsed,url=url,platform='liepin',source_mode='manual',
                                         rights_note='independently authored test fixture'))
            with Store(workspace.db) as store:
                for record in records:store.add(record)
                self.assertEqual({r.location for r in store.records()},
                                 {'CN / 人工省 / 甲市','CN / 人工省 / 乙市'})
            output=workspace.root/'location-report'
            analyze(workspace.db,output)
            manifest=json.loads((output/'run_manifest.json').read_text(encoding='utf8'))
            self.assertEqual(manifest['stats']['selected_source_records'],3)
            self.assertEqual(manifest['stats']['full_text_job_groups'],2)
            self.assertNotEqual(records[0].fingerprint,records[1].fingerprint)
            self.assertEqual(records[0].fingerprint,records[2].fingerprint)

    def test_fresh_location_snapshot_keeps_identity_and_prior_report_immutable(self):
        from vibe_job_radar.models import JobRecord
        from vibe_job_radar.pipeline import analyze
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Workspace(Path(tmp))
            common=dict(url=URL,platform='liepin',source_mode='manual',
                        rights_note='independently authored test fixture')
            before=JobRecord(**self.parse_location(posting()),**common,
                             collected_at='2026-10-01T00:00:00+00:00')
            with Store(workspace.db) as store:store.add(before)
            old=workspace.root/'previous-report';analyze(workspace.db,old)
            hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in old.iterdir() if p.is_file()}
            after=JobRecord(**self.parse_location(posting(jobLocation=self.place('甲市'))),**common,
                            collected_at='2026-10-02T00:00:00+00:00')
            self.assertEqual(before.record_id,after.record_id)
            with Store(workspace.db) as store:
                store.add(after)
                self.assertEqual(store.records()[0].location,'CN / 人工省 / 甲市')
            self.assertEqual(hashes,{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in old.iterdir() if p.is_file()})

if __name__=='__main__': unittest.main()
