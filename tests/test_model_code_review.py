"""Authored clauses for explicit use of language models to review code."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_core import CONF,NOW,job
from vibe_job_radar.extract import RuleExtractor
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.store import Store


class ModelCodeReviewTests(unittest.TestCase):
    POSITIVE='利用大语言模型辅助完成代码审查与缺陷定位。'

    def rows(self,text,**kwargs):
        record=job(text,**kwargs)
        rows=RuleExtractor(CONF).extract(record,['architect'])
        self.assertTrue(all(record.text[r.start:r.end]==r.quote for r in rows))
        return rows

    def test_explicit_model_review_is_direct_without_inventing_named_tools(self):
        rows=self.rows('岗位职责\n'+self.POSITIVE)
        self.assertEqual({r.capability for r in rows},{'ai_coding','testing_review'})
        self.assertTrue(all(r.accepted and r.positive and r.relation=='direct' for r in rows))
        self.assertTrue(all(r.strength=='expected' for r in rows))
        self.assertTrue(all(r.quote==self.POSITIVE and not r.tools for r in rows))

    def test_models_and_use_verbs_preserve_the_same_review_meaning(self):
        for model in ('AI','LLM','Claude','大语言模型'):
            for verb in ('使用','运用','借助'):
                with self.subTest(model=model,verb=verb):
                    rows=self.rows(f'任职要求\n{verb} {model} 进行深度代码评审。')
                    self.assertEqual({r.capability for r in rows},{'ai_coding','testing_review'})
                    self.assertTrue(all(r.accepted and not r.tools for r in rows))

    def test_candidate_and_same_clause_action_prefixes_are_supported(self):
        for prefix in ('候选人必须','应聘者需要熟练','负责接口治理，','职责：'):
            with self.subTest(prefix=prefix):
                rows=self.rows(prefix+self.POSITIVE)
                self.assertTrue(any(r.capability=='ai_coding' and r.accepted for r in rows))

    def test_model_knowledge_products_and_token_suffixes_are_not_usage(self):
        for text in (
            '熟悉大语言模型和代码审查方法。',
            '负责开发利用大语言模型辅助完成代码审查的产品。',
            '使用大语言模型进行代码审查工具开发。',
            '利用大语言模型进行代码评审产品设计。',
            '使用 LLMTools 进行代码审查。',
            '利用 AIOps 辅助进行代码评审。',
            '使用大语言模型的理论知识理解代码审查。',
            '使用大语言模型'+('有关事项'*6)+'进行代码审查。',
        ):
            with self.subTest(text=text):self.assertEqual(self.rows(text),[])

    def test_independent_clauses_cannot_join_a_model_to_review(self):
        for boundary in ('。','；',';','\n','，但必须'):
            with self.subTest(boundary=boundary):
                self.assertEqual(self.rows('使用大语言模型'+boundary+'进行代码审查。'),[])

    def test_negative_and_not_required_use_remains_negative(self):
        for prefix in ('不要求','无需','禁止','不得'):
            with self.subTest(prefix=prefix):
                rows=self.rows(prefix+self.POSITIVE)
                self.assertTrue(rows)
                self.assertTrue(all(not r.positive for r in rows))

    def test_snippets_and_ambiguous_dependence_stay_unaccepted(self):
        rows=self.rows(self.POSITIVE,evidence_level='snippet')
        self.assertTrue(rows);self.assertTrue(all(not r.accepted for r in rows))
        rows=self.rows('不能只'+self.POSITIVE)
        self.assertTrue(rows);self.assertTrue(all(not r.accepted for r in rows))

    def test_company_and_benefits_do_not_become_candidate_requirements(self):
        for heading in ('公司介绍','福利待遇'):
            with self.subTest(heading=heading):self.assertEqual(self.rows(heading+'\n'+self.POSITIVE),[])

    def test_custom_direct_patterns_can_disable_the_new_anchor(self):
        config=copy.deepcopy(CONF);config['direct_patterns']=[]
        self.assertEqual(RuleExtractor(config).extract(job(self.POSITIVE),['architect']),[])

    def test_reanalysis_keeps_original_text_old_report_and_config_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp)
            with Store(root/'jobs.sqlite') as store:store.add(job(self.POSITIVE))
            old=copy.deepcopy(CONF);old['direct_patterns']=[]
            analyze(root/'jobs.sqlite',root/'old',config=old,as_of=NOW)
            before={p.name:p.read_bytes() for p in (root/'old').iterdir() if p.is_file()}
            result=analyze(root/'jobs.sqlite',root/'new',config=CONF,as_of=NOW)
            self.assertEqual(result['stats']['accepted_positive_requirement_rows'],2)
            self.assertEqual(before,{p.name:p.read_bytes() for p in (root/'old').iterdir() if p.is_file()})
            rows=[json.loads(line) for line in (root/'new/requirements.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue(all(self.POSITIVE[r['start']:r['end']]==r['quote'] for r in rows))
            self.assertEqual(json.loads((root/'new/effective_config.json').read_text(encoding='utf-8'))['direct_patterns'],CONF['direct_patterns'])

    def test_compound_review_product_development_suffixes_are_not_model_use(self):
        for suffix in ('工作台开发', '工作流开发', '工作流程开发', '流程设计与产品实现',
                       '任务编排平台开发', '实践平台研发', '环节优化产品设计'):
            with self.subTest(suffix=suffix):
                self.assertEqual(self.rows('任职要求\n使用Claude进行代码审查' + suffix + '。'), [])

    def test_review_activity_nouns_require_a_complete_right_boundary(self):
        for suffix in ('', '工作', '任务', '实践', '流程', '环节', '工作流', '工作流程'):
            with self.subTest(suffix=suffix):
                rows = self.rows('任职要求\n使用Claude进行代码审查' + suffix + '，指导团队修改缺陷。')
                self.assertEqual({r.capability for r in rows}, {'ai_coding', 'testing_review'})
                self.assertTrue(all(r.accepted and r.positive and r.relation == 'direct' and not r.tools
                                    for r in rows))

    def test_mixed_jd_keeps_product_clauses_as_unaccepted_support(self):
        products = ('使用大语言模型进行代码审查工作台开发。',
                    '利用大语言模型进行代码审查流程设计与产品实现。',
                    '使用Claude开展代码审查工作流开发，构建自动审查工具。')
        text = '任职要求\n1、' + self.POSITIVE + '\n2、不要求' + self.POSITIVE
        text += ''.join(f'\n{i}、{clause}' for i, clause in enumerate(products, 3))
        rows = self.rows(text)
        support = [r for r in rows if r.quote in products]
        direct = [r for r in rows if r.quote not in products]
        self.assertEqual(len(support), 3)
        self.assertEqual({r.quote for r in support}, set(products))
        self.assertTrue(all(r.capability == 'testing_review' and r.relation == 'role_related'
                            and r.review_status == 'needs_review' and not r.accepted for r in support))
        self.assertEqual(len(direct), 4)
        self.assertTrue(all(r.relation == 'direct' and r.accepted for r in direct))
        self.assertEqual(sum(r.positive for r in direct), 2)
