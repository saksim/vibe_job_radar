"""Authored requirements, not captured job text: explicit model use with long context."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from test_core import CONF, NOW, job
from vibe_job_radar.extract import RuleExtractor
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.store import Store


class ExplicitAIApplicationTests(unittest.TestCase):
    POSITIVE = ('具备运用 AI 处理工程任务的能力：了解大语言模型的输入条件与响应特点，'
                '能够在接口设计、代码生成及代码评审环节有效使用。')

    def rows(self, text, **kwargs):
        record = job(text, **kwargs)
        result = RuleExtractor(CONF).extract(record, ['architect'])
        self.assertTrue(all(record.text[r.start:r.end] == r.quote for r in result))
        return result

    def test_explicit_application_with_intervening_model_knowledge_is_direct(self):
        rows = self.rows('任职要求\n' + self.POSITIVE)
        ai = [r for r in rows if r.capability == 'ai_coding']
        self.assertEqual(len(ai), 1)
        self.assertTrue(ai[0].accepted and ai[0].positive)
        self.assertEqual(ai[0].relation, 'direct')
        self.assertEqual(ai[0].quote, self.POSITIVE)
        self.assertEqual(ai[0].tools, [])

    def test_model_and_use_verbs_do_not_invent_a_named_coding_product(self):
        for model in ('AI', 'LLM', 'GPT', 'Claude', '大语言模型'):
            for verb in ('使用', '运用', '驾驭', '借助'):
                with self.subTest(model=model, verb=verb):
                    text = (f'具备{verb} {model} 解决工程问题的能力，理解输入限制及校验方式，'
                            '能在接口实现与代码评审阶段有效使用。')
                    rows = self.rows(text)
                    self.assertTrue(any(r.capability == 'ai_coding' and r.accepted for r in rows))
                    self.assertTrue(all(not r.tools for r in rows))
                    self.assertFalse(any(r.capability == 'tool_fluency' for r in rows))

    def test_code_review_synonym_is_attributed_to_the_original_clause(self):
        rows = self.rows('必须使用 Cursor。\n负责代码评审。')
        review = [r for r in rows if r.capability == 'testing_review']
        self.assertEqual(len(review), 1)
        self.assertEqual(review[0].quote, '负责代码评审。')
        self.assertEqual(review[0].relation, 'role_related')
        self.assertFalse(review[0].accepted)

    def test_unrelated_model_knowledge_and_building_a_product_are_not_usage(self):
        for text in (
            '具备 AI 产品研发知识，负责代码生成产品的设计与推广。',
            '熟悉大语言模型的输入条件及行业应用，能够在代码评审环节介绍其产品卖点。',
            '负责开发 AI 产品，使客户能够在接口实现与代码评审环节有效使用。',
            '公司提供 AI 工具，员工能够在接口设计与代码评审环节有效使用。',
            '具备运用 AI 进行财务分析的能力，掌握业务预测方法，能够在报表生成环节有效使用。',
            '负责打造具备运用 AI 处理工程任务的能力，能在代码评审环节有效使用的工具。',
        ):
            with self.subTest(text=text):
                self.assertEqual(self.rows(text), [])

    def test_candidate_requirement_prefix_is_not_a_product_subject(self):
        for prefix in ('必须', '需要', '应当', '候选人需', '应聘者必须'):
            with self.subTest(prefix=prefix):
                rows = self.rows(prefix + self.POSITIVE)
                self.assertTrue(any(r.capability == 'ai_coding' and r.accepted for r in rows))

    def test_independent_obligations_do_not_form_a_direct_anchor(self):
        first = '具备运用 AI 处理工程任务的能力'
        second = '能够在接口设计、代码生成及代码评审环节有效使用。'
        for boundary in ('。', '；', ';', '\n', '，但必须'):
            with self.subTest(boundary=boundary):
                self.assertEqual(self.rows(first + boundary + second), [])

    def test_long_or_incomplete_relations_are_not_accepted(self):
        for text in (
            '具备运用 AI 的能力，' + '有关信息' * 28 + '能在代码评审环节有效使用。',
            '具备运用 AI 的能力，能在' + '有关环节' * 12 + '代码评审中有效使用。',
            '具备运用 AI 的能力，能在代码评审' + '有关环节' * 12 + '有效使用。',
            '具备运用 AIOps 的能力，能在代码评审环节有效使用。',
            '具备运用 GPTSomething 的能力，能在代码评审环节有效使用。',
        ):
            with self.subTest(text=text):
                self.assertEqual(self.rows(text), [])

    def test_negation_and_snippets_keep_the_original_limits(self):
        for prefix in ('不要求', '无需', '禁止', '不得'):
            with self.subTest(prefix=prefix):
                rows = self.rows(prefix + self.POSITIVE)
                self.assertTrue(rows)
                self.assertTrue(all(not r.positive for r in rows))
        rows = self.rows(self.POSITIVE, evidence_level='snippet')
        self.assertTrue(rows)
        self.assertTrue(all(not r.accepted for r in rows))

    def test_company_benefits_and_ambiguous_dependence_stay_out_of_accepted(self):
        for heading in ('公司介绍', '福利待遇'):
            with self.subTest(heading=heading):
                self.assertEqual(self.rows(heading + '\n' + self.POSITIVE), [])
        rows = self.rows('不能只' + self.POSITIVE)
        self.assertTrue(rows)
        self.assertTrue(all(not r.accepted for r in rows))

    def test_custom_pattern_configuration_can_disable_the_new_anchor(self):
        config = copy.deepcopy(CONF)
        config['direct_patterns'] = []
        self.assertEqual(RuleExtractor(config).extract(job(self.POSITIVE), ['architect']), [])

    def test_new_analysis_preserves_old_report_and_exact_source(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with Store(root/'jobs.sqlite') as store:
                store.add(job(self.POSITIVE))
            old_config = copy.deepcopy(CONF)
            old_config['direct_patterns'] = []
            analyze(root/'jobs.sqlite', root/'old', config=old_config, as_of=NOW)
            before = {p.name:p.read_bytes() for p in (root/'old').iterdir() if p.is_file()}
            result = analyze(root/'jobs.sqlite', root/'new', config=CONF, as_of=NOW)
            self.assertGreater(result['stats']['accepted_positive_requirement_rows'], 0)
            self.assertEqual(before, {p.name:p.read_bytes() for p in (root/'old').iterdir() if p.is_file()})
            rows = [json.loads(line) for line in (root/'new/requirements.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue(rows)
            self.assertTrue(all(self.POSITIVE[r['start']:r['end']] == r['quote'] for r in rows))
            snapshot = json.loads((root/'new/effective_config.json').read_text(encoding='utf-8'))
            self.assertEqual(snapshot['direct_patterns'], CONF['direct_patterns'])
