
"""Component checks run before opening a workspace; packaging requires both explicit modes."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

def good_component(channel):
    return {'success':True,'runtime':'portable','browser_channel':channel,
        'controller':'minimal_cdp' if channel=='chrome' else 'playwright_public_cdp',
        'minimal_controller':channel=='chrome','code':'native_component_ready','stage':'passed',
        'browser_version':'156.0.0.0','returncode':0,'blank_page_check':True,
        'request_guard_check':True,'cleanup_verified':True,'external_connections':0,
        'live_sites_certified':False,'cleanup':{
            'attempted':True,'close_returned':True,'close_error_type':'','browser_disconnected':True,
            'tunnel_closed':True,'tunnel_thread_stopped':True,
            **({'profile_removed':True,'profile_cleanup_ok':True,'bridge_exited':True} if channel=='chrome' else {})}}

def scripts():
    root=Path(__file__).resolve().parents[1]/'scripts'
    with patch.object(sys,'path',[str(root),*sys.path]):
        import build_windows_portable as builder
        import verify_windows_portable as verifier
    return builder,verifier

class NativeComponentPortableTests(unittest.TestCase):
    def test_cli_uses_explicit_mode_without_opening_workspace_or_server(self):
        from vibe_job_radar.workbench import main
        with patch('vibe_job_radar.workbench.Workspace') as workspace, \
             patch('vibe_job_radar.workbench.LocalServer') as server, \
             patch('vibe_job_radar.guided.native_check.run_cli',return_value=0) as run:
            self.assertEqual(main(['--native-browser-check','--native-browser-channel','chrome']),0)
        run.assert_called_once_with(channel='chrome');workspace.assert_not_called();server.assert_not_called()

    def test_cli_conflicting_flags_never_launch(self):
        from vibe_job_radar.workbench import main
        with patch('vibe_job_radar.guided.native_check.run_cli') as run, contextlib.redirect_stderr(io.StringIO()):
            for args in [['--native-browser-check','--doctor'],['--native-browser-check','--public-worker'],['--native-browser-check','--port','1'],
                         ['--native-browser-channel','chrome'],['--native-browser-check','--native-browser-channel','chrome-beta']]:
                with self.subTest(args=args),self.assertRaises(SystemExit):main(args)
        run.assert_not_called()

    def test_cli_prints_report_and_propagates_failed_check(self):
        from vibe_job_radar.guided.native_check import run_cli
        row={'success':False,'code':'native_check_failed'}
        with patch('vibe_job_radar.guided.native_check.check_native_browser',return_value=row) as check, \
             contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(run_cli(channel='chrome'),2)
        self.assertEqual(json.loads(out.getvalue()),row);check.assert_called_once_with(channel='chrome')

    def test_qualification_requires_both_current_modes_and_complete_facts(self):
        builder,_=scripts();rows={c:good_component(c) for c in ['bundled','chrome']}
        self.assertTrue(builder.native_components_valid(rows))
        for broken in [None,{}, {'bundled':rows['bundled']}]:
            self.assertFalse(builder.native_components_valid(broken))
        for key,value in [('cleanup_verified',False),('external_connections',True),('external_connections',1),
                          ('controller','playwright_public_cdp'),('runtime','source'),('returncode',True),
                          ('minimal_controller',False),('browser_version','PRIVATE')]:
            with self.subTest(key=key):
                bad={**rows,'chrome':{**rows['chrome'],key:value}}
                self.assertFalse(builder.native_components_valid(bad))

    def test_verifier_executes_target_and_drops_unknown_fields(self):
        _,verifier=scripts();row={**good_component('chrome'),'unknown':'PRIVATE-INPUT'}
        with patch.object(verifier.subprocess,'CREATE_NO_WINDOW',0,create=True), \
             patch.object(verifier.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(row).encode())) as run:
            proof=verifier.verify_native_component(Path('owned.exe'),Path('cwd'),{'PATH':'fixed'},'chrome')
        self.assertEqual(run.call_args.args[0],['owned.exe','--native-browser-check','--native-browser-channel','chrome'])
        self.assertEqual(run.call_args.kwargs['timeout'],60)
        self.assertNotIn('PRIVATE',str(proof));self.assertEqual(proof,good_component('chrome'))

    def test_verifier_preserves_failure_as_failure(self):
        _,verifier=scripts();row=good_component('chrome')
        for code,change in [(2,{}),(0,{'cleanup_verified':False}),(0,{'browser_channel':'bundled'})]:
            with self.subTest(code=code,change=change), \
                 patch.object(verifier.subprocess,'CREATE_NO_WINDOW',0,create=True), \
                 patch.object(verifier.subprocess,'run',return_value=SimpleNamespace(returncode=code,stdout=json.dumps({**row,**change}).encode())):
                with self.assertRaises(AssertionError):verifier.verify_native_component(Path('owned.exe'),Path('cwd'),{},'chrome')

    def test_failed_portable_check_retains_typed_cleanup_in_caller_evidence(self):
        _,verifier=scripts();good=good_component('chrome');cleanup={**good['cleanup'],'profile_removed':False,'tunnel_thread_stopped':None,'private_path':'SECRET'}
        row={**good,'success':False,'stage':'cleanup','cleanup_verified':False,'cleanup':cleanup};evidence={}
        with patch.object(verifier.subprocess,'CREATE_NO_WINDOW',0,create=True), \
             patch.object(verifier.subprocess,'run',return_value=SimpleNamespace(returncode=2,stdout=json.dumps(row).encode(),stderr=b'SECRET')) as run:
            with self.assertRaises(AssertionError):verifier.verify_native_component(Path('owned.exe'),Path('cwd'),{},'chrome',evidence=evidence)
        self.assertEqual(evidence['cleanup'],{k:v for k,v in cleanup.items() if k!='private_path'})
        self.assertEqual(evidence['returncode'],2);self.assertEqual(evidence['stage'],'cleanup')
        self.assertNotIn('SECRET',str(evidence));run.assert_called_once();self.assertEqual(run.call_args.kwargs['timeout'],60)

    def test_failure_metadata_filters_mistyped_fields_and_raw_error_strings(self):
        _,verifier=scripts();row={k:'SECRET INPUT' for k in good_component('chrome')}
        row['cleanup']={'attempted':'SECRET','close_returned':1,'close_error_type':'SECRET PATH','unknown':'SECRET'};evidence={}
        with patch.object(verifier.subprocess,'CREATE_NO_WINDOW',0,create=True), \
             patch.object(verifier.subprocess,'run',return_value=SimpleNamespace(returncode=2,stdout=json.dumps(row).encode(),stderr=b'SECRET')):
            with self.assertRaises(AssertionError):verifier.verify_native_component(Path('owned.exe'),Path('cwd'),{},'chrome',evidence=evidence)
        self.assertNotIn('SECRET',str(evidence));self.assertIsNone(evidence['success']);self.assertIsNone(evidence['external_connections'])
        self.assertIsNone(evidence['cleanup']['attempted']);self.assertIsNone(evidence['cleanup']['close_returned'])
        self.assertEqual(evidence['cleanup']['close_error_type'],'unrecognized');self.assertEqual(evidence['code'],'unrecognized')

    def test_success_flag_cannot_override_missing_unknown_or_failed_cleanup(self):
        builder,verifier=scripts()
        for channel in ['bundled','chrome']:
            good=good_component(channel)
            cases=[None,{},*({**good['cleanup'],field:value} for field in good['cleanup'] for value in
                (['PermissionError',None] if field=='close_error_type' else [False,None,1]))]
            for cleanup in cases:
                with self.subTest(channel=channel,cleanup=cleanup):
                    row={**good,'cleanup':cleanup};self.assertFalse(builder.native_component_valid(row,channel))
                    with patch.object(verifier.subprocess,'CREATE_NO_WINDOW',0,create=True), \
                         patch.object(verifier.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout=json.dumps(row).encode())):
                        with self.assertRaises(AssertionError):verifier.verify_native_component(Path('owned.exe'),Path('cwd'),{},channel)

    def test_portable_parent_report_keeps_failed_second_mode_without_retry(self):
        _,verifier=scripts();good=good_component('bundled');bad=good_component('chrome')
        bad={**bad,'success':False,'cleanup_verified':False,'stage':'cleanup','cleanup':{**bad['cleanup'],'bridge_exited':False}}
        replies=[SimpleNamespace(returncode=0,stdout=json.dumps(good).encode()),SimpleNamespace(returncode=2,stdout=json.dumps(bad).encode())]
        result={'checks':[]}
        with patch.object(verifier.subprocess,'CREATE_NO_WINDOW',0,create=True),patch.object(verifier.subprocess,'run',side_effect=replies) as run:
            with self.assertRaises(AssertionError):verifier.verify_native_components(Path('owned.exe'),Path('cwd'),{},result)
        self.assertEqual(run.call_count,2);self.assertEqual(result['stage'],'native_component_chrome');self.assertEqual(len(result['checks']),1)
        self.assertEqual(result['native_components']['bundled'],good)
        self.assertEqual(result['native_components']['chrome'],{**bad,'returncode':2})
