"""Artificial DOM observations; no BOSS/51job live-access certification."""
import unittest

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot

TEXT = '岗位职责：负责数据平台建设和系统开发。任职要求：熟练掌握Python及数据库设计，具备相关项目经验。'
SITES = [('boss', 'https://www.zhipin.com/job_detail/fixture.html', 'job-sec-text'),
         ('51job', 'https://we.51job.com/pc/jobdetail?jobId=123', 'job_msg')]


def page(url, container, extra='', *, tail=''):
    return PageSnapshot(url, f'<h1>数据工程师</h1><div class="{container}">{TEXT}{extra}</div>{tail}')


class DOMDetailSafeguards(unittest.TestCase):
    def test_explicit_expand_control_is_not_full_job_text(self):
        for site, url, container in SITES:
            with self.subTest(site=site):
                with self.assertRaisesRegex(CrawlError, 'jd_incomplete'):
                    builtins().get(site).detail(page(url, container, '<button>展开全部</button>'))

    def test_changed_job_after_navigation_is_rejected(self):
        for site, url, container in SITES:
            changed = url.replace('fixture.html', 'different.html').replace('jobId=123', 'jobId=456')
            adapter = builtins().get(site)
            validator = getattr(adapter, 'validate_detail_identity', lambda *_: None)
            with self.subTest(site=site):
                with self.assertRaisesRegex(CrawlError, 'job_identity_mismatch'):
                    validator(url, page(changed, container))

    def test_conflicting_canonical_never_certifies_current_job(self):
        for site, url, container in SITES:
            with self.subTest(site=site):
                with self.assertRaisesRegex(CrawlError, 'job_identity_mismatch'):
                    builtins().get(site).detail(page(url, container, tail='<link rel="canonical" href="https://unrelated.test/job/123">'))

    def test_complete_original_body_and_unrelated_controls_remain_usable(self):
        for site, url, container in SITES:
            with self.subTest(site=site):
                result = builtins().get(site).detail(page(url, container,
                    '<p>负责开发展开全部按钮的交互。</p>',
                    tail='<aside><button>展开全部</button></aside>'))
                self.assertIn(TEXT, result['text'])
                self.assertIn('负责开发展开全部按钮', result['text'])
                self.assertEqual(result['title'], '数据工程师')

    def test_tracking_only_canonical_is_compatible(self):
        for site, url, container in SITES:
            tracked = url + ('&' if '?' in url else '?') + 'utm_source=fixture'
            adapter = builtins().get(site)
            with self.subTest(site=site):
                adapter.validate_detail_identity(tracked, page(url, container,
                    tail=f'<link rel="canonical" href="{url}">'))

    def test_hidden_expansion_template_does_not_reject_complete_body(self):
        for site, url, container in SITES:
            with self.subTest(site=site):
                for hidden in ('hidden', 'style="display:none"'):
                    parsed = builtins().get(site).detail(page(url, container,
                        f'<div {hidden}><button>展开全部</button></div>'))
                    self.assertIn(TEXT, parsed['text'])

    def test_full_description_suffixes_are_explicit_expansion_controls(self):
        for site, url, container in SITES:
            for label in ('展开全部职位描述', '展开更多职位描述', '点击展开全部职位描述',
                          '查看完整职位描述'):
                with self.subTest(site=site, label=label):
                    with self.assertRaisesRegex(CrawlError, 'jd_incomplete'):
                        builtins().get(site).detail(page(url, container, f'<button>{label}</button>'))

    def test_rendered_observation_excludes_class_hidden_expand_templates(self):
        for site, url, container in SITES:
            markup = page(url, container,
                '<button class="collapsed-template">展开全部</button>',
                tail='<style>.collapsed-template { display:none }</style>').html
            with self.subTest(site=site):
                parsed = builtins().get(site).detail(PageSnapshot(url, markup, visible_text=TEXT))
                self.assertIn(TEXT, parsed['text'])
                for rendered in (TEXT+'\n展开全部', None, ''):
                    with self.subTest(rendered=rendered):
                        with self.assertRaisesRegex(CrawlError, 'jd_incomplete'):
                            builtins().get(site).detail(PageSnapshot(url, markup, visible_text=rendered))
