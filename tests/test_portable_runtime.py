"""Immutable portable-component behavior; real artifact acceptance is separate."""
import os
from pathlib import Path
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
            prepare_portable()
            self.assertEqual(os.environ['PLAYWRIGHT_BROWSERS_PATH'],str(Path(sys.executable).resolve().parent/'browsers'))
        with self.frozen(),patch.dict(os.environ,{'PLAYWRIGHT_BROWSERS_PATH':'explicit-test-cache'},clear=True):
            prepare_portable();self.assertEqual(os.environ['PLAYWRIGHT_BROWSERS_PATH'],'explicit-test-cache')

    def test_source_cannot_run_packaged_entry_or_claim_bundled_browser(self):
        with patch.object(sys,'frozen',False,create=True),patch.dict(os.environ,{},clear=True):
            with self.assertRaises(RuntimeError):prepare_portable()
            self.assertNotIn('PLAYWRIGHT_BROWSERS_PATH',os.environ)
            self.assertEqual(description()['kind'],'source')


class PortableDesktopTests(unittest.TestCase):
    def test_source_and_windows_keep_existing_browser_open(self):
        from vibe_job_radar import runtime
        for frozen,system in ((False,'linux'),(True,'win32')):
            with self.subTest(frozen=frozen,system=system),patch.object(runtime,'is_portable',return_value=frozen), \
                 patch.object(sys,'platform',system),patch.object(runtime.webbrowser,'open',return_value=True) as opened:
                self.assertTrue(runtime.open_workbench_browser('http://127.0.0.1:1234/#token=artificial'))
                opened.assert_called_once()

    def test_linux_desktop_child_restores_system_libraries_without_mutating_parent(self):
        from vibe_job_radar import runtime
        for original in (None,'/authored/system-libraries'):
            environment={'LD_LIBRARY_PATH':'/authored/bundle','KEEP':'unchanged'}
            if original is not None:environment['LD_LIBRARY_PATH_ORIG']=original
            with self.subTest(original=original),patch.object(runtime,'is_portable',return_value=True), \
                 patch.object(sys,'platform','linux'),patch.dict(os.environ,environment,clear=True), \
                 patch.object(Path,'is_file',return_value=True),patch.object(runtime.subprocess,'Popen') as spawned:
                spawned.return_value.poll.return_value=None
                self.assertTrue(runtime.open_workbench_browser('http://127.0.0.1:1234/#token=artificial'))
                self.assertEqual(spawned.call_args.args[0][0],'/usr/bin/xdg-open')
                child=spawned.call_args.kwargs['env']
                self.assertEqual(child.get('LD_LIBRARY_PATH'),original)
                self.assertNotIn('LD_LIBRARY_PATH_ORIG',child)
                self.assertEqual(child['KEEP'],'unchanged')
                self.assertEqual(dict(os.environ),environment)

    def test_missing_or_failed_desktop_opener_keeps_manual_url_fallback(self):
        from vibe_job_radar import runtime
        with patch.object(runtime,'is_portable',return_value=True),patch.object(sys,'platform','darwin'):
            with patch.object(Path,'is_file',return_value=False),patch.object(runtime.subprocess,'Popen') as spawned:
                self.assertFalse(runtime.open_workbench_browser('http://127.0.0.1:1234/#token=artificial'))
                spawned.assert_not_called()
            with patch.object(Path,'is_file',return_value=True),patch.object(runtime.subprocess,'Popen',side_effect=OSError):
                self.assertFalse(runtime.open_workbench_browser('http://127.0.0.1:1234/#token=artificial'))

    def test_guidance_distinguishes_platform_without_unlocking_component_install(self):
        from vibe_job_radar import runtime
        with patch.object(runtime,'is_portable',return_value=True):
            for system in ('linux','darwin','win32'):
                with self.subTest(system=system),patch.object(sys,'platform',system):
                    info=runtime.description()
                    self.assertFalse(info['components_mutable'])
                    self.assertEqual(info['guidance'],runtime.PORTABLE_GUIDANCE if system=='win32' else runtime.POSIX_GUIDANCE)
                    with self.assertRaises(InputError):runtime.require_source_install()
