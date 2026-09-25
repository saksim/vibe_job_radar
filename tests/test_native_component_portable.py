"""Do not certify checkout results or lose safe evidence from a failed frozen process."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from verify_windows_portable import verify_native_component


class NativeComponentPortableTests(unittest.TestCase):
    def row(self):
        return dict(success=True,runtime='portable',code='native_component_ready',stage='passed',
            browser_channel='bundled',browser_version='153.0.1.2',minimal_controller=True,
            blank_page_check=True,request_guard_check=True,cleanup_verified=True,
            external_connections=0,live_sites_certified=False)

    def test_only_requested_executable_runs_and_safe_facts_are_retained(self):
        row=self.row();row['private_path']='SECRET';evidence={}
        reply=SimpleNamespace(returncode=0,stdout=json.dumps(row).encode(),stderr=b'SECRET')
        with patch('verify_windows_portable.subprocess.run',return_value=reply) as run, \
                patch('verify_windows_portable.subprocess.CREATE_NO_WINDOW',0,create=True):
            verify_native_component(Path('candidate.exe'),Path('separate'),{'PATH':'system'},evidence)
        self.assertEqual(run.call_args.args[0],['candidate.exe','--native-browser-check'])
        self.assertNotIn('SECRET',json.dumps(evidence));self.assertTrue(evidence['success'])
        self.assertEqual(run.call_count,1)

    def test_source_runtime_skipped_guard_or_unconfirmed_cleanup_cannot_qualify(self):
        for change in ({'runtime':'source'},{'request_guard_check':False},{'cleanup_verified':False},
                       {'external_connections':False},{'external_connections':1},
                       {'minimal_controller':1},{'browser_channel':'msedge'}):
            row={**self.row(),**change};evidence={}
            reply=SimpleNamespace(returncode=0,stdout=json.dumps(row).encode(),stderr=b'')
            with self.subTest(change=change),patch('verify_windows_portable.subprocess.run',return_value=reply), \
                    patch('verify_windows_portable.subprocess.CREATE_NO_WINDOW',0,create=True),self.assertRaises(AssertionError):
                verify_native_component(Path('candidate.exe'),Path('separate'),{},evidence)

    def test_failed_executable_keeps_stage_and_code_without_raw_output_or_retry(self):
        row={**self.row(),'success':False,'stage':'launch','code':'browser_executable_missing',
             'cleanup_verified':False,'browser_version':'SECRET','external_connections':None,'raw':'SECRET'}
        reply=SimpleNamespace(returncode=2,stdout=json.dumps(row).encode(),stderr=b'SECRET')
        evidence={}
        with patch('verify_windows_portable.subprocess.run',return_value=reply) as run, \
                patch('verify_windows_portable.subprocess.CREATE_NO_WINDOW',0,create=True),self.assertRaises(AssertionError):
            verify_native_component(Path('candidate.exe'),Path('separate'),{},evidence)
        self.assertEqual(evidence['returncode'],2);self.assertEqual(evidence['stage'],'launch')
        self.assertEqual(evidence['code'],'browser_executable_missing')
        self.assertNotIn('SECRET',json.dumps(evidence));self.assertEqual(run.call_count,1)

    def test_cleanup_failure_retains_only_typed_completion_facts_without_retry(self):
        cleanup=dict(attempted=True,close_returned=True,profile_removed=False,
            profile_cleanup_failed=True,bridge_exited=True,tunnel_closed=True,
            tunnel_thread_stopped=None,close_error_type='',private_path='SECRET')
        row={**self.row(),'success':False,'stage':'cleanup','cleanup_verified':False,
             'cleanup':cleanup}
        reply=SimpleNamespace(returncode=2,stdout=json.dumps(row).encode(),stderr=b'SECRET')
        evidence={}
        with patch('verify_windows_portable.subprocess.run',return_value=reply) as run, \
                patch('verify_windows_portable.subprocess.CREATE_NO_WINDOW',0,create=True),self.assertRaises(AssertionError):
            verify_native_component(Path('candidate.exe'),Path('separate'),{},evidence)
        self.assertEqual(evidence['cleanup'], {k:v for k,v in cleanup.items() if k!='private_path'})
        self.assertNotIn('SECRET',json.dumps(evidence));self.assertEqual(run.call_count,1)

    def test_cleanup_diagnostic_rejects_unexpected_values_and_raw_error_text(self):
        row={**self.row(),'success':False,'cleanup_verified':False,
             'cleanup':dict(attempted='SECRET',close_returned=1,close_error_type='SECRET path')}
        reply=SimpleNamespace(returncode=2,stdout=json.dumps(row).encode(),stderr=b'')
        evidence={}
        with patch('verify_windows_portable.subprocess.run',return_value=reply), \
                patch('verify_windows_portable.subprocess.CREATE_NO_WINDOW',0,create=True),self.assertRaises(AssertionError):
            verify_native_component(Path('candidate.exe'),Path('separate'),{},evidence)
        self.assertIsNone(evidence['cleanup']['attempted'])
        self.assertIsNone(evidence['cleanup']['close_returned'])
        self.assertEqual(evidence['cleanup']['close_error_type'],'unrecognized')
        self.assertNotIn('SECRET',json.dumps(evidence))
