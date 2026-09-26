"""Synthetic unheaded work/qualification sections; no real JD or network."""
import unittest
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from test_liepin_recorded_layout import markup, posting, URL, TITLE

WORK = [
    '平台设计：设计合成数据处理平台，制定清晰的模块边界并记录接口约定。',
    '服务实现：搭建内部任务服务，开发失败告警并维护可复核的回归用例。',
    '团队建设：带领研发成员完成技术评审，推动发布检查和知识分享。',
]
QUALIFICATIONS = [
    '具备分布式软件实践经验，能够分析故障并说明技术方案的取舍。',
    '熟悉Python与关系数据库，能够核对自动生成代码并编写测试。',
]
BODY = '\n'.join(WORK + QUALIFICATIONS)

class LabelledIntroductionTests(unittest.TestCase):
    def parse(self, body=BODY, **options):
        data = options.pop('data', posting(description=body))
        return builtins().get('liepin').detail(PageSnapshot(URL, markup(data, body=body, **options)))

    def test_complete_corroborated_sections_preserve_text_and_title(self):
        for body in (BODY, '"'+BODY+'"', BODY.replace('：', ':')):
            for breaks in (True, False):
                with self.subTest(quoted=body.startswith('"'), raw_breaks=breaks):
                    result=self.parse(body, raw_breaks=breaks)
                    self.assertEqual(result['text'],body)
                    self.assertEqual(result['title'],TITLE)
                    self.assertEqual(result['parser'],'liepin:job_intro_jsonld:v1')

    def test_structured_prefix_or_absent_visible_intro_still_refused(self):
        for options in ({'data':posting(description=WORK[0])}, {'anchor':False}, {'attrs':'hidden'}):
            with self.subTest(options=options),self.assertRaises(CrawlError):self.parse(**options)

    def test_sections_without_multiple_explicit_qualifications_refused(self):
        for lines in (WORK, WORK+QUALIFICATIONS[:1], WORK+[QUALIFICATIONS[0]]*2):
            with self.subTest(lines=len(lines)),self.assertRaises(CrawlError):self.parse('\n'.join(lines))

    def test_distinct_work_sections_and_action_semantics_required(self):
        bodies = [
            '\n'.join(WORK[:2]+QUALIFICATIONS),
            '\n'.join([line.split('：',1)[1] for line in WORK]+QUALIFICATIONS),
            '\n'.join(['平台设计：'+line.split('：',1)[1] for line in WORK]+QUALIFICATIONS),
            '\n'.join(['品牌故事：品牌拥有广泛影响并得到全国市场关注和认可。',
                        '发展历史：机构经历多个阶段并持续获得各类品牌荣誉。',
                        '企业风貌：园区风景优美且生活设施齐全并提供特色活动。']+QUALIFICATIONS),
        ]
        for body in bodies:
            with self.subTest(body=body[:20]),self.assertRaises(CrawlError):self.parse(body)

    def test_new_format_keeps_identity_foreign_and_incomplete_guards(self):
        for options in ({'data':posting(description=BODY,url=URL.replace('123','456'))},
                        {'body':BODY+'\n展开全部'}, {'body':BODY+'\n公司简介：额外内容'},
                        {'body':BODY+'\n推荐职位：额外内容'}):
            with self.subTest(options=str(options)[:40]),self.assertRaises(CrawlError):self.parse(**options)

    def test_oversized_unheaded_section_list_does_not_gain_acceptance(self):
        with self.assertRaises(CrawlError):self.parse('\n'.join((WORK+QUALIFICATIONS)*21))
