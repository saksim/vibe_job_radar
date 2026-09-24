"""No downloads or external network. Startup phases and installer failures are explicit."""
from __future__ import annotations

import builtins as python_builtins
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.browser_health import (BrowserStartupError, environment_report,
    failed_report, probe_browser, safe_text, supported_version)
from vibe_job_radar.guided.browser_install import CommandResult, install_commands, run_command
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import InputError, Workspace


class HealthTests(unittest.TestCase):
    def test_same_named_python_chromium_is_not_readiness(self):
        with patch('vibe_job_radar.guided.browser_health.package_version', side_effect=lambda n: '1.57.0' if n == 'playwright' else '0.0.0'):
            info = environment_report()
        self.assertEqual(info['chromium_python_package'], '0.0.0')
        self.assertTrue(info['warnings']); self.assertFalse(info['ready'])
        self.assertFalse(info['launch_tested']); self.assertIsNone(info['executable_exists'])

    def test_diagnostics_do_not_dump_environment(self):
        with patch.dict(os.environ, {'PRIVATE_PASSWORD': 'DO-NOT-DUMP', 'PLAYWRIGHT_BROWSERS_PATH': 'https://u:secret@host/?token=secret'}):
            info = environment_report()
        self.assertNotIn('DO-NOT-DUMP', json.dumps(info))
        self.assertNotIn('u:secret', json.dumps(info))

    def test_supported_version_accepts_user_157(self):
        for version in ('1.48.0', '1.57.0', '1.62.0'):
            self.assertTrue(supported_version(version))
        for version in ('1.47.0', '2.0.0', None, 'garbage'):
            self.assertFalse(supported_version(version))

    def test_safe_output_redacts_urls_auth_and_credential_fields(self):
        raw = ('https://user:pass@host/path?token=secret\nAuthorization: Bearer abcdef\n'
               'Cookie: session=abcdef\napi_key="foobar" password=abc\nnormal: install failed')
        value = safe_text(raw)
        for secret in ('pass@', 'secret', 'abcdef', 'foobar', 'password=abc'):
            self.assertNotIn(secret, value)
        self.assertIn('install failed', value)

    def test_safe_output_is_bounded_and_strips_ansi(self):
        self.assertEqual(len(safe_text('x'*10000, 100)), 100)
        self.assertEqual(safe_text('\x1b[31merror\x1b[0m'), 'error')

    def test_exception_classification_distinguishes_failures(self):
        cases = [('driver', RuntimeError('spawn failed'), 'playwright_driver_failed'),
                 ('launch', RuntimeError("Executable doesn't exist"), 'browser_executable_missing'),
                 ('launch', PermissionError('blocked'), 'browser_permission_denied'),
                 ('launch', RuntimeError('Missing X server or $DISPLAY'), 'browser_display_unavailable'),
                 ('launch', TimeoutError('timeout'), 'browser_launch_timeout'),
                 ('context', AttributeError('route_web_socket'), 'browser_context_failed'),
                 ('launch', RuntimeError('unknown failure'), 'browser_launch_failed')]
        for stage, exc, code in cases:
            with self.subTest(stage=stage, code=code):
                result = failed_report({'stage': stage}, exc)
                self.assertEqual(result['code'], code)
                self.assertFalse(result['ready']); self.assertIn('error_type', result)

    def test_only_playwright_module_absence_means_package_missing(self):
        a = failed_report({'stage': 'import'}, ModuleNotFoundError('missing', name='playwright'))
        b = failed_report({'stage': 'import'}, ModuleNotFoundError('missing', name='greenlet'))
        self.assertEqual(a['code'], 'playwright_missing')
        self.assertEqual(b['code'], 'playwright_import_failed')

    def test_commands_use_actual_interpreter_and_do_not_install_chromium_package(self):
        with patch('sys.executable', 'D:/Anaconda Space/py312/python.exe'):
            commands = install_commands()
            info = environment_report()
        self.assertTrue(all(c[1][0] == 'D:/Anaconda Space/py312/python.exe' for c in commands))
        self.assertEqual(commands[1][1][1:], ['-m','playwright','install','chromium'])
        self.assertNotIn('pip install Chromium', info['commands']['browser'])


class StartupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.exe = Path(self.tmp.name)/'chrome.exe'; self.exe.touch()
        self.runtime = MagicMock()
        self.runtime.chromium.executable_path = str(self.exe)
        self.sync = MagicMock(); self.sync.return_value.start.return_value = self.runtime
        module = types.ModuleType('playwright.sync_api'); module.sync_playwright = self.sync
        self.modules = patch.dict(sys.modules, {'playwright': types.ModuleType('playwright'), 'playwright.sync_api': module})
        self.modules.start(); self.addCleanup(self.modules.stop)
        self.env = patch('vibe_job_radar.guided.browser.environment_report', side_effect=lambda: {'playwright_version':'1.57.0', 'launch_tested':False,'ready':False})
        self.env.start(); self.addCleanup(self.env.stop)

    def backend(self):
        return PlaywrightBackend(builtins().get('boss'), None, threading.Event(), transport_factory=lambda *a: MagicMock())

    def test_missing_binary_never_attempts_launch(self):
        self.exe.unlink()
        with self.assertRaises(BrowserStartupError) as error: self.backend()
        self.assertEqual(error.exception.code, 'browser_executable_missing')
        self.assertEqual(error.exception.report['stage'], 'executable')
        self.assertFalse(error.exception.report['launch_tested'])
        self.runtime.chromium.launch.assert_not_called(); self.runtime.stop.assert_called_once()

    def test_driver_error_preserves_cause_and_cleanup(self):
        self.sync.return_value.start.side_effect = RuntimeError('driver broken')
        with self.assertRaises(BrowserStartupError) as error: self.backend()
        self.assertEqual(error.exception.code, 'playwright_driver_failed')
        self.assertIn('driver broken', error.exception.report['error_summary'])

    def test_launch_error_not_mislabeled_missing(self):
        self.runtime.chromium.launch.side_effect = RuntimeError('application exited')
        with self.assertRaises(BrowserStartupError) as error: self.backend()
        self.assertEqual(error.exception.code, 'browser_launch_failed')
        self.assertTrue(error.exception.report['executable_exists'])
        self.runtime.stop.assert_called_once()

    def test_context_error_not_mislabeled_missing(self):
        self.runtime.chromium.launch.return_value.new_context.side_effect = AttributeError('context bug')
        with self.assertRaises(BrowserStartupError) as error: self.backend()
        self.assertEqual(error.exception.code, 'browser_context_failed')
        self.runtime.chromium.launch.return_value.close.assert_called_once()

    def test_incompatible_version_stops_before_driver(self):
        with patch('vibe_job_radar.guided.browser.supported_version', return_value=False):
            with self.assertRaises(BrowserStartupError) as error: self.backend()
        self.assertEqual(error.exception.code, 'playwright_incompatible')
        self.sync.assert_not_called()

    def test_real_init_uses_headed_mode_and_records_success(self):
        backend = self.backend()
        self.assertTrue(backend.startup_report['ready'])
        self.assertFalse(self.runtime.chromium.launch.call_args.kwargs['headless'])
        self.assertEqual(backend.startup_report['mode'], 'headed')
        backend.close(); self.runtime.stop.assert_called_once()

    def test_probe_uses_backend_and_closes_without_site_navigation(self):
        backend = MagicMock(); backend.startup_report = {'ready':True, 'launch_tested':True}
        backend.page.title.return_value = 'Vibe Radar browser check'
        with patch('vibe_job_radar.guided.browser.PlaywrightBackend', return_value=backend) as factory:
            result = probe_browser()
        self.assertTrue(result['ready'])
        self.assertFalse(factory.call_args.kwargs['headless'])
        backend.open.assert_not_called(); backend.close.assert_called_once()
        wire = factory.call_args.kwargs['transport_factory']()
        self.assertFalse(wire.allowed_resource('https://www.zhipin.com/'))

    def test_blank_page_failure_preserves_exact_step_without_retry_or_timeout_change(self):
        for step in ('set_content', 'read_title', 'verify_title'):
            with self.subTest(step=step):
                backend=MagicMock();backend.startup_report={'ready':True,'launch_tested':True}
                backend.page.title.return_value='Vibe Radar browser check'
                backend.page.is_closed.return_value=False
                backend.browser.is_connected.return_value=True
                if step=='set_content':backend.page.set_content.side_effect=TimeoutError('auth_token=private')
                elif step=='read_title':backend.page.title.side_effect=TimeoutError('auth_token=private')
                else:backend.page.title.return_value='private unexpected title'
                with patch('vibe_job_radar.guided.browser.PlaywrightBackend',return_value=backend):
                    result=probe_browser()
                self.assertFalse(result['ready']);self.assertEqual(result['stage'],'blank_page')
                self.assertEqual(result['blank_page_check']['step'],step)
                self.assertEqual(result['blank_page_check']['page_closed'],False)
                self.assertEqual(result['blank_page_check']['browser_connected'],True)
                self.assertIn('set_content',result['blank_page_check']['elapsed_ms'])
                self.assertNotIn('private',json.dumps(result))
                backend.page.set_content.assert_called_once()
                self.assertEqual(backend.page.set_content.call_args.kwargs,{})
                self.assertEqual(backend.page.title.call_count,0 if step=='set_content' else 1)
                backend.page.set_default_timeout.assert_not_called()
                backend.page.goto.assert_not_called();backend.close.assert_called_once()

    def test_blank_page_lifecycle_observations_exclude_our_cleanup(self):
        backend=MagicMock();backend.startup_report={'ready':True,'launch_tested':True}
        backend.page.title.return_value='Vibe Radar browser check'
        backend.page.is_closed.return_value=False;backend.browser.is_connected.return_value=True
        page_events={};browser_events={}
        backend.page.on.side_effect=lambda name,callback:page_events.update({name:callback})
        backend.browser.on.side_effect=lambda name,callback:browser_events.update({name:callback})
        backend.page.set_content.side_effect=lambda *a: [page_events[name]('private ignored event') for name in ('domcontentloaded','load')]
        backend.close.side_effect=lambda: [page_events['close'](),browser_events['disconnected']()]
        with patch('vibe_job_radar.guided.browser.PlaywrightBackend',return_value=backend):
            result=probe_browser()
        self.assertTrue(result['ready']);detail=result['blank_page_check']
        self.assertEqual(detail['step'],'verified')
        self.assertEqual(detail['events_before_cleanup'],
            {'domcontentloaded':1,'load':1,'crash':0,'close':0,'disconnected':0})
        self.assertEqual(set(detail['elapsed_ms']),{'set_content','read_title'})
        self.assertTrue(all(type(value) is int and value>=0 for value in detail['elapsed_ms'].values()))
        self.assertNotIn('private',json.dumps(detail));backend.close.assert_called_once()

    def test_blank_page_close_observation_cannot_replace_original_failure(self):
        backend=MagicMock();backend.startup_report={'ready':True,'launch_tested':True}
        def page_closed(*args):
            backend.page=None;backend.browser=None
            raise TimeoutError('artificial page closed during set_content')
        backend.page.set_content.side_effect=page_closed
        with patch('vibe_job_radar.guided.browser.PlaywrightBackend',return_value=backend):
            result=probe_browser()
        self.assertFalse(result['ready']);self.assertEqual(result['error_type'],'TimeoutError')
        self.assertEqual(result['blank_page_check']['step'],'set_content')
        self.assertTrue(result['blank_page_check']['page_closed'])
        self.assertFalse(result['blank_page_check']['browser_connected'])
        backend.close.assert_called_once()


