"""Developer-only workbench readiness checks; no site or login decision changes."""
import importlib.util
from pathlib import Path
from unittest.mock import Mock
import unittest

class WorkbenchReadinessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1]/'scripts/run_browser_choice_acceptance.py'
        spec = importlib.util.spec_from_file_location('edge_readiness_fixture', path)
        cls.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.module)

    def setup_page(self):
        server = Mock(origin='http://127.0.0.1:12345', entry_url='http://127.0.0.1:12345/#token=SYNTHETIC')
        page = Mock(url=server.origin + '/')
        page.goto.return_value.status = 200
        return page, server

    def test_waits_for_authenticated_ui_without_retry_or_load(self):
        page, server = self.setup_page()
        expect = Mock()
        self.module.open_workbench(page, server, expect)
        page.goto.assert_called_once_with(server.entry_url, wait_until='domcontentloaded', timeout=30000)
        expect.return_value.to_contain_text.assert_called_once_with('真实记录 0', timeout=30000)
        expect.return_value.to_be_visible.assert_called_once()
        page.reload.assert_not_called()

    def test_navigation_error_not_swallowed_or_retried(self):
        page, server = self.setup_page()
        page.goto.side_effect = TimeoutError('synthetic failure')
        with self.assertRaises(TimeoutError):
            self.module.open_workbench(page, server, Mock())
        self.assertEqual(page.goto.call_count, 1)

    def test_200_alone_cannot_pass_without_ui_initialization(self):
        page, server = self.setup_page()
        expect = Mock()
        expect.return_value.to_contain_text.side_effect = AssertionError('no status')
        with self.assertRaises(AssertionError):
            self.module.open_workbench(page, server, expect)

    def test_http_error_or_unconsumed_fragment_cannot_pass(self):
        page, server = self.setup_page()
        page.goto.return_value.status = 403
        with self.assertRaises(AssertionError):
            self.module.open_workbench(page, server, Mock())
        page.goto.return_value.status = 200
        page.url = server.entry_url
        with self.assertRaises(AssertionError):
            self.module.open_workbench(page, server, Mock())
