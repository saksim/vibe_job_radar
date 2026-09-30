"""Offline readiness checks retain timeout, DOM verification and first failure."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from vibe_job_radar.guided.browser_health import probe_browser


class BlankPage:
    def __init__(self, *, content_error=None, title_error=None, title='Vibe Radar browser check'):
        self.content_error, self.title_error, self.value = content_error, title_error, title
        self.calls = []
        self.dom_written = False
    def set_content(self, html, **options):
        self.calls.append(('content', options))
        self.dom_written = True
        if self.content_error: raise self.content_error
        # A document can be parsed while full-load completion is still pending.
        if options.get('wait_until', 'load') == 'load': raise TimeoutError('load remains pending')
    def title(self):
        self.calls.append(('title', {}))
        if self.title_error: raise self.title_error
        return self.value


class BlankReadinessTests(unittest.TestCase):
    def check(self, page):
        closed = []
        backend = SimpleNamespace(page=page, close=lambda: closed.append(True), startup_report={
            'stage':'ready','ready':True,'launch_tested':True,'executable_exists':True,
            'mode':'headed','playwright_version':'1.63.0','browser_channel':'bundled'})
        with patch('vibe_job_radar.guided.browser.PlaywrightBackend', return_value=backend) as factory:
            report = probe_browser()
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(closed, [True])
        return report

    def test_parsed_fixed_document_does_not_wait_for_full_load_and_still_checks_title(self):
        page = BlankPage(); report = self.check(page)
        self.assertTrue(report['ready'])
        self.assertEqual(report['code'], 'browser_ready')
        self.assertEqual(page.calls, [('content', {'wait_until':'domcontentloaded','timeout':6000}), ('title', {})])
        self.assertTrue(page.dom_written)

    def test_dom_timeout_fails_without_retry_or_title_read_and_keeps_launch_facts(self):
        page = BlankPage(content_error=TimeoutError('fixed DOM deadline')); report = self.check(page)
        self.assertFalse(report['ready'])
        self.assertEqual(report['code'], 'browser_check_failed')
        self.assertEqual(report['stage'], 'blank_page_content')
        self.assertTrue(report['launch_tested']); self.assertTrue(report['executable_exists'])
        self.assertEqual(report['error_type'], 'TimeoutError')
        self.assertEqual(len(page.calls), 1)

    def test_title_read_failure_is_distinct_and_never_turns_into_readiness(self):
        page = BlankPage(title_error=RuntimeError('title read failed')); report = self.check(page)
        self.assertFalse(report['ready'])
        self.assertEqual(report['stage'], 'blank_page_verify')
        self.assertEqual(report['error_type'], 'RuntimeError')
        self.assertEqual([name for name, _ in page.calls], ['content', 'title'])

    def test_wrong_title_is_rejected_after_dom_readiness(self):
        page = BlankPage(title='unexpected'); report = self.check(page)
        self.assertFalse(report['ready'])
        self.assertEqual(report['stage'], 'blank_page_verify')
        self.assertIn('blank page verification failed', report['error_summary'])
        self.assertEqual([name for name, _ in page.calls], ['content', 'title'])


if __name__ == '__main__': unittest.main()
