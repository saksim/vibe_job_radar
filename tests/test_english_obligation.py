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

    def test_contracted_prohibitions_are_not_positive_heading_defaults(self):
        for command in ("Don't use", "Don’t use", "You mustn't use", "You mustn’t use"):
            with self.subTest(command=command):
                rows = self.rows('Requirements:\n'+command+' Cursor for production code.')
                self.assertTrue(rows)
                self.assertTrue(all(r.strength == 'prohibited' and not r.positive for r in rows))
                self.assertEqual(aggregate(rows, CONF), [])
        for text in ("Requirements:\nYou don't need to use Cursor.",
                     "Requirements:\nYou don’t have to use Cursor."):
            self.assertTrue(all(r.strength == 'not_required' for r in self.rows(text)))
        for text in ("Requirements:\nYou mustn't rely on Cursor.",
                     "Requirements:\nYou mustn’t only use Cursor.",
                     "Requirements:\nDon't use Cursor and use Codex.",
                     "Requirements:\nDo not use Cursor and apply Codex.",
                     "Do not use Cursor, apply Codex, or integrate Claude Code.",
                     "Don't use Cursor, and employ Codex.",
                     "Don't use Cursor and experience with Codex is required.",
                     "Don't use Cursor because you must use Codex.",
                     "Don't use Cursor since you must use Codex.",
                     "Don't use Cursor because Codex is required.",
                     "Don't use Cursor when you must use Codex.",
                     "Don't use Cursor if you must use Codex.",
                     "Don't use Cursor provided that you must use Codex.",
                     "Don't use Cursor, yet use Codex.",
                     "Don't use Cursor, yet write code with Codex.",
                     "Don't use Cursor, still adopt Codex.",
                     "Requirements:\nDon't use Cursor but write code with Codex.",
                     "Use Codex but please don't use Cursor.",
                     "Do not use Cursor, but use of Codex is prohibited.",
                     "Requirements:\nUse of Codex is prohibited.",
                     "Preferred qualifications:\nUse of Codex is prohibited.",
                     "Requirements:\nUse of Codex will be prohibited.",
                     "Requirements:\nUse of Codex 2.1 prohibited.",
                     "Requirements:\nUse of Codex is not expected.",
                     "Requirements:\nUse of Codex is prohibited even if a license is required.",
                     "Don't use Cursor but apply to Codex."):
            self.assertTrue(all(not r.accepted for r in self.rows(text)))

        for text in ("Do not use Cursor to upload customer files.",
                     "You must not use Cursor to send customer files.",
                     "Don't use Cursor to upload customer files.",
                     "Candidates mustn’t use Cursor to send customer files.",
                     "Successful candidates must not use Cursor.",
                     "We require that you do not upload code with Cursor.",
                     "Our applicants must not use Cursor.",
                     "If on call, candidates must not use Cursor.",
                     "Don't use Cursor because it is unapproved.",
                     "Important: Do not use Cursor.",
                     "For production, do not use Cursor.",
                     "To protect customer data, don't use Cursor.",
                     "You must not ever use Cursor.",
                     "You mustn’t directly upload customer files with Cursor.",
                     "Please don't ever use Cursor.",
                     "You please don't use Cursor.",
                     "禁止使用 Cursor（do not use Cursor）。"):
            with self.subTest(text=text):
                rows = self.rows(text)
                self.assertTrue(rows)
                self.assertTrue(all(r.strength == 'prohibited' and not r.positive for r in rows))
        for text in ("Use of Cursor is optional, but Codex is required.",
                     "Use of Cursor is optional and Codex is required.",
                     "Use of Cursor is optional. Codex is required.",
                     "Use of Cursor is optional and is required.",
                     "Use of Cursor is required and is optional.",
                     "Use of Cursor is optional while Codex is required.",
                     "Use of Cursor is strongly recommended against.",
                     "Requirements:\nUse of Cursor is recommended against.",
                     "Use of Cursor is optional except in production.",
                     "Use of Cursor is optional unless production code is involved.",
                     "Use of Cursor is optional for development, but required in production.",
                     "Use of Cursor is required for development, but optional in production.",
                     "Use of Cursor is required but not required for production.",
                     "Use of Cursor is expected to be prohibited.",
                     "Use of Cursor is recommended to be avoided.",
                     "Use of Cursor is expected to eventually be prohibited.",
                     "Use of Cursor is required not to be used."):
            with self.subTest(text=text):
                rows = self.rows(text)
                self.assertTrue(rows)
                self.assertTrue(all(not r.accepted for r in rows))

        rows = self.rows("Don't use Cursor. Use of Codex is prohibited.")
        self.assertFalse(any(r.positive and r.accepted for r in rows))
        self.assertTrue(all(not r.accepted for r in rows if 'Codex' in r.tools))
        self.assertTrue(all(r.strength == 'prohibited' for r in rows if 'Cursor' in r.tools))

    def test_contracted_prohibitions_do_not_absorb_adjacent_positive_tool(self):
        for text in ("Requirements:\nUse Codex. Don't use Cursor.",
                     "Requirements:\nUse Codex. Don’t use Cursor.",
                     "Requirements:\nUse Codex. Mustn't use Cursor.",
                     "Requirements:\nUse Codex, but you mustn’t use Cursor.",
                     "Requirements:\nDon't use Cursor. Use Codex.",
                     "Requirements:\nYou mustn't use Cursor, but you must use Codex.",
                     "Don't use Cursor, but use Codex.",
                     "Don’t use Cursor, but apply Codex.",
                     "Mustn't use Cursor, but you'll adopt Codex.",
                     "Don't use Cursor, but also integrate Codex.",
                     "Don't use Cursor. Adopt Codex.",
                     "Don't use Cursor but use Codex.",
                     "Use Codex but don’t use Cursor.",
                     "Use Codex and don't use Cursor.",
                     "Don’t use Cursor however you will apply Codex.",
                     "Don't use Cursor but also adopt Codex.",
                     "Don't use Cursor and you will use Codex.",
                     "Don't use Cursor and you'll use Codex.",
                     "Don’t use Cursor and you’ll adopt Codex.",
                     "Requirements: Don't use Cursor but use Codex.",
                     "Please don't use Cursor but use Codex.",
                     "We build tools. Don\'t use Cursor but use Codex.",
                     "don't use Cursor. use Codex."):
            with self.subTest(text=text):
                rows = self.rows(text)
                cursor = [r for r in rows if 'Cursor' in r.tools]
                codex = [r for r in rows if 'Codex' in r.tools]
                self.assertTrue(cursor and codex)
                self.assertTrue(all(r.strength == 'prohibited' and not r.positive for r in cursor))
                self.assertTrue(all(r.positive and r.accepted for r in codex))

    def test_nested_must_negations_require_review_without_anchoring_other_skills(self):
        for command in ("You mustn't fail to use", "You mustn’t be afraid to use",
                        "You must not fail to use", "You must not be afraid to use"):
            with self.subTest(command=command):
                text = 'Requirements:\n'+command+' Cursor.\n熟悉分布式架构。'
                rows = self.rows(text)
                self.assertTrue(rows)
                self.assertTrue(all(r.strength == 'unspecified' and not r.accepted for r in rows))
                self.assertTrue(all(r.relation == 'direct' for r in rows))
                self.assertEqual(aggregate(rows, CONF), [])
                mixed = self.rows('Requirements:\n'+command+' Cursor. You must use Codex.')
                cursor = [r for r in mixed if 'Cursor' in r.tools]
                codex = [r for r in mixed if 'Codex' in r.tools]
                self.assertTrue(cursor and codex)
                self.assertTrue(all(not r.accepted for r in cursor))
                self.assertTrue(all(r.positive and r.accepted for r in codex))

    def test_original_report_excludes_contracted_ban_and_keeps_raw_quote(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); db = root/'jobs.sqlite'
            text = "Requirements:\nDon’t use Cursor for production code."
            with Store(db) as store: store.add(job(text))
            first = root/'first'
            analyze(db, first, config=CONF, as_of=NOW)
            old = {p.name:p.read_bytes() for p in first.iterdir() if p.is_file()}
            rows = [json.loads(line) for line in (first/'requirements.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue(rows)
            self.assertTrue(all(r['strength'] == 'prohibited' and text[r['start']:r['end']] == r['quote'] for r in rows))
            self.assertIn('Don’t use Cursor for production code.', (first/'requirements_zh.csv').read_text(encoding='utf-8-sig'))
            manifest = json.loads((first/'run_manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['rule_engine'], 'rules-0.2.2')
            self.assertTrue(all(r['extraction_method'] == 'rules-0.2.2' for r in rows))
            self.assertEqual(manifest['stats']['accepted_positive_requirement_rows'], 0)
            self.assertEqual(manifest['stats']['vibe_evidence_job_groups'], 0)
            analyze(db, root/'second', config=CONF, as_of=NOW)
            self.assertEqual(old, {p.name:p.read_bytes() for p in first.iterdir() if p.is_file()})

    def test_preferred_required_and_explicit_use_are_understood(self):
        for text, expected in (
            ('Preferred qualifications:\nExperience with Cursor.', 'preferred'),
            ('Nice to have:\nCursor experience.', 'preferred'),
            ('Requirements:\nHands-on knowledge of Cursor.', 'required'),
            ('About you:\nExperience with Cursor.', 'required'),
            ('Experience using Cursor to write tests.', 'required'),
            ('Use of Cursor is required.', 'required'),
            ('Requirements:\nUse of Cursor is not optional.', 'required'),
            ('Use of Cursor is required and is expected for every task.', 'required'),
            ('Use of Cursor is required and will be mandatory for every task.', 'required'),
            ('Use of Cursor is preferred and is required for production.', 'required'),
            ('Use of Cursor is required and is preferred for production.', 'required'),
            ('Use of Cursor is expected and will be mandatory.', 'required'),
            ('Use of Cursor is preferred and is expected.', 'expected'),
            ('Use of Cursor is a required skill.', 'required'),
            ('Use of Cursor is absolutely required.', 'required'),
            ('Use of Cursor is a plus.', 'preferred'),
            ('Preferred qualifications:\nUse of Cursor.', 'preferred'),
            ('Requirements:\nUse of Cursor.', 'required'),
            ('Use of Cursor is a critically important required skill.', 'required'),
            ('Use of Cursor is required for restricted repositories.', 'required'),
            ('Use of Cursor is required because no alternative is approved.', 'required'),
            ('Use of Cursor is required while you are working remotely.', 'required'),
            ('Use of Cursor is required while candidates are developing features.', 'required'),
            ('Use of Cursor is required for optional workflows.', 'required'),
            ('Use of Cursor is required for restricted repos and optional workflows.', 'required'),
            ('Use of Cursor is required to avoid unsafe changes.', 'required'),
            ('Use of Cursor is preferred but required in production.', 'required'),
            ('Use of Cursor is required and expected for production.', 'required'),
            ('Use of Cursor is preferred and required and is expected.', 'required'),
            ('Use of Cursor is preferred but required and expected.', 'required'),
            ('Use of Cursor is not required.', 'not_required'),
            ('Use of Cursor is not necessary.', 'not_required'),
            ('Requirements:\nUse of Cursor for restricted repositories.', 'required'),
            ('Use of Cursor is optional.', 'not_required'),
            ('Requirements:\nUse of Cursor is expected.', 'required'),
            ('Requirements:\nUse of Cursor is mandatory.', 'required'),
            ('Requirements:\nUse of Cursor will be required.', 'required'),
            ('Use of Cursor is expected.', 'expected'),
            ('Use of Cursor will be mandatory.', 'required'),
            ('Use of Cursor is essential.', 'required'),
            ('Use of Cursor is highly recommended.', 'preferred'),
            ('Use of Cursor is encouraged.', 'preferred'),
            ('Use Cursor for AI-assisted coding.', 'expected'),
            ("You will use Claude Code to implement changes.", 'expected')):
            with self.subTest(text=text):
                rows = self.rows(text); self.assertTrue(rows)
                self.assertTrue(all(row.strength == expected and row.accepted for row in rows))

    def test_unqualified_tool_mention_requires_review_and_does_not_anchor_other_skills(self):
        for text in ('We build Claude Code for software teams.', 'Our platform integrates Cursor.',
                     'Cursor and Codex.', 'Cursor 与 Codex。',
                     "Our applicants don't use Cursor today.",
                     "Candidates don't use Cursor today.",
                     "You don't use Cursor today.",
                     "You do not use Cursor today.",
                     "We use Cursor but don't use Codex internally.",
                     "We use Cursor and don't use Codex internally.",
                     "You don't use Cursor today but use Codex instead.",
                     "We don't require that you do not use Cursor.",
                     "We don't require that candidates mustn't use Cursor.",
                     "We never demand that applicants must not use Cursor.",
                     "It is not required that candidates mustn't use Cursor.",
                     "We can't require that candidates mustn't use Cursor.",
                     "We cannot require that candidates mustn't use Cursor.",
                     "Tell us whether candidates mustn't use Cursor.",
                     "If candidates mustn't use Cursor, contact support.",
                     "Do you think candidates mustn't use Cursor?",
                     "For production, we don't use Cursor.",
                     "Important: We don't use Cursor.",
                     "Example: Do not use Cursor."):
            with self.subTest(text=text):
                rows = self.rows(text+'\n熟悉分布式架构。')
                self.assertTrue(rows); self.assertTrue(all(not row.accepted for row in rows))
                self.assertEqual(aggregate(rows, CONF), [])
                self.assertFalse(any(row.relation != 'direct' for row in rows))
        for text in (
                "Our research covers workflows, use cases involving Cursor, and benchmarks.",
                "Requirements:\nUse cases involving Cursor.",
                "We don't use Cursor.",
                "Our team members don't use Cursor.",
                "Acme engineers don't use Cursor.",
                "We don't use Cursor but use Codex internally.",
                "We don't use Cursor, but use Codex internally.",
                "Our team doesn't use Cursor but use Codex internally.",
                "Acme does not use Cursor but use Codex internally."):
            with self.subTest(text=text):
                rows = self.rows(text)
                self.assertTrue(rows)
                self.assertFalse(any(row.accepted and row.positive for row in rows))
                self.assertEqual(aggregate(rows, CONF), [])
        self.assertTrue(all(not row.accepted for row in self.rows('Use of Cursor.')))
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
            self.assertEqual(result['rule_engine'], RuleExtractor.version)
            rows = [json.loads(line) for line in (first/'requirements.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertTrue(rows)
            self.assertFalse(any('software teams' in row['quote'] or 'subscription' in row['quote'] for row in rows))
            self.assertTrue(any(row['quote']=='You must use Cursor.' and row['strength']=='required' for row in rows))
            self.assertIn('You must use Cursor.', (first/'requirements_zh.csv').read_text(encoding='utf-8-sig'))
            analyze(db, root/'second', config=CONF, as_of=NOW)
            self.assertEqual(old, {p.name:p.read_bytes() for p in first.iterdir() if p.is_file()})
