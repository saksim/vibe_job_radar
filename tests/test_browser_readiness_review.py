"""PR18 review regressions. No supplier requests, package downloads or real accounts."""
from __future__ import annotations

import builtins as python_builtins
import json
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.browser_health import (
    BrowserStartupError, environment_report, probe_browser, supported_version,
)
from vibe_job_radar.guided.browser_install import install_commands


@contextmanager
def fake_runtime(version='1.57.0'):
    with tempfile.TemporaryDirectory() as tmp:
        exe = Path(tmp) / 'chrome.exe'
        exe.touch()
        runtime = MagicMock()
        runtime.chromium.executable_path = str(exe)
        sync = MagicMock()
        sync.return_value.start.return_value = runtime
        module = types.ModuleType('playwright.sync_api')
        module.sync_playwright = sync
        facts = {**environment_report(), 'playwright_version': version}
        with patch.dict(sys.modules, {'playwright': types.ModuleType('playwright'), 'playwright.sync_api': module}), \
             patch('vibe_job_radar.guided.browser.environment_report', return_value=facts):
            yield exe, runtime, sync, facts


def backend():
    return PlaywrightBackend(builtins().get('boss'), None, Event(),
                             transport_factory=lambda *a: MagicMock())


class VersionReviewTests(unittest.TestCase):
    def test_post_and_local_builds_match_declared_requirement(self):
        for value in ('1.57.0.post1', '1.57.0+conda', '1.57.0.post1+conda.2',
                      'v1.57.0', '0!1.57.0', '1.48.0.0', '1.57-1'):
            with self.subTest(value=value):
                self.assertTrue(supported_version(value))

    def test_prerelease_dev_epoch_and_upper_bound_follow_pep440(self):
        for value in ('1.57.0rc1', '1.57.0.dev1', '1!1.57.0', '2.0.0+conda',
                      '2.0.dev1', '1.47.9.post1', 'not-a-version'):
            with self.subTest(value=value):
                self.assertFalse(supported_version(value))

    def test_incompatible_version_is_never_imported_and_repair_can_recheck(self):
        real_import = python_builtins.__import__
        imported = []
        def observed_import(name, *args, **kwargs):
            if name == 'playwright.sync_api':
                imported.append(name)
            return real_import(name, *args, **kwargs)
        with fake_runtime('1.47.0') as (_, runtime, sync, facts), \
             patch('builtins.__import__', side_effect=observed_import):
            with self.assertRaises(BrowserStartupError) as error:
                backend()
            self.assertEqual(error.exception.code, 'playwright_incompatible')
            self.assertEqual(imported, [])
            sync.assert_not_called()
            facts['playwright_version'] = '1.57.0'
            repaired = backend()
            self.assertTrue(repaired.startup_report['ready'])
            self.assertEqual(imported, ['playwright.sync_api'])
            repaired.close()

    def test_missing_distribution_does_not_import_old_client(self):
        with fake_runtime(None) as (_, runtime, sync, _):
            with self.assertRaises(BrowserStartupError) as error:
                backend()
            self.assertEqual(error.exception.code, 'playwright_missing')
            sync.assert_not_called()

    def test_missing_optional_validator_has_actionable_error(self):
        with fake_runtime() as (_, _, sync, _), \
             patch('vibe_job_radar.guided.browser.supported_version',
                   side_effect=ModuleNotFoundError('missing packaging', name='packaging')):
            with self.assertRaises(BrowserStartupError) as error:
                backend()
            self.assertEqual(error.exception.code, 'version_validator_missing')
            self.assertIn('packaging', error.exception.report['message'])
            sync.assert_not_called()

    def test_repair_installs_declared_validator_with_playwright(self):
        self.assertIn('packaging>=24.2', install_commands()[0][1])
        metadata = (Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text(encoding='utf-8')
        self.assertIn('browser = ["playwright>=1.48,<2", "packaging>=24.2"]', metadata)
        self.assertIn('dependencies = []', metadata)

    def test_offline_core_remains_importable_without_site_packages(self):
        root = Path(__file__).resolve().parents[1]
        command = [sys.executable, '-S', '-c',
                   "import sys;sys.path.insert(0,sys.argv[1]);"
                   "from vibe_job_radar.guided.browser_health import environment_report;"
                   "from vibe_job_radar.workspace import Workspace;"
                   "import tempfile;"
                   "print(environment_report()['ready']);"
                   "t=tempfile.TemporaryDirectory();w=Workspace(t.name);"
                   "print(w.doctor()['workspace_writable']);t.cleanup()", str(root/'src')]
        result = subprocess.run(command, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ['False', 'True'])


class StartupFactsReviewTests(unittest.TestCase):
    def test_stat_permission_error_is_not_rewritten_to_absence(self):
        with fake_runtime() as (exe, runtime, _, _), \
             patch('vibe_job_radar.guided.browser.Path.stat', side_effect=PermissionError('access denied')):
            with self.assertRaises(BrowserStartupError) as error:
                backend()
            report = error.exception.report
            self.assertEqual(report['code'], 'browser_permission_denied')
            self.assertEqual(report['stage'], 'executable')
            self.assertEqual(report['executable_path'], str(exe))
            self.assertIsNone(report['executable_exists'])
            self.assertFalse(report['launch_tested'])
            runtime.chromium.launch.assert_not_called()
            runtime.stop.assert_called_once()

    def test_missing_binary_is_still_distinguished_from_permission(self):
        with fake_runtime() as (exe, runtime, _, _):
            exe.unlink()
            with self.assertRaises(BrowserStartupError) as error:
                backend()
            self.assertEqual(error.exception.code, 'browser_executable_missing')
            self.assertFalse(error.exception.report['executable_exists'])
            runtime.chromium.launch.assert_not_called()

    def test_blank_content_failure_preserves_successful_launch_evidence(self):
        mock = MagicMock()
        mock.startup_report = {'stage': 'ready', 'ready': True, 'launch_tested': True,
                               'executable_path': '/verified/chrome', 'executable_exists': True,
                               'mode': 'headed', 'playwright_version': '1.57.0'}
        mock.page.set_content.side_effect = RuntimeError('browser crashed')
        with patch('vibe_job_radar.guided.browser.PlaywrightBackend', return_value=mock):
            result = probe_browser()
        self.assertEqual(result['stage'], 'blank_page_content')
        self.assertEqual(result['code'], 'browser_check_failed')
        self.assertTrue(result['launch_tested'])
        self.assertTrue(result['executable_exists'])
        self.assertEqual(result['executable_path'], '/verified/chrome')
        self.assertFalse(result['ready'])
        mock.close.assert_called_once()

    def test_title_failure_keeps_metadata_and_closes_browser(self):
        mock = MagicMock()
        mock.startup_report = {'stage': 'ready', 'ready': True, 'launch_tested': True,
                               'executable_path': '/verified/chrome', 'executable_exists': True,
                               'mode': 'headed', 'playwright_version': '1.57.0+conda'}
        mock.page.title.return_value = 'wrong title'
        with patch('vibe_job_radar.guided.browser.PlaywrightBackend', return_value=mock):
            result = probe_browser()
        self.assertEqual(result['playwright_version'], '1.57.0+conda')
        self.assertTrue(result['launch_tested'])
        self.assertEqual(result['stage'], 'blank_page_verify')
        self.assertIn('blank page verification failed', result['error_summary'])
        mock.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
