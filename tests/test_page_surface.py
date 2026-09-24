"""A dormant login template cannot block an otherwise readable job page."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError, PageSnapshot
from vibe_job_radar.guided.page_surface import surface_text

SEARCH = 'https://www.liepin.com/zhaopin/?key=test'
DETAIL = 'https://www.liepin.com/job/123456.shtml'
BODY = '岗位职责：负责时间序列算法研发、数据质量分析、模型训练和线上效果验证。任职要求：熟悉 Python 和机器学习，能够独立完成测试与技术文档。'
HTML = '<h1>时间序列算法工程师</h1><dl><dt>职位介绍</dt><dd>' + BODY + '</dd></dl>'


class PageSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.adapter = builtins().get('liepin')

    def test_hidden_template_does_not_block_list_or_complete_detail(self):
        for hidden in ('<template>登录后查看</template>', '<div hidden>安全验证</div>',
                       '<div style="display: none !important">登录后查看</div>',
                       '<section style="visibility:hidden"><div>安全验证</div></section>'):
            with self.subTest(hidden=hidden):
                page = PageSnapshot(SEARCH, '<a href="' + DETAIL + '">时间序列算法工程师</a>' + hidden)
                self.assertEqual(len(self.adapter.cards(page)), 1)
                self.assertEqual(self.adapter.detail(PageSnapshot(DETAIL, HTML + hidden))['text'], BODY)

    def test_browser_rendering_handles_stylesheet_hidden_template(self):
        hidden = '<style>.login{display:none}</style><div class="login">登录后查看</div>'
        page = PageSnapshot(DETAIL, HTML + hidden, visible_text='时间序列算法工程师\n职位介绍\n' + BODY)
        self.assertEqual(self.adapter.detail(page)['text'], BODY)

    def test_closed_job_is_not_a_parser_failure_or_recommended_job(self):
        page = PageSnapshot(DETAIL, '<h1>该职位已暂停招聘</h1>' + HTML)
        with self.assertRaises(CrawlError) as caught:
            self.adapter.detail(page)
        self.assertEqual(caught.exception.code, 'job_unavailable')
        self.assertEqual(self.adapter.detail(PageSnapshot(DETAIL,
            '<div hidden>该职位已暂停招聘</div>' + HTML))['text'], BODY)

    def test_visible_login_and_captcha_still_block(self):
        for message in ('登录后查看', '安全验证', '访问过于频繁', 'verify you are human'):
            with self.subTest(message=message):
                page = PageSnapshot(DETAIL, HTML, visible_text=message)
                with self.assertRaises(CrawlError) as caught:
                    self.adapter.detail(page)
                self.assertEqual(caught.exception.code, 'manual_required')

    def test_offline_visible_gate_is_not_filtered(self):
        for gate in ('<div>登录后查看</div>', '<div aria-hidden="true">安全验证</div>'):
            with self.subTest(gate=gate), self.assertRaises(CrawlError) as caught:
                self.adapter.detail(PageSnapshot(DETAIL, HTML + gate))
            self.assertEqual(caught.exception.code, 'manual_required')

    def test_url_challenge_is_not_hidden_by_rendered_text(self):
        with self.assertRaises(CrawlError) as caught:
            self.adapter.cards(PageSnapshot('https://www.liepin.com/challenge/', '', visible_text=''))
        self.assertEqual(caught.exception.code, 'manual_required')

    def test_complete_text_is_still_required(self):
        incomplete = HTML.replace(BODY, '岗位职责：登录后查看完整职位描述')
        with self.assertRaises(CrawlError) as caught:
            self.adapter.detail(PageSnapshot(DETAIL, incomplete, visible_text='职位介绍'))
        self.assertEqual(caught.exception.code, 'jd_incomplete')

    def test_rendered_observation_is_private_and_validated(self):
        self.assertNotIn('private text', repr(PageSnapshot(DETAIL, HTML, visible_text='private text')))
        for value in (True, [], 'x' * 5_000_001):
            with self.subTest(kind=type(value).__name__), self.assertRaises(CrawlError):
                surface_text(PageSnapshot(DETAIL, HTML, visible_text=value))

    def test_backend_passes_the_same_rendered_text_to_adapter(self):
        page = Mock()
        page.url = DETAIL
        page.is_closed.return_value = False
        page.locator.return_value.inner_text.return_value = BODY
        page.content.return_value = HTML + '<div hidden>安全验证</div>'
        backend = SimpleNamespace(error=None, page=page, adapter=self.adapter)
        snapshot = PlaywrightBackend.snapshot(backend)
        self.assertEqual(snapshot.visible_text, BODY)
        self.assertEqual(self.adapter.detail(snapshot)['text'], BODY)


if __name__ == '__main__':
    unittest.main()
