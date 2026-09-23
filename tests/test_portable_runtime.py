"""Immutable portable-component behavior; real artifact acceptance is separate."""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from vibe_job_radar.runtime import description,is_portable,prepare_portable
from vibe_job_radar.guided.browser_health import command_help,environment_report
from vibe_job_radar.guided.browser_install import install_commands
from vibe_job_radar.guided.service import GuidedService
from vibe_job_radar.workspace import InputError,Workspace


class PortableRuntimeTests(unittest.TestCase):
    def frozen(self):return patch.multiple(sys,frozen=True,_MEIPASS='artificial_bundle_for_unit_test',create=True)

    def test_source_keeps_fixed_installer_commands(self):
        with patch.object(sys,'frozen',False,create=True):
            self.assertFalse(is_portable());self.assertTrue(description()['components_mutable'])
            self.assertEqual(install_commands()[0][1][:4],[sys.executable,'-m','pip','install'])
            self.assertIn('-m playwright install chromium',command_help()['browser'])

    def test_portable_refuses_every_package_operation_before_subprocess(self):
        with self.frozen(),patch('subprocess.Popen') as subprocess:
            for mode in ('ensure','reinstall','upgrade','tls'):
                with self.subTest(mode=mode),self.assertRaises(InputError):install_commands(mode)
            subprocess.assert_not_called()
            info=environment_report();self.assertEqual(info['runtime']['kind'],'portable')
            self.assertFalse(info['runtime']['components_mutable'])
            self.assertEqual(set(info['commands']),{'portable_repair'})
            self.assertNotIn('-m pip',str(info['commands']))

    def test_portable_service_refuses_install_without_queuing_or_changing_workspace(self):
        with tempfile.TemporaryDirectory() as tmp,self.frozen():
            workspace=Workspace(tmp);service=GuidedService(workspace)
            try:
                with patch.object(service,'_submit_setup') as queued:
                    for mode in ('ensure','reinstall','upgrade','tls'):
                        with self.subTest(mode=mode),self.assertRaises(InputError):service.install({'consent':True,'mode':mode})
                    queued.assert_not_called()
                self.assertEqual(service.state()['runtime']['kind'],'portable')
                self.assertFalse(workspace.db.exists())
            finally:service.close()

    def test_portable_sets_bundled_layout_but_preserves_explicit_cache(self):
        with self.frozen(),patch.dict(os.environ,{},clear=True):
            prepare_portable();self.assertEqual(os.environ['PLAYWRIGHT_BROWSERS_PATH'],'0')
        with self.frozen(),patch.dict(os.environ,{'PLAYWRIGHT_BROWSERS_PATH':'explicit-test-cache'},clear=True):
            prepare_portable();self.assertEqual(os.environ['PLAYWRIGHT_BROWSERS_PATH'],'explicit-test-cache')

    def test_source_cannot_run_packaged_entry_or_claim_bundled_browser(self):
        with patch.object(sys,'frozen',False,create=True),patch.dict(os.environ,{},clear=True):
            with self.assertRaises(RuntimeError):prepare_portable()
            self.assertNotIn('PLAYWRIGHT_BROWSERS_PATH',os.environ)
            self.assertEqual(description()['kind'],'source')
