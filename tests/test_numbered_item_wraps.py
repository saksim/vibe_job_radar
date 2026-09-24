"""Independently authored text, never a real job: soft wraps and exact evidence."""
import json
from pathlib import Path
import tempfile
import unittest

from test_core import CONF, NOW, job
from vibe_job_radar.extract import RuleExtractor, hard_constraints, spans
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.store import Store


class NumberedItemWrapTests(unittest.TestCase):
    def rows(self, text, **kwargs):
        record = job(text, **kwargs)
        rows = RuleExtractor(CONF).extract(record, ['architect'])
        self.assertTrue(all(record.text[r.start:r.end] == r.quote for r in rows))
        return rows

    @staticmethod
    def meanings(rows):
        return sorted((r.capability, r.relation, r.strength, tuple(r.tools), r.review_status) for r in rows)

    def test_numbered_continuation_has_the_complete_original_quote(self):
        for newline in ('\n', '\r\n'):
            for marker in ('1、', '2. ', '(3) ', '（4）', '5) '):
                with self.subTest(newline=repr(newline), marker=marker):
                    quote = '推动AI编程在研发小组中' + newline + '的应用，提升交付效率。'
                    text = '岗位职责' + newline + marker + quote
                    units = list(spans(text))
                    self.assertEqual(len(units), 1)
                    self.assertEqual(units[0].text, quote)
                    self.assertEqual(text[units[0].start:units[0].end], quote)
                    rows = self.rows(text)
                    self.assertTrue(rows)
                    self.assertTrue(all(r.quote == quote.replace('\r\n', '\n') for r in rows))
                    self.assertEqual(self.meanings(rows), self.meanings(self.rows(text.replace(quote, quote.replace(newline, '')))))

    def test_cjk_words_split_over_multiple_lines_match_without_changing_source(self):
        quote = '熟练使用 Cursor 辅助编\n程，开展代码审\n查和单元测试。'
        text = '任职要求\n1. ' + quote
        rows = self.rows(text)
        self.assertEqual(self.meanings(rows), self.meanings(self.rows(text.replace(quote, quote.replace('\n', '')))))
        self.assertTrue({'ai_coding', 'tool_fluency', 'testing_review'} <= {r.capability for r in rows})
        self.assertTrue(all(r.quote == quote and r.accepted for r in rows))

    def test_english_tools_keep_spaces_and_versions_within_a_chinese_item(self):
        quote = '熟练使用 Claude Code 2.1 在小组中\n的辅助编程流程，并开展代码审查。'
        rows = self.rows('1、' + quote)
        self.assertTrue(rows)
        self.assertTrue(all(r.tools == ['Claude Code'] and r.quote == quote for r in rows))
        self.assertEqual(self.rows('1、熟悉 Cur\nsor 的编程体验。'), [])

    def test_new_numbered_bulleted_or_unnumbered_obligation_stays_separate(self):
        for following in ('2、熟悉分布式架构', '（2）熟悉分布式架构', '- 熟悉分布式架构',
                          '• 熟悉分布式架构', '一、熟悉分布式架构', '熟悉分布式架构'):
            with self.subTest(following=following):
                rows = self.rows('1、熟练使用 Cursor 进行AI编程\n' + following)
                related = [r for r in rows if '分布式' in r.quote]
                self.assertTrue(related)
                self.assertTrue(all(r.relation == 'role_related' and not r.accepted for r in related))

    def test_separate_negations_do_not_change_the_positive_tool_obligation(self):
        for prefix in ('禁止', '不得', '无需', '不要求', '但不必', '同时不能', '并且不允许'):
            with self.subTest(prefix=prefix):
                rows = self.rows('1、熟练使用 Cursor 进行AI编程\n' + prefix + '使用 Codex。')
                self.assertTrue(any(r.tools == ['Cursor'] and r.accepted and r.positive for r in rows))
                self.assertTrue(any(r.tools == ['Codex'] and not r.positive for r in rows))
                self.assertFalse(any(len(r.tools) > 1 for r in rows))

    def test_comma_obligation_boundary_survives_a_wrap(self):
        text = '1、熟练使用 Cursor 在团队中\n的代码流程，禁止上传客户数据。'
        rows = self.rows(text)
        self.assertTrue(any(r.tools == ['Cursor'] and r.positive for r in rows))
        security = [r for r in rows if r.capability == 'security']
        self.assertTrue(security)
        self.assertTrue(all(r.quote == '禁止上传客户数据。' and not r.positive for r in security))

    def test_split_negation_is_not_a_new_positive_requirement(self):
        for negative in ('不要求', '不需要', '不必', '不强制', '不允许', '不能', '不得', '禁止', '严禁', '无需'):
            for position in range(1, len(negative)):
                with self.subTest(negative=negative, position=position):
                    unwrapped = '1、' + negative + '使用 Cursor。'
                    wrapped = '1、' + negative[:position] + '\n' + negative[position:] + '使用 Cursor。'
                    rows = self.rows(wrapped)
                    self.assertEqual(self.meanings(rows), self.meanings(self.rows(unwrapped)))
                    self.assertTrue(rows)
                    self.assertTrue(all(not r.positive and '\n' in r.quote for r in rows))

    def test_blank_line_sentence_heading_and_english_line_are_boundaries(self):
        for suffix, following in (('\n', '的单元测试流程'), ('。', '的单元测试流程'),
                                  (';', '的单元测试流程'), ('.', '的单元测试流程'),
                                  ('', '补充说明：普通测试流程'), ('', 'You must use Codex.')):
            with self.subTest(suffix=suffix, following=following):
                text = '1、熟练使用 Cursor 进行AI编程' + suffix + '\n' + following
                self.assertTrue(all('\n' not in s.text for s in spans(text)))

    def test_wrapped_known_headings_preserve_company_and_benefits_exclusion(self):
        for heading in ('【公司介绍】', '【福利待遇】', '【关于我们】'):
            with self.subTest(heading=heading):
                text = '1、熟练使用 Cursor 进行AI编程\n' + heading + '\n公司提供 Codex 会员。'
                rows = self.rows(text)
                self.assertTrue(rows)
                self.assertTrue(all(r.tools == ['Cursor'] for r in rows))
        text = '【福利待遇】\n公司提供 Cursor 会员。\n【任职要求】\n1、Codex 经验'
        rows = self.rows(text)
        self.assertTrue(rows)
        self.assertTrue(all(r.tools == ['Codex'] and r.strength == 'required' for r in rows))

    def test_unpunctuated_qualification_heading_is_not_a_continuation(self):
        text = '工作职能\n1、使用 Cursor 在小组中的应用\n任职资格\n2、分布式架构经验'
        units = list(spans(text))
        self.assertEqual([s.text for s in units], ['使用 Cursor 在小组中的应用', '分布式架构经验'])
        self.assertEqual([s.section for s in units], ['工作职能', '任职资格'])
        rows = self.rows(text)
        self.assertTrue(all('任职资格' not in r.quote for r in rows))
        self.assertTrue(all(not r.accepted for r in rows if '分布式' in r.quote))

    def test_unnumbered_or_english_only_paragraphs_are_not_joined(self):
        for text in ('使用 Cursor 在团队中\n的测试流程', '1. You must use Cursor\nfor code review.',
                     '1.2 是工具版本说明\n的补充', '一、熟练使用 Cursor 在团队中\n的测试流程'):
            with self.subTest(text=text):
                self.assertTrue(all('\n' not in s.text for s in spans(text)))

    def test_long_items_and_many_lines_keep_the_original_line_boundaries(self):
        for text in ('1、使用 Cursor 在团队中\n' + '的' * 2001,
                     '1、使用 Cursor 在团队中\n' + '\n'.join(['的流程'] * 8)):
            with self.subTest(size=len(text)):
                self.assertTrue(all('\n' not in s.text for s in spans(text)))

    def test_hard_constraint_matches_wrapped_word_with_exact_quote(self):
        quote = '具备3年架构设计经\r\n验。'
        text = '1、' + quote
        record = job(text)
        rows = hard_constraints(record, 'fixture-group', ['architect'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['category'], 'experience')
        self.assertEqual(rows[0]['quote'], quote.replace('\r\n', '\n'))
        self.assertEqual(record.text[rows[0]['start']:rows[0]['end']], rows[0]['quote'])

    def test_snippet_or_ambiguous_wrapped_evidence_is_still_review_only(self):
        text = '1、熟练使用 Cursor 在团队中\n的代码流程。'
        self.assertTrue(all(not r.accepted for r in self.rows(text, evidence_level='snippet')))
        self.assertTrue(all(not r.accepted for r in self.rows('1、Cursor 在团队中\n的代码流程。')))

    def test_original_pipeline_new_report_keeps_source_and_prior_output_bytes(self):
        text = '岗位职责\n1、推动AI编程在研发小组中\n的应用，提升交付效率。'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); db = root / 'jobs.sqlite'; record = job(text)
            with Store(db) as store:
                store.add(record)
            analyze(db, root/'first', config=CONF, as_of=NOW)
            before = {p.name:p.read_bytes() for p in (root/'first').iterdir() if p.is_file()}
            result = analyze(db, root/'second', config=CONF, as_of=NOW)
            self.assertEqual(result['rule_engine'], 'rules-0.2.1')
            rows = [json.loads(line) for line in (root/'second'/'requirements.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue(rows)
            self.assertTrue(all(r['quote'] == text[text.index('推动'):] for r in rows))
            with Store(db) as store:
                saved = store.records()[0]
            self.assertEqual(saved.to_dict(), record.to_dict())
            self.assertEqual(before, {p.name:p.read_bytes() for p in (root/'first').iterdir() if p.is_file()})
