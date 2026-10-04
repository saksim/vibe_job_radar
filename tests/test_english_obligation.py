"""Authored phrases, not market evidence: preserve language and exact offsets."""
import json
from pathlib import Path
import tempfile
import unittest

from test_core import CONF, NOW, job
from vibe_job_radar.extract import RuleExtractor, apply_reviews, spans
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.store import Store
from vibe_job_radar.synthesis import aggregate


class EnglishObligationTests(unittest.TestCase):
    def rows(self, text):
        result = RuleExtractor(CONF).extract(job(text), ['architect'])
        self.assertTrue(all(text[row.start:row.end] == row.quote for row in result))
        return result

    def test_company_and_benefits_mentions_are_not_candidate_requirements(self):
        for heading in ('About us', 'About Example Labs', 'Company overview', 'Who we are',
                        'Benefits', 'Benefits and perks', 'What we offer', 'Compensation and benefits'):
            for separator in (':\n', ': ', '\n'):
                with self.subTest(heading=heading, separator=separator):
                    self.assertEqual(self.rows(heading+separator+'We build Claude Code for software teams.'), [])
        self.assertEqual(self.rows('福利待遇\n公司提供 Cursor 会员。'), [])

    def test_qualifications_reset_excluded_section_and_preserve_original_heading(self):
        text = ('Benefits\nWe provide a Cursor subscription.\nQualifications\n'
                'Experience using Cursor to write production code is required.')
        rows = self.rows(text)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row.accepted and row.strength == 'required' for row in rows))
        self.assertTrue(all('subscription' not in row.quote for row in rows))
        self.assertEqual(list(spans(text))[-1].section, 'Qualifications')

    def test_role_section_is_not_confused_with_about_company(self):
        for heading in ('About this role', 'About the role', "What you'll do", 'Responsibilities'):
            with self.subTest(heading=heading):
                rows = self.rows(heading+':\nUse Cursor to implement changes.')
                self.assertTrue(rows)
                self.assertTrue(all(row.accepted and row.strength == 'expected' for row in rows))

    def test_adjacent_sentences_keep_positive_tool_and_negative_security_separate(self):
        text = 'You must use Cursor. You must not upload customer secrets.'
        rows = self.rows(text)
        tools = [row for row in rows if row.capability == 'tool_fluency']
        self.assertEqual(len(tools), 1); self.assertTrue(tools[0].positive and tools[0].accepted)
        self.assertEqual(tools[0].quote, 'You must use Cursor.')
        security = [row for row in rows if row.capability == 'security']
        self.assertEqual(len(security), 1); self.assertFalse(security[0].positive)
        self.assertEqual(security[0].quote, 'You must not upload customer secrets.')

    def test_opposite_tool_obligations_across_english_sentence_and_comma(self):
        for text in ('You must not use Cursor. You must use Codex.',
                     'You do not need to use Cursor, but you must use Codex.',
                     'Cursor experience is not required, but you must use Codex.'):
            with self.subTest(text=text):
                rows = self.rows(text)
                self.assertTrue(any('Cursor' in row.tools and not row.positive for row in rows))
                self.assertTrue(any('Codex' in row.tools and row.positive and row.accepted for row in rows))
                self.assertFalse(any('Cursor' in row.tools and row.positive and row.accepted for row in rows))

    def test_preferred_required_and_explicit_use_are_understood(self):
        for text, expected in (
            ('Preferred qualifications:\nExperience with Cursor.', 'preferred'),
            ('Nice to have:\nCursor experience.', 'preferred'),
            ('Requirements:\nHands-on knowledge of Cursor.', 'required'),
            ('About you:\nExperience with Cursor.', 'required'),
            ('Experience using Cursor to write tests.', 'required'),
            ('Use Cursor for AI-assisted coding.', 'expected'),
            ("You will use Claude Code to implement changes.", 'expected')):
            with self.subTest(text=text):
                rows = self.rows(text); self.assertTrue(rows)
                self.assertTrue(all(row.strength == expected and row.accepted for row in rows))

    def test_unqualified_tool_mention_requires_review_and_does_not_anchor_other_skills(self):
        for text in ('We build Claude Code for software teams.', 'Our platform integrates Cursor.',
                     'Cursor and Codex.', 'Cursor 与 Codex。'):
            with self.subTest(text=text):
                rows = self.rows(text+'\n熟悉分布式架构。')
                self.assertTrue(rows); self.assertTrue(all(not row.accepted for row in rows))
                self.assertEqual(aggregate(rows, CONF), [])
                self.assertFalse(any(row.relation != 'direct' for row in rows))
        rows = self.rows('Cursor and Codex.')
        selected = rows[0]
        apply_reviews(rows, {selected.requirement_id:{'decision':'approve','reviewer':'fixture',
                       'reason':'authored test: reviewer checked surrounding obligation'}})
        self.assertTrue(selected.accepted)

    def test_building_named_product_does_not_prove_using_the_coding_tool(self):
        for text in ('You must build Claude Code for software teams.',
                     'Responsibilities:\nYou must sell Cursor subscriptions.',
                     'Qualifications:\nExperience developing Claude Code.'):
            with self.subTest(text=text):
                rows = self.rows(text); self.assertTrue(rows)
                self.assertTrue(all(not row.accepted for row in rows))
        rows = self.rows('You must build features using Cursor.')
        self.assertTrue(any(row.accepted and row.positive for row in rows))

    def test_versions_abbreviations_urls_and_numbered_items_keep_offsets(self):
        text = ('Requirements:\n1. Use Cursor 2.1 with Python 3.12, e.g. review a fix.\n'
                '2. You must use Codex with U.S. data and https://example.org/v1.2/docs.\n'
                '3. You must not send API keys! You must use Cursor locally.')
        units = list(spans(text)); rows = self.rows(text)
        self.assertTrue(all(text[s.start:s.end] == s.text for s in units))
        self.assertTrue(any('2.1' in row.quote and '3.12' in row.quote and 'e.g.' in row.quote for row in rows))
        self.assertTrue(any('U.S. data' in row.quote and '/v1.2/docs.' in row.quote for row in rows))
        self.assertTrue(any(row.quote == 'You must use Cursor locally.' and row.positive for row in rows))
        self.assertFalse(any(row.quote.startswith(('1.', '2.', '3.')) for row in rows))

    def test_company_heading_cannot_pull_later_generic_skills_into_vibe_context(self):
        text = 'About us:\nWe build Claude Code.\nRequirements:\n熟悉分布式架构。'
        self.assertEqual(self.rows(text), [])

    def test_unspecified_chinese_stays_for_review_without_changing_explicit_requirements(self):
        self.assertTrue(all(not row.accepted for row in self.rows('Cursor 实战。')))
        rows = self.rows('任职要求\n熟练使用 Cursor。\n无需使用 Codex，但必须编写单元测试。')
        self.assertTrue(any(row.accepted and row.positive and 'Cursor' in row.tools for row in rows))
        self.assertTrue(any(not row.positive and 'Codex' in row.tools for row in rows))

    def test_original_pipeline_exports_only_real_obligation_and_preserves_prior_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); db = root/'jobs.sqlite'
            text = ('About us:\nWe build Claude Code for software teams.\nBenefits:\n'
                    'We provide a Cursor subscription.\nRequirements:\n'
                    'You must use Cursor. You must not upload customer secrets.')
            with Store(db) as store: store.add(job(text))
            first = root/'first'; result = analyze(db, first, config=CONF, as_of=NOW)
            old = {p.name:p.read_bytes() for p in first.iterdir() if p.is_file()}
            self.assertEqual(result['rule_engine'], 'rules-0.2.0')
            rows = [json.loads(line) for line in (first/'requirements.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue(rows)
            self.assertFalse(any('software teams' in row['quote'] or 'subscription' in row['quote'] for row in rows))
            self.assertTrue(any(row['quote']=='You must use Cursor.' and row['strength']=='required' for row in rows))
            self.assertIn('You must use Cursor.', (first/'requirements_zh.csv').read_text(encoding='utf-8-sig'))
            analyze(db, root/'second', config=CONF, as_of=NOW)
            self.assertEqual(old, {p.name:p.read_bytes() for p in first.iterdir() if p.is_file()})
