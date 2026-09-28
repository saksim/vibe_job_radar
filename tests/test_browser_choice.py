"""Offline contract tests. No system-browser install or native crash simulation as fact."""
from __future__ import annotations
import json
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from vibe_job_radar.guided.browser_choice import BrowserChoice, validate_choice
from vibe_job_radar.guided.browser_health import environment_report, failed_report, BrowserStartupError
from vibe_job_radar.guided.browser import PlaywrightBackend
from vibe_job_radar.guided.adapters import builtins
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import Workspace, InputError


def ok():
    return {**environment_report(), 'code': 'browser_ready', 'ready': True,
            'message': '已通过空白页启动检查', 'launch_tested': True, 'stage': 'ready'}


def crash():
    return failed_report({**environment_report(), 'stage':'launch'}, RuntimeError(
        '<launched> pid=17\n[pid=17] <process did exit: exitCode=3221226356, signal=null>'))


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=BrowserChoice(self.root)

    def test_constructor_and_read_do_not_write_or_launch(self):
        with patch('subprocess.Popen',side_effect=AssertionError),patch('socket.getaddrinfo',side_effect=AssertionError):
            self.assertEqual(self.store.read()['selected'],'bundled')
        self.assertFalse(self.store.path.exists())

    def test_failed_probe_cannot_change_selection(self):
        with self.assertRaises(InputError): self.store.record(crash(),'msedge',select=True)
        self.assertFalse(self.store.path.exists())

    def test_history_survives_restart_but_is_not_current_health(self):
        self.store.record(crash(),'bundled')
        data=BrowserChoice(self.root).read()
        last=self.store.historical_view(data,environment_report()['playwright_version'])
        self.assertEqual(last['exit_hex'],'0xC0000374');self.assertTrue(last['historical_only'])
        self.assertEqual(data['selected'],'bundled')

    def test_history_does_not_store_logs_paths_commands_or_credentials(self):
        report={**crash(),'python':'C:/Users/PRIVATE/person/python.exe','error_summary':'Cookie: SECRET',
                'executable_path':'PRIVATE.exe','commands':{'x':'PRIVATE'}}
        self.store.record(report,'bundled')
        raw=self.store.path.read_text(encoding='utf-8')
        for marker in ('PRIVATE','SECRET','Cookie','error_summary','commands','executable_path'):
            self.assertNotIn(marker,raw)

    def test_successful_explicit_choice_survives_new_store(self):
        self.store.record(ok(),'msedge',select=True)
        self.assertEqual(BrowserChoice(self.root).read()['selected'],'msedge')

    def test_successful_probe_alone_does_not_change_choice(self):
        self.store.record(ok(),'msedge')
        self.assertEqual(self.store.read()['selected'],'bundled')

    def test_unknown_channels_and_paths_rejected(self):
        for value in ('chrome-beta','msedge-dev','https://example.com','C:/msedge.exe',None,True,[],{}):
            with self.subTest(value=value),self.assertRaises(InputError):validate_choice(value)

    def test_corrupt_or_future_file_is_not_default_choice(self):
        for raw in ('[]','null','not-json','{"schema_version":true}',
                    '{"schema_version":2,"selected":"bundled","last_check":null}'):
            self.store.path.write_text(raw,encoding='utf-8')
            with self.assertRaises(InputError):self.store.read()

    def test_oversized_file_rejected(self):
        self.store.path.write_text(' '*8193,encoding='utf-8')
        with self.assertRaises(InputError):self.store.read()

    def test_symbolic_link_refused_without_read_or_overwrite(self):
        with patch.object(Path,'is_symlink',return_value=True):
            with self.assertRaises(InputError):self.store.read()
            with self.assertRaises(InputError):self.store.record(ok(),'msedge',select=True)

    def test_atomic_save_failure_keeps_original_selection(self):
        self.store.record(ok(),'bundled')
        before=self.store.path.read_bytes()
        with patch('vibe_job_radar.guided.browser_choice.atomic_json',side_effect=OSError):
            with self.assertRaises(OSError):self.store.record(ok(),'msedge',select=True)
        self.assertEqual(before,self.store.path.read_bytes())

    def test_changed_sdk_or_interpreter_does_not_reuse_old_check(self):
        self.store.record(ok(),'msedge',select=True)
        self.assertFalse(self.store.historical_view(self.store.read(),'different')['matches_environment'])
        with patch('vibe_job_radar.guided.browser_choice.interpreter_id',return_value='0'*64):
            self.assertFalse(self.store.historical_view(self.store.read(),environment_report()['playwright_version'])['matches_environment'])

    def test_history_rejects_arbitrary_fields(self):
        self.store.record(ok(),'bundled')
        d=self.store.read();d['last_check']['private']='secret'
        self.store.path.write_text(json.dumps(d),encoding='utf-8')
        with self.assertRaises(InputError):self.store.read()


class ServiceChoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.ws=Workspace(self.tmp.name);self.probe=MagicMock(return_value=ok())
        self.installer=MagicMock(side_effect=AssertionError('must not install'))
        self.service=GuidedService(self.ws,health_probe=self.probe,installer=self.installer)
        self.addCleanup(self.service.close)

    def wait(self):
        deadline=time.monotonic()+5
        while self.service.state()['busy'] and time.monotonic()<deadline:time.sleep(.01)
        self.assertFalse(self.service.state()['busy'])

    def choose(self,channel='msedge'):
        self.service.check_browser({'channel':channel,'consent':True});self.wait()

    def test_start_is_unverified_not_uninstalled(self):
        s=self.service.state()
        self.assertEqual(s['installation'],'not_started')
        self.assertEqual(s['installation_scope'],'current_process_actions_only_not_component_readiness')
        self.assertFalse(s['browser_health']['ready']);self.probe.assert_not_called()

    def test_successful_choice_is_used_by_recheck_without_installer(self):
        self.choose();self.service.check_browser({});self.wait()
        self.assertEqual(self.probe.call_args_list[0].kwargs,{'channel':'msedge'})
        self.assertEqual(self.probe.call_args_list[1].kwargs,{'channel':'msedge'})
        self.installer.assert_not_called();self.assertFalse(self.ws.db.exists())
        self.assertEqual(self.service.ledger.summary('boss')['page']['day'],0)

    def test_restart_restores_choice_not_ready_status(self):
        self.choose();self.service.close()
        new=GuidedService(self.ws,health_probe=self.probe);self.addCleanup(new.close)
        s=new.state();self.assertEqual(s['browser_choice']['selected'],'msedge')
        self.assertFalse(s['browser_health']['ready'])
        self.assertTrue(s['browser_choice']['last_check']['historical_only'])
        self.assertEqual(s['installation'],'not_started')
        self.probe.assert_called_once()

    def test_crash_survives_restart_without_reinstalling(self):
        self.probe.return_value=crash();self.service.check_browser({});self.wait();self.service.close()
        new=GuidedService(self.ws,health_probe=self.probe);self.addCleanup(new.close)
        self.assertEqual(new.state()['browser_choice']['last_check']['exit_hex'],'0xC0000374')
        self.probe.assert_called_once();self.installer.assert_not_called()

    def test_failed_alternative_is_not_silently_saved_or_default_retried(self):
        self.probe.return_value=crash();self.choose()
        s=self.service.state();self.assertEqual(s['browser_choice']['selected'],'bundled')
        self.assertEqual(s['browser_health']['browser_channel'],'msedge')
        self.assertFalse(s['browser_health']['selection_applied']);self.probe.assert_called_once()

    def test_selection_requires_boolean_consent_and_only_fixed_fields(self):
        for d in ({'channel':'msedge'},{'channel':'msedge','consent':1},
                  {'channel':'msedge','consent':True,'url':'https://example.com'},
                  {'channel':'chrome-beta','consent':True},{'executable_path':'anything'}):
            with self.subTest(d=d),self.assertRaises(InputError):self.service.check_browser(d)
        self.probe.assert_not_called()

    def test_live_context_is_never_replaced(self):
        self.service._backends['test']=MagicMock()
        with self.assertRaises(InputError):self.choose()
        self.probe.assert_not_called();self.service._backends.clear()

    def test_selection_blocked_until_sdk_restart(self):
        self.service._restart_required=True;self.choose()
        self.assertEqual(self.service.state()['browser_choice']['selected'],'bundled')
        self.probe.assert_not_called()

    def test_save_error_never_claims_choice_applied(self):
        with patch.object(self.service._choice,'record',side_effect=OSError):self.choose()
        s=self.service.state();self.assertFalse(s['browser_health']['ready'])
        self.assertEqual(s['browser_choice']['selected'],'bundled')

    def test_saved_choice_reaches_actual_collection_factory(self):
        self.choose();factory=MagicMock();factory.return_value.startup_report=ok()
        self.service.factory=factory
        self.service._backend({'id':'test','platform':'liepin'})
        self.assertEqual(factory.call_args.kwargs,{'channel':'msedge'})
        self.assertEqual(factory.call_args.args[0].key,'liepin')
        self.service._backends.clear()

    def test_corrupt_choice_blocks_browser_not_offline_workspace(self):
        self.service.close();self.service._choice.path.write_text('bad',encoding='utf-8')
        new=GuidedService(self.ws,health_probe=self.probe);self.addCleanup(new.close)
        self.assertIsNone(new.state()['browser_choice']['selected'])
        with self.assertRaises(BrowserStartupError):new._backend({'id':'x','platform':'liepin'})
        self.probe.assert_not_called();self.ws.status()


