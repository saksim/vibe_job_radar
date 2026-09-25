"""Component failure/cleanup and CLI boundaries; actual frozen process runs in CI."""
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from vibe_job_radar.guided import native_check
from vibe_job_radar.guided.browser_health import BrowserStartupError
from vibe_job_radar.workbench import main


class NativeComponentCheckTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        profile=Path(self.tmp.name)/'owned';profile.mkdir()
        controller=SimpleNamespace(minimal_events=True,version='153.0.1.2',profile=profile,
            cleanup_failed=False,process=Mock(poll=Mock(return_value=None)))
        tunnel=SimpleNamespace(connections=0,_closed=False,thread=Mock(is_alive=Mock(return_value=True)))
        page=Mock()
        backend=SimpleNamespace(browser=controller,tunnel=tunnel,page=page,
            context=SimpleNamespace(pages=[page]),_bound_pages={page:'session'},
            native_counts={k:0 for k in ('document','business','asset','robots','login','responses','blocked')},
            _requests={},_observations=[],error=None,_halted=False)
        def evaluate(expression):
            if 'fetch(' in expression:
                backend.error='resource_domain_blocked';backend._halted=True
                backend.native_counts['blocked']=1
                return 'blocked'
            return {'url':'about:blank','value':42}
        page.evaluate.side_effect=evaluate
        def close():
            profile.rmdir();controller.process.poll.return_value=0
            tunnel._closed=True;tunnel.thread.is_alive.return_value=False
        backend.close=Mock(side_effect=close)
        self.backend=backend

    def run_check(self):
        with patch.object(native_check,'NativeBackend',return_value=self.backend) as launch:
            row=native_check.check_native_browser()
        self.launch=launch
        return row

    def test_fresh_component_uses_no_network_rules_or_user_workspace(self):
        with patch('vibe_job_radar.network_policy.NetworkPolicy.capture',side_effect=AssertionError('must not read user network configuration')):
            row=self.run_check()
        self.assertTrue(row['success']);self.assertTrue(row['cleanup_verified'])
        self.assertEqual(row['external_connections'],0)
        self.assertFalse(row['live_sites_certified'])
        adapter=self.launch.call_args.args[0]
        self.assertFalse(adapter.native_contract.rules)
        self.assertEqual(adapter.native_contract.hosts,('native-check.invalid',))
        self.assertEqual(self.launch.call_args.kwargs,{'headless':True})
        self.backend.close.assert_called_once()

    def test_page_mismatch_fails_before_any_request_probe_and_still_cleans(self):
        self.backend.page.evaluate.side_effect=lambda _: {'url':'https://unexpected.invalid/','value':42}
        row=self.run_check()
        self.assertFalse(row['success']);self.assertEqual(row['stage'],'blank_page')
        self.assertTrue(row['cleanup_verified']);self.assertEqual(self.backend.page.evaluate.call_count,1)

    def test_nonminimal_controller_cannot_claim_native_success(self):
        self.backend.browser.minimal_events=False
        row=self.run_check();self.assertFalse(row['success'])
        self.assertFalse(row['minimal_controller']);self.assertTrue(row['cleanup_verified'])

    def test_nonzero_connection_cannot_pass_even_if_local_guard_returns_blocked(self):
        self.backend.tunnel.connections=1
        row=self.run_check();self.assertFalse(row['success'])
        self.assertEqual(row['external_connections'],1);self.assertTrue(row['cleanup_verified'])

    def test_network_error_without_expected_controller_refusal_cannot_pass(self):
        self.backend.page.evaluate.side_effect=[{'url':'about:blank','value':42},'blocked']
        row=self.run_check();self.assertFalse(row['success'])
        self.assertFalse(row['request_guard_check']);self.assertTrue(row['cleanup_verified'])

    def test_unclean_profile_cannot_pass_or_be_deleted_by_another_path(self):
        self.backend.close.side_effect=None
        row=self.run_check();self.assertFalse(row['success'])
        self.assertFalse(row['cleanup_verified']);self.assertTrue(self.backend.browser.profile.exists())
        self.backend.close.assert_called_once()

    def test_cleanup_preserves_each_failed_completion_fact(self):
        cases = ('profile_removed', 'profile_cleanup_failed', 'bridge_exited',
                 'tunnel_closed', 'tunnel_thread_stopped')
        original = self.backend.close.side_effect
        for field in cases:
            with self.subTest(field=field):
                profile = self.backend.browser.profile
                if not profile.exists():
                    profile.mkdir()
                self.backend.browser.cleanup_failed = False
                def close():
                    original()
                    if field == 'profile_removed':
                        profile.mkdir()
                    elif field == 'profile_cleanup_failed':
                        self.backend.browser.cleanup_failed = True
                    elif field == 'bridge_exited':
                        self.backend.browser.process.poll.return_value = None
                    elif field == 'tunnel_closed':
                        self.backend.tunnel._closed = False
                    else:
                        self.backend.tunnel.thread.is_alive.return_value = True
                self.backend.close.side_effect = close
                row = self.run_check()
                self.assertFalse(row['success']); self.assertFalse(row['cleanup_verified'])
                self.assertEqual(row['stage'], 'cleanup')
                expected = dict(attempted=True,close_returned=True,close_error_type='',
                    profile_removed=True,profile_cleanup_failed=False,bridge_exited=True,
                    tunnel_closed=True,tunnel_thread_stopped=True)
                expected[field] = not expected[field]
                self.assertEqual(row['cleanup'], expected)
        self.assertEqual(self.backend.close.call_count, len(cases))

    def test_close_exception_keeps_fixed_facts_and_never_qualifies(self):
        original = self.backend.close.side_effect
        def close():
            original()
            raise subprocess.TimeoutExpired('SECRET command and path', 10)
        self.backend.close.side_effect = close
        row = self.run_check()
        self.assertFalse(row['success']); self.assertFalse(row['cleanup_verified'])
        self.assertEqual(row['cleanup']['close_error_type'], 'TimeoutExpired')
        self.assertFalse(row['cleanup']['close_returned'])
        self.assertTrue(row['cleanup']['profile_removed'])
        self.assertTrue(row['cleanup']['bridge_exited'])
        self.assertNotIn('SECRET', json.dumps(row))
        self.backend.close.assert_called_once()

    def test_unavailable_cleanup_observation_is_unknown_not_success_or_exception_text(self):
        self.backend.browser.process.poll.side_effect = OSError('SECRET path')
        row = self.run_check()
        self.assertFalse(row['success']); self.assertFalse(row['cleanup_verified'])
        self.assertIsNone(row['cleanup']['bridge_exited'])
        self.assertTrue(row['cleanup']['profile_removed'])
        self.assertNotIn('SECRET', json.dumps(row))

    def test_startup_failure_keeps_only_known_error_code(self):
        error=BrowserStartupError({'code':'browser_executable_missing','message':'SECRET ACCOUNT /private/path'})
        with patch.object(native_check,'NativeBackend',side_effect=error):
            row=native_check.check_native_browser()
        self.assertFalse(row['success']);self.assertEqual(row['code'],'browser_executable_missing')
        self.assertNotIn('SECRET',json.dumps(row));self.assertNotIn('/private',json.dumps(row))
        self.assertIsNone(row['external_connections'])
        self.assertFalse(row['cleanup']['attempted'])
        self.assertIsNone(row['cleanup']['profile_removed'])

    def test_cli_returns_failure_without_opening_server_or_workspace(self):
        with patch.object(native_check,'check_native_browser',return_value={'success':False}), \
                patch('vibe_job_radar.workbench.Workspace') as workspace, \
                patch('vibe_job_radar.workbench.LocalServer') as server, redirect_stdout(io.StringIO()) as stream:
            self.assertEqual(main(['--native-browser-check','--workspace','ignored']),2)
        workspace.assert_not_called();server.assert_not_called()
        self.assertEqual(json.loads(stream.getvalue()),{'success':False})

    def test_cli_modes_are_exclusive_and_cannot_open_a_port(self):
        with patch.object(native_check,'run_cli') as run, patch('sys.stderr',io.StringIO()):
            for args in (['--native-browser-check','--doctor'],['--native-browser-check','--public-worker'],
                         ['--native-browser-check','--port','1234']):
                with self.subTest(args=args),self.assertRaises(SystemExit):main(args)
            run.assert_not_called()
