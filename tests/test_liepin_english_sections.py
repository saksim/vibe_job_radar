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
            second = Path(tmp)/'second'
            analyze(store, second, role_filter=['architect'], demo_mode=True)
            # A later report must not rewrite the earlier one. Run timestamps
            # and manifests need not be identical between separate reports.
            self.assertEqual(hashes, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in first.iterdir()})
            self.assertEqual(set(hashes), {p.name for p in second.iterdir()})
            replay = json.loads((second/'jobs.jsonl').read_text(encoding='utf8'))
            self.assertEqual(replay['record_id'], record.record_id)
            self.assertEqual(replay['text'], BODY)
            manifest = json.loads((second/'run_manifest.json').read_text(encoding='utf8'))
            self.assertEqual(manifest['stats']['full_text_job_groups'], 1)
            for name, expected in manifest['output_files_sha256'].items():
                self.assertEqual(hashlib.sha256((second/name).read_bytes()).hexdigest(), expected)

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
                     BODY.replace(lines[1], '- Short item.'),
                     '\n'.join(lines[:4]+lines[1:3])):
            with self.subTest(body=body[:30]), self.assertRaises(CrawlError):
                self.parse(body)
        # Repeating a duty does not replace the two genuine qualifications.
        body = BODY+'\n'+lines[1]
        self.assertEqual(self.parse(body)['text'], body)

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

    def test_english_login_and_expansion_controls_are_incomplete(self):
        for prompt in (
                "Please sign in to see the complete job description.",
                "Please log in to see the complete job description.",
                "Please login to see the qualifications.",
                "Please see the full job description.",
                "Please click here to read more.",
                "Click here to show more.",
                "Please sign in to access the complete job description.",
                "Log in to unlock the complete job description.",
                "Please sign in to continue reading this posting.",
                "Please click here to see the full description.",
                "Continue reading the full job description.",
                "Please click here to continue reading.",
                "Please continue to read the complete description.",
                "Register or sign in to view the complete job description.",
                "Already registered? Sign in to view the complete job description."):
            with self.subTest(prompt=prompt), self.assertRaises(CrawlError):
                self.parse(BODY+"\n- "+prompt)

    def test_substantive_read_more_and_show_more_phrases_are_preserved(self):
        for qualification in (
                "Ability to read more complex technical diagrams and specifications.",
                "Develop dashboards that show more detailed software diagnostics.",
                "Ability to see the full job lifecycle from architecture to production.",
                "Read the complete job specification and turn it into a technical design.",
                "Design sign-in workflows to access internal monitoring dashboards.",
                "Sign in to staging and validate each release before production rollout.",
                "Design sign-in forms that let applicants view the full job description.",
                "Sign in to staging and validate employee qualifications."):
            body = BODY+"\n- "+qualification
            with self.subTest(qualification=qualification):
                self.assertEqual(self.parse(body)['text'], body)

    def test_recognized_english_prompts_report_incomplete_across_layouts(self):
        for prompt in ("Please sign in to see the complete job description.",
                       "Continue reading the full job description."):
            for body, layout in (
                    (BODY, {}),
                    (BODY.replace("Qualifications:", "任职资格"), {}),
                    (BODY, {'anchor':False, 'raw_breaks':False})):
                with self.subTest(prompt=prompt, layout=layout, body=body[:6]):
                    with self.assertRaises(CrawlError) as failure:
                        self.parse(body+"\n- "+prompt, **layout)
                    self.assertEqual(failure.exception.code, 'jd_incomplete')

    def test_control_text_cannot_supply_a_second_qualification(self):
        lines = BODY.splitlines()
        for control in ("The remaining material is available only to account holders.",
                        "Use your account to uncover the rest of this vacancy.",
                        "Members may inspect additional details after authentication.",
                        "The remaining skills are visible only to account holders."):
            body = "\n".join(lines[:-1]+["- "+control])
            with self.subTest(control=control), self.assertRaises(CrawlError):
                self.parse(body)

    def test_access_gates_preserve_clause_order_and_account_qualifiers(self):
        prompts = (
            "To view the complete job description, please sign in.",
            "Please sign in to your account to view the complete job description.",
            "To read the full description, sign in with your account.",
            "To continue reading, please log in to your account.",
            "For the full job description, please log in.",
            "You must sign in to see the full qualifications.",
            "Please sign in with your account to access the full job details.",
        )
        for prompt in prompts:
            for body, layout in (
                    (BODY, {}),
                    (BODY.replace("Qualifications:", "任职资格"), {}),
                    (BODY, {'anchor':False, 'raw_breaks':False})):
                with self.subTest(prompt=prompt, layout=layout, body=body[:6]):
                    with self.assertRaises(CrawlError) as failure:
                        self.parse(body+"\n- "+prompt, **layout)
                    self.assertEqual(failure.exception.code, 'jd_incomplete')
        for duty in (
                "To view production metrics, sign in to the monitoring account.",
                "Sign in to your account to validate the employee qualifications.",
                "To view the complete job lifecycle, maintain production dashboards.",
                "Explain how to sign in to view the full job description to applicants.",
                "Implement a form where applicants sign in to view the full job description.",
        ):
            body = BODY+"\n- "+duty
            with self.subTest(duty=duty):
                self.assertEqual(self.parse(body)['text'], body)
