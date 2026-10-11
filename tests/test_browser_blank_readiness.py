"""Offline readiness checks retain timeout, DOM verification and first failure."""
import unittest
import json
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit
from html.parser import HTMLParser
from unittest.mock import patch

from vibe_job_radar.guided.browser_health import probe_browser


class BlankPage:
    def __init__(self, *, content_error=None, title_error=None, title='Vibe Radar browser check'):
        self.content_error, self.title_error, self.value = content_error, title_error, title
        self.calls = []
        self.dom_written = False
        self.inline_url = None
    def on(self, event, callback):
        pass
    def is_closed(self):
        return False
    def set_content(self, html, **options):
        # Controlled old SDK completion failure after the DOM was written.
        self.dom_written = True
        raise TimeoutError('authored console completion barrier absent')
    def goto(self, url, **options):
        self.inline_url = url
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
        browser = SimpleNamespace(on=lambda event, callback: None, is_connected=lambda: True)
        backend = SimpleNamespace(page=page, browser=browser, close=lambda: closed.append(True), startup_report={
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


    def test_inline_document_does_not_depend_on_legacy_console_completion(self):
        page = BlankPage(); report = self.check(page)
        self.assertTrue(report['ready'])
        self.assertEqual(report['blank_page_check']['step'], 'verified')
        self.assertEqual(set(report['blank_page_check']['elapsed_ms']), {'load_inline_page', 'read_title'})
        self.assertTrue(page.dom_written)
        self.assertEqual(len(page.calls), 2)

    def test_only_fixed_script_free_inline_document_can_be_loaded(self):
        page = BlankPage(); report = self.check(page)
        self.assertTrue(report['ready'])
        self.assertEqual(urlsplit(page.inline_url).scheme, 'data')
        self.assertTrue(page.inline_url.startswith('data:text/html;charset=utf-8,'))
        tags = []
        class Tags(HTMLParser):
            def handle_starttag(self, tag, attrs): tags.append((tag, dict(attrs)))
        parser = Tags(); parser.feed(unquote(page.inline_url.split(',', 1)[1]))
        self.assertEqual([tag for tag, _ in tags], ['meta', 'meta', 'title', 'p'])
        self.assertEqual(tags[1][1], {'http-equiv':'Content-Security-Policy',
            'content':"default-src 'none'; base-uri 'none'; form-action 'none'"})
        self.assertTrue(all(not set(attrs).intersection({'src','href','action'}) for _, attrs in tags))
        self.assertEqual(page.calls[0][1], {'wait_until':'domcontentloaded','timeout':6000})



class NavigationPage(BlankPage):
    def __init__(self, mode):
        super().__init__()
        self.mode, self.events = mode, {}
        self._main_frame = object()
        self.location = 'about:blank'
    def on(self, name, callback):
        self.events[name] = callback
    @property
    def main_frame(self):
        if self.mode == 'unavailable':
            raise RuntimeError('PRIVATE_FRAME_ERROR')
        return self._main_frame
    @property
    def url(self):
        if self.mode == 'unavailable':
            raise RuntimeError('PRIVATE_URL_ERROR')
        return self.location
    def emit_navigation(self, frame):
        if 'framenavigated' in self.events:
            self.events['framenavigated'](frame)
    def goto(self, url, **options):
        self.calls.append(('content', options))
        if self.mode in ('committed', 'success', 'burst'):
            self.location = url
            for _ in range(1005 if self.mode == 'burst' else 1):
                self.emit_navigation(self._main_frame)
        elif self.mode == 'other':
            self.location = 'https://PRIVATE.invalid/path?token=PRIVATE'
            self.emit_navigation(object())
        elif self.mode == 'unavailable':
            self.emit_navigation(self._main_frame)
        if self.mode != 'success':
            raise TimeoutError('authored inline navigation timeout')
        self.events['domcontentloaded']()
        self.events['load']()


class BlankNavigationObservationTests(unittest.TestCase):
    def run_probe(self, mode, *, mutate_cleanup=False):
        page = NavigationPage(mode)
        closed = []
        browser = SimpleNamespace(on=lambda *args: None, is_connected=lambda: True)
        def close():
            closed.append(True)
            if mutate_cleanup:
                page.location = 'https://PRIVATE.invalid/cleanup'
                page.emit_navigation(page._main_frame)
        backend = SimpleNamespace(page=page, browser=browser, close=close,
            startup_report={'ready':True,'launch_tested':True})
        with patch('vibe_job_radar.guided.browser.PlaywrightBackend', return_value=backend):
            report = probe_browser()
        self.assertEqual(closed, [True])
        self.assertEqual(page.calls[0],
            ('content', {'wait_until':'domcontentloaded','timeout':6000}))
        self.assertEqual(len(page.calls), 2 if mode == 'success' else 1)
        self.assertNotIn('PRIVATE', json.dumps(report))
        return report

    def observation(self, report):
        self.assertIn('navigation_before_cleanup', report['blank_page_check'])
        return report['blank_page_check']['navigation_before_cleanup']

    def test_timeout_distinguishes_no_commit_from_committed_inline_document(self):
        for mode, commits, location in [('not_committed',0,'about_blank'),
                                       ('committed',1,'fixed_inline')]:
            with self.subTest(mode=mode):
                report = self.run_probe(mode)
                self.assertFalse(report['ready'])
                self.assertEqual(report['error_type'], 'TimeoutError')
                facts = self.observation(report)
                self.assertEqual(facts, {'main_frame_commits':commits,
                    'other_frame_commits':0,'frame_observation_failed':False,
                    'location':location,'observed_page_is_current':True})

    def test_other_frame_and_private_location_cannot_claim_fixed_document_commit(self):
        report = self.run_probe('other')
        facts = self.observation(report)
        self.assertFalse(report['ready'])
        self.assertEqual(facts['main_frame_commits'], 0)
        self.assertEqual(facts['other_frame_commits'], 1)
        self.assertEqual(facts['location'], 'other')

    def test_observation_errors_preserve_original_failure_and_remain_unknown(self):
        report = self.run_probe('unavailable')
        self.assertFalse(report['ready'])
        self.assertEqual(report['error_type'], 'TimeoutError')
        facts = self.observation(report)
        self.assertTrue(facts['frame_observation_failed'])
        self.assertEqual(facts['location'], 'unavailable')

    def test_cleanup_cannot_change_frozen_commit_and_location_observations(self):
        report = self.run_probe('success', mutate_cleanup=True)
        self.assertTrue(report['ready'])
        facts = self.observation(report)
        self.assertEqual(facts['main_frame_commits'], 1)
        self.assertEqual(facts['location'], 'fixed_inline')

    def test_navigation_event_counts_are_bounded_without_retrying_the_probe(self):
        report = self.run_probe('burst')
        self.assertFalse(report['ready'])
        self.assertEqual(self.observation(report)['main_frame_commits'], 999)


if __name__ == '__main__': unittest.main()