class BackendChannelTests(unittest.TestCase):
    def setUp(self):
        self.runtime=MagicMock();self.runtime.chromium.executable_path='/missing/bundled/chrome'
        sync=MagicMock();sync.return_value.start.return_value=self.runtime
        module=types.ModuleType('playwright.sync_api');module.sync_playwright=sync
        p=patch.dict(sys.modules,{'playwright':types.ModuleType('playwright'),'playwright.sync_api':module})
        p.start();self.addCleanup(p.stop)
        p=patch('vibe_job_radar.guided.browser.environment_report',return_value={**environment_report(),'playwright_version':'1.63.0'})
        p.start();self.addCleanup(p.stop)

    def backend(self,channel='msedge'):
        return PlaywrightBackend(builtins().get('liepin'),None,threading.Event(),channel=channel,transport_factory=MagicMock())

    def test_edge_does_not_require_the_missing_bundled_executable(self):
        b=self.backend();self.addCleanup(b.close)
        kw=self.runtime.chromium.launch.call_args.kwargs
        self.assertEqual(kw['channel'],'msedge');self.assertFalse(kw['headless'])
        self.assertNotIn('executable_path',kw);self.assertNotIn('ignore_default_args',kw)
        self.assertNotIn('user_data_dir',kw)
        self.assertEqual(b.startup_report['browser_channel'],'msedge')
        self.runtime.chromium.launch_persistent_context.assert_not_called()
        self.assertEqual(self.runtime.chromium.launch.return_value.new_context.call_args.kwargs,
                         {'service_workers':'block','accept_downloads':False})

    def test_absent_edge_reports_missing_channel_without_default_retry(self):
        self.runtime.chromium.launch.side_effect=RuntimeError("Executable doesn't exist for msedge")
        with self.assertRaises(BrowserStartupError) as e:self.backend()
        self.assertEqual(e.exception.report['code'],'browser_channel_missing')
        self.runtime.chromium.launch.assert_called_once()

    def test_unknown_channels_cannot_start_driver(self):
        for ch in ('chrome-beta','msedge-beta','/some/path',True):
            with self.assertRaises(ValueError):self.backend(ch)
        self.runtime.chromium.launch.assert_not_called()

    def test_policy_or_heap_failure_does_not_trigger_fallback(self):
        self.runtime.chromium.launch.side_effect=PermissionError('Access is denied')
        with self.assertRaises(BrowserStartupError) as e:self.backend()
        self.assertEqual(e.exception.report['code'],'browser_permission_denied')
        self.runtime.chromium.launch.assert_called_once()


if __name__=='__main__':unittest.main()
