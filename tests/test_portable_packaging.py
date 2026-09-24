"""Candidate evidence must describe these bytes, including after a failed rerun."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from unittest.mock import patch


class PortablePackagingTests(unittest.TestCase):
    def setUp(self):
        script=Path(__file__).resolve().parents[1]/'scripts/build_windows_portable.py'
        spec=importlib.util.spec_from_file_location('portable_builder_under_test',script)
        self.builder=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.builder)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.bundle=self.root/'payload';self.bundle.mkdir()
        (self.bundle/'VibeJobRadar.exe').write_bytes(b'artificial unit fixture, not an executable')

    def test_changed_added_and_removed_payload_invalidates_prior_runtime_check(self):
        proof={'success':True,'verified_browser':'bundled','files':self.builder.inventory(self.bundle)}
        self.builder.validate_runtime_evidence(self.bundle,proof)
        binary=self.bundle/'VibeJobRadar.exe';original=binary.read_bytes()
        binary.write_bytes(b'different binary')
        with self.assertRaises(ValueError):self.builder.validate_runtime_evidence(self.bundle,proof)
        binary.write_bytes(original);extra=self.bundle/'added.dll';extra.write_bytes(b'added')
        with self.assertRaises(ValueError):self.builder.validate_runtime_evidence(self.bundle,proof)
        extra.unlink();binary.unlink()
        with self.assertRaises(ValueError):self.builder.validate_runtime_evidence(self.bundle,proof)

    def test_failed_or_empty_check_cannot_qualify_even_with_matching_files(self):
        files=self.builder.inventory(self.bundle)
        for report in (None,{}, {'success':1,'verified_browser':'bundled','files':files},
                       {'success':False,'verified_browser':'bundled','files':files},
                       {'success':True,'verified_browser':'bundled','files':{}},
                       {'success':True,'verified_browser':'bundled','files':[]},
                       {'success':True,'files':files},
                       {'success':True,'verified_browser':'msedge','files':files}):
            with self.subTest(report=report),self.assertRaises(ValueError):
                self.builder.validate_runtime_evidence(self.bundle,report)

    def test_failed_rerun_replaces_green_manifest_and_keeps_prior_candidate(self):
        out=self.root/'out';out.mkdir();candidate=out/'previous-verified.zip';candidate.write_bytes(b'keep')
        (out/'manifest.json').write_text('{"runtime_verified":true}',encoding='utf-8')
        (out/'portable-verification.json').write_text('{"success":true}',encoding='utf-8')
        def failing(*args,**kwargs):
            (out/'portable-verification.json').write_text('{"success":false,"stage":"original_report"}',encoding='utf-8')
            raise RuntimeError('artificial failed executable acceptance')
        with patch.object(self.builder,'ROOT',self.root),patch.object(self.builder,'build_candidate',side_effect=failing):
            with self.assertRaises(RuntimeError):self.builder.build(out,self.root/'source-evidence.json')
        self.assertEqual(candidate.read_bytes(),b'keep')
        self.assertEqual(json.loads((out/'manifest.json').read_text(encoding='utf-8'))['status'],'build_failed')
        self.assertFalse(json.loads((out/'manifest.json').read_text(encoding='utf-8'))['runtime_verified'])
        self.assertEqual(json.loads((out/'portable-verification.json').read_text(encoding='utf-8'))['stage'],'original_report')

    def test_builder_rejects_invalid_source_before_loading_build_tools(self):
        evidence=self.root/'source.json';evidence.write_text('{"success":false}',encoding='utf-8')
        with patch.object(self.builder,'ROOT',self.root),patch.object(self.builder.sys,'platform','win32'),\
             patch.object(self.builder.platform,'machine',return_value='AMD64'),\
             patch.object(self.builder.importlib.metadata,'version') as versions,\
             patch.object(self.builder.subprocess,'run') as run:
            with self.assertRaises(ValueError):self.builder.build(self.root/'out',evidence)
            versions.assert_not_called();run.assert_not_called()

    def test_startup_evidence_is_required_only_for_explicit_ci_acceptance(self):
        proof={'success':True,'verified_browser':'bundled','files':self.builder.inventory(self.bundle)}
        with self.assertRaises(ValueError):
            self.builder.validate_runtime_evidence(self.bundle,proof,require_login_startup=True)
        proof['startup_registration_verified']=True
        with self.assertRaises(ValueError):
            self.builder.validate_runtime_evidence(self.bundle,proof,require_login_startup=True)
        proof['startup_worker_verified']=True
        self.builder.validate_runtime_evidence(self.bundle,proof,require_login_startup=True)
        with patch.dict(self.builder.os.environ,{'GITHUB_ACTIONS':'false'}), \
             patch.object(self.builder,'build_candidate') as build:
            with self.assertRaises(ValueError):
                self.builder.build(self.root/'out',self.root/'source.json',verify_login_startup=True)
            build.assert_not_called()

    def test_archive_must_retain_exact_verified_file_names_and_bytes(self):
        expected=self.builder.inventory(self.bundle);path=self.root/'payload.zip'
        with zipfile.ZipFile(path,'w') as archive:
            archive.write(self.bundle/'VibeJobRadar.exe','VibeJobRadar/VibeJobRadar.exe')
        self.builder.validate_archive(path,expected)
        for contents in ({'VibeJobRadar/VibeJobRadar.exe':b'changed'},
                         {'outside.exe':b'artificial unit fixture, not an executable'},
                         {'VibeJobRadar/VibeJobRadar.exe':b'artificial unit fixture, not an executable','extra':b'private'}):
            with zipfile.ZipFile(path,'w') as archive:
                for name,value in contents.items():archive.writestr(name,value)
            with self.subTest(names=list(contents)),self.assertRaises(ValueError):self.builder.validate_archive(path,expected)

    def test_browser_relocation_preserves_bytes_and_limits_legacy_windows_paths(self):
        original=self.bundle/'_internal/playwright/driver/package/.local-browsers'
        chrome=original/'chromium-1243/chrome-win64/chrome.exe'
        chrome.parent.mkdir(parents=True);chrome.write_bytes(b'artificial browser fixture')
        expected=self.builder.inventory(original)
        self.builder.relocate_browsers(self.bundle)
        self.assertFalse(original.exists())
        self.assertEqual(self.builder.inventory(self.bundle/'browsers'),expected)
        self.builder.check_payload_path_lengths(self.bundle)
        with self.assertRaises(ValueError):self.builder.relocate_browsers(self.bundle)
        # Each directory component is legal, but their combined relative path
        # would exceed the promised extraction budget.
        deep=self.bundle/('a'*60)/('b'*60)/('c'*22)
        deep.mkdir(parents=True)
        with self.assertRaises(ValueError):self.builder.check_payload_path_lengths(self.bundle)


if __name__=='__main__':unittest.main()