class InstallerTests(unittest.TestCase):
    def test_nonzero_exit_keeps_bounded_redacted_output(self):
        args = [sys.executable, '-c', "print('https://u:secret@host/path?token=secret');print('failed to download');exit(7)"]
        with patch('vibe_job_radar.guided.browser_install.subprocess.Popen', wraps=subprocess.Popen) as popen:
            result = run_command(args, cancel=threading.Event(), timeout=10)
        self.assertFalse(popen.call_args.kwargs['shell'])
        self.assertEqual(result.returncode,7)
        self.assertNotIn('secret',result.output); self.assertIn('failed to download',result.output)

    def test_timeout_terminates_only_installer(self):
        result = run_command([sys.executable,'-c','import time;time.sleep(30)'], cancel=threading.Event(), timeout=.2)
        self.assertTrue(result.timed_out); self.assertNotEqual(result.returncode,0)

    def test_shutdown_cancels_installer(self):
        event=threading.Event(); event.set()
        result=run_command([sys.executable,'-c','import time;time.sleep(30)'],cancel=event,timeout=10)
        self.assertTrue(result.cancelled)

    def test_overlong_lines_cannot_leak_fragmented_secrets(self):
        command=[sys.executable,'-c',"print('Authorization: Bearer '+'Q'*20000);print('usable error')"]
        result=run_command(command,cancel=threading.Event(),timeout=10)
        self.assertNotIn('Q'*20,result.output);self.assertIn('usable error',result.output)
        self.assertLessEqual(len(result.output),12000)


class ServiceHealthTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ws=Workspace(self.tmp.name)
        self.probe=MagicMock(return_value={**environment_report(),'ready':True,'code':'browser_ready','message':'verified'})
        self.installer=MagicMock(return_value=CommandResult(0,'download complete'))
        self.service=GuidedService(self.ws,health_probe=self.probe,installer=self.installer)
        self.addCleanup(self.service.close)
    def wait(self):
        deadline=time.monotonic()+5
        while self.service.state()['busy'] and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.service.state()['busy'])

    def test_check_does_not_install_collect_or_spend_quota(self):
        self.service.check_browser({});self.wait()
        self.probe.assert_called_once();self.installer.assert_not_called()
        self.assertFalse(self.ws.db.exists())
        self.assertTrue(self.service.state()['browser_health']['ready'])
        self.assertEqual(self.service.ledger.summary('boss')['page']['day'],0)

    def test_no_browser_ready_claim_until_probe_succeeds(self):
        self.probe.return_value={**environment_report(),'code':'browser_executable_missing','message':'missing'}
        self.service.install({'consent':True});self.wait()
        self.assertEqual(self.installer.call_count,2)
        self.assertEqual(self.service.state()['installation'],'installed_not_ready')
        self.assertFalse(self.service.state()['browser_health']['ready'])

    def test_failed_download_stops_and_shows_stage(self):
        self.installer.side_effect=[CommandResult(0,'ok'),CommandResult(1,'download error')]
        self.service.install({'consent':True});self.wait()
        self.probe.assert_not_called()
        state=self.service.state()
        self.assertEqual(state['browser_health']['stage'],'browser_download')
        self.assertIn('download error',state['setup']['steps'][-1]['log'])
        self.assertFalse(state['browser_health']['ready'])

    def test_package_failure_does_not_attempt_download(self):
        self.installer.return_value=CommandResult(1,'package denied')
        self.service.install({'consent':True});self.wait()
        self.assertEqual(self.installer.call_count,1);self.probe.assert_not_called()

    def test_install_timeout_has_distinct_code(self):
        self.installer.return_value=CommandResult(-1,'timeout',timed_out=True)
        self.service.install({'consent':True});self.wait()
        self.assertEqual(self.service.state()['installation'],'dependency_install_timeout')

    def test_install_progress_is_visible_and_already_sanitized(self):
        entered=threading.Event();release=threading.Event()
        def installer(*a,progress,**kw):
            progress('https://u:secret@host/path\ncurrent download progress')
            entered.set();release.wait(3);return CommandResult(0,'ok')
        self.service._installer=installer
        self.service.install({'consent':True});self.assertTrue(entered.wait(2))
        state=self.service.state();release.set();self.wait()
        self.assertIn('current download progress',state['setup']['steps'][0]['log'])
        self.assertNotIn('secret',json.dumps(state))

    def test_manual_install_followed_by_check_refreshes_stale_failure(self):
        self.probe.return_value={**environment_report(),'code':'browser_executable_missing','message':'missing'}
        self.service.check_browser({});self.wait();self.assertFalse(self.service.state()['browser_health']['ready'])
        self.probe.return_value={**environment_report(),'code':'browser_ready','ready':True,'message':'verified'}
        self.service.check_browser({});self.wait();self.assertTrue(self.service.state()['browser_health']['ready'])

    def test_check_rejects_remote_url_or_executable_parameters(self):
        with self.assertRaises(InputError):self.service.check_browser({'url':'https://example.com'})
        with self.assertRaises(InputError):self.service.check_browser({'executable_path':'C:/other.exe'})
        self.probe.assert_not_called()

    def test_setup_does_not_close_live_sessions_without_user_action(self):
        backend=MagicMock();self.service._backends['existing']=backend
        with self.assertRaises(InputError):self.service.check_browser({})
        with self.assertRaises(InputError):self.service.install({'consent':True})
        backend.close.assert_not_called();self.service._backends.clear()

    def test_probe_errors_leave_worker_reusable(self):
        self.probe.side_effect=RuntimeError('probe failure')
        self.service.check_browser({});self.wait()
        self.assertEqual(self.service.state()['browser_health']['code'],'browser_check_failed')
        self.probe.side_effect=None;self.service.check_browser({});self.wait()
        self.assertTrue(self.service.state()['browser_health']['ready'])

    def test_startup_failure_records_diagnosis_not_generic_browser_missing(self):
        report=failed_report({**environment_report(),'stage':'launch'},PermissionError('access denied'))
        self.service.factory=MagicMock(side_effect=BrowserStartupError(report))
        ident=self.service.create({'platform':'boss','keyword':'时间序列','consent':True,'rights_note':'unit test fixture'})['id']
        self.wait();job=self.service._load(ident)
        self.assertEqual(job['code'],'browser_permission_denied')
        self.assertEqual(job['startup_diagnostic']['stage'],'launch')
        self.assertEqual(self.service.ledger.summary('boss')['request']['day'],0)


if __name__=='__main__':unittest.main()
