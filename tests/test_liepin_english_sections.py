"""Authored job text in the observed bilingual section layout; no live content."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.models import JobRecord
from vibe_job_radar.pipeline import analyze
from vibe_job_radar.store import Store
import test_liepin_recorded_layout as fixtures


BODY = ("Tasks:\n"
        "- Design a synthetic queue service with explicit recovery boundaries.\n"
        "- Maintain the authored test platform and inspect delivery failures.\n"
        "Qualifications:\n"
        "- Experience building software services and reviewing database changes.\n"
        "- Familiar with source control, automated tests and release maintenance.")
TITLE = 'Software Architect 合成软件架构师'


class EnglishSectionTests(unittest.TestCase):
    def parse(self, body=BODY, *, description=None, **layout):
        data = fixtures.posting(title=TITLE, description=body if description is None else description)
        html = fixtures.markup(data, body=body, **layout)
        return builtins().get('liepin').detail(PageSnapshot(fixtures.URL, html))

    def test_complete_sections_preserve_authored_text_and_identity(self):
        for raw in (True, False):
            for body in (BODY, BODY.replace('Tasks:', 'TASKS:').replace('Qualifications:', 'QUALIFICATIONS:')):
                with self.subTest(raw=raw, body=body[:10]):
                    parsed = self.parse(body, raw_breaks=raw)
                    self.assertEqual(parsed['text'], body)
                    self.assertEqual(parsed['title'], TITLE)
                    self.assertEqual(parsed['parser'], 'liepin:job_intro_jsonld:v1')

    def test_sections_reach_store_and_original_report_without_inventing_ai(self):
        parsed = self.parse()
        with tempfile.TemporaryDirectory() as tmp, Store(':memory:') as store:
            record = JobRecord(**parsed, url=fixtures.URL, platform='liepin',
                               source_mode='synthetic', is_synthetic=True)
            store.add(record)
            first = Path(tmp)/'first'
            result = analyze(store, first, role_filter=['architect'], demo_mode=True)
            self.assertEqual(result['stats']['full_text_job_groups'], 1)
            self.assertEqual(result['stats']['vibe_evidence_job_groups'], 0)
            saved = json.loads((first/'jobs.jsonl').read_text(encoding='utf8'))
            self.assertEqual(saved['record_id'], record.record_id)
            self.assertEqual(saved['text'], BODY)
            hashes = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in first.iterdir()}
            analyze(store, Path(tmp)/'second', role_filter=['architect'], demo_mode=True)
            self.assertEqual(hashes, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in first.iterdir()})

    def test_paired_ordered_headings_are_required(self):
        tasks, qualifications = BODY.split('Qualifications:')
        for body in (tasks, 'Qualifications:'+qualifications,
                     'Qualifications:'+qualifications+'\n'+tasks,
                     BODY.replace('Qualifications:', 'Tasks:'),
                     BODY.replace('Tasks:', 'Our tasks:'),
                     BODY.replace('Qualifications:', 'About our qualifications:')):
            with self.subTest(body=body[:30]), self.assertRaises(CrawlError):
                self.parse(body)

    def test_each_section_requires_distinct_substantive_items(self):
        lines = BODY.splitlines()
        for body in ('\n'.join(lines[:2]+lines[3:]),
                     '\n'.join(lines[:-1]),
                     BODY.replace(lines[2], lines[1]),
                     BODY.replace(lines[-1], lines[-2]),
                     BODY.replace(lines[1], '- Short item.')):
            with self.subTest(body=body[:30]), self.assertRaises(CrawlError):
                self.parse(body)

    def test_prefix_absent_or_hidden_visible_intro_does_not_gain_acceptance(self):
        for layout in ({'description':BODY[:50]}, {'anchor':False}, {'attrs':'hidden'},
                       {'attrs':'style="display:none"'}):
            with self.subTest(layout=layout), self.assertRaises(CrawlError):
                self.parse(**layout)

    def test_foreign_identity_and_conflicting_body_stay_rejected(self):
        adapter = builtins().get('liepin')
        data = fixtures.posting(title=TITLE, description=BODY,
                                url=fixtures.URL.replace('123', '456'))
        with self.assertRaises(CrawlError) as failure:
            adapter.detail(PageSnapshot(fixtures.URL, fixtures.markup(data, body=BODY)))
        self.assertEqual(failure.exception.code, 'job_identity_mismatch')
        with self.assertRaises(CrawlError):
            self.parse(description=BODY.replace('queue service', 'billing service'))

    def test_truncation_and_foreign_panels_remain_rejected(self):
        for suffix in ('\n展开全部', '\n登录后查看完整职位', '\n公司简介：其他介绍',
                       '\nSimilar jobs:\n- A different unrelated software vacancy.',
                       '\n- Please sign in to view the complete job description.'):
            with self.subTest(suffix=suffix), self.assertRaises(CrawlError):
                self.parse(BODY+suffix)

    def test_extra_unlabelled_prose_and_overlong_sections_stay_rejected(self):
        for body in ('A general corporate announcement.\n'+BODY,
                     BODY+'\nAn unlabelled company introduction.',
                     BODY+'\n'+'\n'.join('- Authored extra qualification number '+str(i) for i in range(100))):
            with self.subTest(body=body[:40]), self.assertRaises(CrawlError):
                self.parse(body)
