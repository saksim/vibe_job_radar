"""An anonymous list behind a verification form must never resume collection."""
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.contracts import CrawlError
import test_login_return as return_tests


class LoginSurfaceTests(unittest.TestCase):
    def backend(self, marker=None, url=None):
        backend = PlaywrightBackend.__new__(PlaywrightBackend)
        backend.adapter = builtins().get('liepin')
        backend.error, backend.auth_mode = None, True
        backend.page = Mock()
        backend.page.url = url or backend.adapter.search_url('时间序列算法工程师')
        backend.page.is_closed.return_value = False
        def locator(selector):
            value = Mock()
            value.count.return_value = int(selector == marker)
            value.inner_text.return_value = '时间序列算法工程师'
            return value
        backend.page.locator.side_effect = locator
        backend.page.content.return_value = '<a href="/job/123.shtml">时间序列算法工程师</a>'
        return backend

    def test_visible_sms_scan_and_captcha_do_not_expose_background_list(self):
        for marker, code in (
            ('input[data-nick="login-smscode"]:visible', 'login_sms_required'),
            ('.ScanQrCode:visible', 'login_scan_required'),
            ('.lp-mini-scan-qrcode:visible', 'login_scan_required'),
            ('iframe[src^="https://turing.captcha.qcloud.com/template/"]:visible', 'login_captcha_required'),
            ('#header-quick-menu-login:visible', 'manual_required'),
        ):
            with self.subTest(marker=marker):
                backend = self.backend(marker)
                with patch('vibe_job_radar.guided.rendered_dom.capture',
                           return_value=('<p>职位</p>', '职位', '<p>职位</p>')):
                    with self.assertRaises(CrawlError) as error:
                        backend.snapshot()
                self.assertEqual(error.exception.code, code)
                backend.page.content.assert_not_called()

    def test_two_factor_ticket_is_not_read_into_a_page_snapshot(self):
        backend = self.backend(url='https://c.liepin.com/login/two-factor?ticket=synthetic-private')
        with self.assertRaises(CrawlError) as error:
            backend.snapshot()
        self.assertEqual(error.exception.code, 'login_sms_required')
        self.assertNotIn('synthetic-private', str(error.exception))
        backend.page.content.assert_not_called()

    def test_no_visible_login_surface_keeps_the_original_snapshot(self):
        backend = self.backend()
        with patch('vibe_job_radar.guided.rendered_dom.capture',
                   return_value=('<p>职位</p>', '职位', '<p>职位</p>')):
            self.assertEqual(backend.snapshot().url, backend.page.url)


class ManualStepContinuationTests(unittest.TestCase):
    def test_cleared_page_ends_watcher_and_keeps_original_task_for_attention(self):
        fixture = return_tests.ReturnWatcherTests(); fixture.setUp()
        fixture.backend.snapshot.side_effect = CrawlError('native_page_cleared')
        fixture.tick()
        self.assertNotIn('task', fixture.manager._watches)
        self.assertEqual(fixture.state['code'], 'native_page_cleared')
        self.assertEqual(fixture.state['login_continuation'], 'needs_attention')
        fixture.service._submit.assert_not_called()
        fixture.backend.open.assert_not_called()

    def test_each_manual_step_keeps_original_deadline_and_requires_fresh_return(self):
        for code in ('login_sms_required', 'login_scan_required', 'login_captcha_required'):
            with self.subTest(code=code):
                fixture = return_tests.ReturnWatcherTests()
                fixture.setUp()
                fixture.tick()
                deadline = fixture.manager._watches['task'].expires
                fixture.backend.snapshot.side_effect = CrawlError(code)
                fixture.tick()
                self.assertIn('task', fixture.manager._watches)
                self.assertEqual(fixture.manager._watches['task'].expires, deadline)
                self.assertEqual(fixture.state['code'], code)
                fixture.backend.snapshot.side_effect = None
                fixture.tick()
                fixture.service._submit.assert_not_called()
                fixture.tick()
                fixture.service._submit.assert_called_once_with('capture', 'task')
                fixture.backend.open.assert_not_called()
