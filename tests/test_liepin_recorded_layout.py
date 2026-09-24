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


if __name__=='__main__': unittest.main()
