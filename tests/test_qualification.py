"""PR29 regression cases ported to the current main and explicit UTF-8 I/O."""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from vibe_job_radar.qualification import fingerprint, source_files, check_source, require_local_evidence


class QualificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        (self.root/'src/vibe_job_radar').mkdir(parents=True)
        (self.root/'src/vibe_job_radar/_version.py').write_text('__version__ = "0.2.1"\n', encoding='utf-8')
        (self.root/'pyproject.toml').write_text('version = "0.2.1"\n', encoding='utf-8')
        (self.root/'scripts').mkdir()
        (self.root/'scripts/start.py').write_text('print("fixture")\n', encoding='utf-8')
    def evidence(self):
        return {'schema_version':1,'kind':'local-candidate-verification','success':True,
                'source':fingerprint(self.root),'source_unchanged':True,
                'tests':{'tests_run':3,'success':True,'failures':0,'errors':0,'skipped':0},
                'steps':[{'name':n,'returncode':0} for n in ('unit-tests','user-guide','offline-demo','source-doctor')]}
    def test_same_source_accepts(self):
        self.assertEqual(require_local_evidence(self.root,self.evidence()),fingerprint(self.root))
    def test_changed_python_source_rejects_old_report(self):
        evidence=self.evidence()
        (self.root/'scripts/start.py').write_text('print("changed")\n', encoding='utf-8')
        with self.assertRaises(ValueError):require_local_evidence(self.root,evidence)
    def test_added_or_removed_source_rejects_old_report(self):
        evidence=self.evidence()
        (self.root/'scripts/extra.py').write_text('pass\n', encoding='utf-8')
        with self.assertRaises(ValueError):require_local_evidence(self.root,evidence)
        (self.root/'scripts/extra.py').unlink()
        (self.root/'scripts/start.py').unlink()
        with self.assertRaises(ValueError):require_local_evidence(self.root,evidence)
    def test_changed_docs_also_invalidate_candidate(self):
        evidence=self.evidence();(self.root/'README.md').write_text('Changed docs', encoding='utf-8')
        with self.assertRaises(ValueError):require_local_evidence(self.root,evidence)
    def test_runtime_data_and_environment_secrets_are_excluded(self):
        expected=fingerprint(self.root)
        (self.root/'.env').write_text('SECRET=do-not-package', encoding='utf-8')
        (self.root/'jobs.sqlite').write_bytes(b'private')
        (self.root/'runs').mkdir();(self.root/'runs/user.json').write_text('{}', encoding='utf-8')
        self.assertEqual(expected,fingerprint(self.root))
    def test_bytecode_and_logs_do_not_break_stability(self):
        expected=fingerprint(self.root)
        (self.root/'src/__pycache__').mkdir();(self.root/'src/__pycache__/a.pyc').write_bytes(b'x')
        (self.root/'scripts/error.log').write_text('private runtime', encoding='utf-8')
        self.assertEqual(expected,fingerprint(self.root))
    def test_zero_or_boolean_test_count_cannot_qualify(self):
        for value in (0,True,-1):
            evidence=self.evidence();evidence['tests']['tests_run']=value
            with self.subTest(value=value),self.assertRaises(ValueError):require_local_evidence(self.root,evidence)
    def test_missing_or_failed_checks_reject(self):
        for name in ('unit-tests','user-guide','offline-demo','source-doctor'):
            evidence=self.evidence()
            evidence['steps']=[s for s in evidence['steps'] if s['name']!=name]
            with self.subTest(name=name),self.assertRaises(ValueError):require_local_evidence(self.root,evidence)
    def test_summary_true_does_not_override_test_failure(self):
        evidence=self.evidence();evidence['tests']['failures']=1
        with self.assertRaises(ValueError):require_local_evidence(self.root,evidence)
    def test_duplicate_steps_do_not_substitute_for_missing_check(self):
        evidence=self.evidence();evidence['steps'][0]['name']='user-guide'
        with self.assertRaises(ValueError):require_local_evidence(self.root,evidence)
    def test_invalid_version_and_python_are_rejected(self):
        (self.root/'pyproject.toml').write_text('version = "0.0.1"\n', encoding='utf-8')
        with self.assertRaises(ValueError):check_source(self.root)
        (self.root/'pyproject.toml').write_text('version = "0.2.1"\n', encoding='utf-8')
        (self.root/'scripts/start.py').write_text('def broken(', encoding='utf-8')
        with self.assertRaises(SyntaxError):check_source(self.root)
    def test_all_source_python_is_parsed(self):
        self.assertEqual(check_source(self.root),{'version':'0.2.1','python_files_parsed':2})
    def test_schema_and_source_stability_are_required(self):
        for override in ({'source_unchanged':False},{'kind':'browser-test'},{'schema_version':2},{'success':False}):
            evidence={**self.evidence(),**override}
            with self.subTest(override=override),self.assertRaises(ValueError):require_local_evidence(self.root,evidence)
    def test_source_file_symlink_is_not_packaged(self):
        target=self.root/'outside';target.write_text('secret', encoding='utf-8')
        try:(self.root/'scripts/link.py').symlink_to(target)
        except OSError:self.skipTest('symlink privileges unavailable')
        with self.assertRaises(ValueError):source_files(self.root)
    def test_malformed_evidence_is_actionable_value_error(self):
        for evidence in (None, [], {'schema_version':1,'kind':'local-candidate-verification','success':True,'tests':None}):
            with self.subTest(evidence=evidence),self.assertRaises(ValueError):
                require_local_evidence(self.root,evidence)
    def test_boolean_success_codes_do_not_count_as_zero(self):
        for field in ('failures','errors'):
            evidence=self.evidence();evidence['tests'][field]=False
            with self.subTest(field=field),self.assertRaises(ValueError):require_local_evidence(self.root,evidence)
        evidence=self.evidence();evidence['steps'][0]['returncode']=False
        with self.assertRaises(ValueError):require_local_evidence(self.root,evidence)
    def test_malformed_step_does_not_crash_with_attribute_error(self):
        evidence=self.evidence();evidence['steps'][0]='unexpected text'
        with self.assertRaises(ValueError):require_local_evidence(self.root,evidence)
    def test_builder_refuses_output_inside_source(self):
        module=self.load_script('build_candidate')
        report=self.root/'evidence.json';report.write_text(json.dumps(self.evidence()), encoding='utf-8')
        with self.assertRaises(ValueError):module.build(self.root/'src/output',report)
    def test_verify_does_not_reuse_an_old_unit_report(self):
        from unittest.mock import patch
        import subprocess
        module=self.load_script('verify_candidate')
        out=self.root/'check';out.mkdir()
        (out/'unit-tests.json').write_text(json.dumps({'success':True,'tests_run':399}), encoding='utf-8')
        with patch.object(module.platform,'platform',return_value='fixture'), patch.object(module.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'')):
            result=module.verify(out)
        self.assertFalse(result['success'])
        self.assertEqual(result['tests'],{})
    def test_package_contains_evidence_and_exact_source_not_user_state(self):
        import zipfile
        module=self.load_script('build_candidate')
        report=self.root/'evidence.json';report.write_text(json.dumps(self.evidence()), encoding='utf-8')
        (self.root/'.env').write_text('private', encoding='utf-8')
        archive=module.build(self.root/'out',report)
        with zipfile.ZipFile(archive) as bundle:
            names=bundle.namelist()
            self.assertNotIn('vibe-job-radar-0.2.1/.env',names)
            marker=json.loads(bundle.read('vibe-job-radar-0.2.1/CANDIDATE.json'))
            self.assertFalse(marker['live_sites_certified'])
            self.assertEqual(marker['remote_ci'],'separate_verification_required')
            self.assertEqual(marker['source'],fingerprint(self.root))
        (self.root/'scripts/start.py').write_text('changed=1\n', encoding='utf-8')
        with self.assertRaises(ValueError):module.build(self.root/'out',report)

    def load_script(self, name):
        path=Path(__file__).resolve().parents[1]/'scripts'/f'{name}.py'
        spec=importlib.util.spec_from_file_location(name+'_test',path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);module.ROOT=self.root
        return module

    def test_missing_skipped_or_nonzero_skipped_rejected(self):
        for value in (None, False, 1, -1):
            data=self.evidence();data['tests']['skipped']=value
            with self.subTest(value=value),self.assertRaises(ValueError):require_local_evidence(self.root,data)
    def test_boolean_schema_cannot_be_integer_one(self):
        with self.assertRaises(ValueError):require_local_evidence(self.root,{**self.evidence(),'schema_version':True})
    def test_obvious_private_files_inside_source_are_rejected(self):
        for name in ('.env','.env.production','cache.sqlite','private.key','private.pem','private.p12'):
            path=self.root/'src'/name;path.write_bytes(b'NEVER-PACKAGE')
            with self.subTest(name=name),self.assertRaises(ValueError):source_files(self.root)
            path.unlink()
    def test_source_directory_symlink_rejected(self):
        target=self.root/'outside';target.mkdir()
        try:(self.root/'docs').symlink_to(target,target_is_directory=True)
        except OSError:self.skipTest('symlink privileges unavailable')
        with self.assertRaises(ValueError):source_files(self.root)
    def test_unsafe_version_cannot_escape_archive_prefix(self):
        for version in ('../../bad','0.2.0/../bad','0.2.0\\bad'):
            (self.root/'src/vibe_job_radar/_version.py').write_text('__version__ = '+json.dumps(version),encoding='utf-8')
            (self.root/'pyproject.toml').write_text('version = '+json.dumps(version),encoding='utf-8')
            with self.subTest(version=version),self.assertRaises(ValueError):check_source(self.root)
    def test_exact_payload_bytes_and_macos_launch_mode(self):
        import hashlib,zipfile
        (self.root/'start_macos.command').write_text('#!/bin/sh\necho 测试\n',encoding='utf-8')
        data=self.evidence();p=self.root/'evidence.json';p.write_text(json.dumps(data),encoding='utf-8')
        archive=self.load_script('build_candidate').build(self.root/'out',p)
        with zipfile.ZipFile(archive) as z:
            for name,digest in data['source']['files'].items():
                self.assertEqual(hashlib.sha256(z.read('vibe-job-radar-0.2.1/'+name)).hexdigest(),digest)
            self.assertEqual(z.getinfo('vibe-job-radar-0.2.1/start_macos.command').external_attr >> 16,0o100755)
        manifest=json.loads((self.root/'out/manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['sha256'],hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertIn(manifest['sha256'][:16],archive.name)
    def test_change_while_packaging_keeps_old_archive_and_no_partial(self):
        from unittest.mock import patch
        module=self.load_script('build_candidate');p=self.root/'evidence.json'
        p.write_text(json.dumps(self.evidence()),encoding='utf-8')
        archive=module.build(self.root/'out',p);before=archive.read_bytes()
        with patch.object(module,'fingerprint',return_value={'changed':True}):
            with self.assertRaises(ValueError):module.build(self.root/'out',p)
        self.assertEqual(archive.read_bytes(),before)
        self.assertEqual(list((self.root/'out').glob('*.part')),[])
    def test_unreported_zero_exit_or_failed_check_cannot_qualify(self):
        from unittest.mock import patch
        import subprocess
        module=self.load_script('verify_candidate')
        for code in (0,1):
            with patch.object(module.platform,'platform',return_value='fixture'), patch.object(module.subprocess,'run',return_value=subprocess.CompletedProcess([],code,b'fixture')):
                self.assertFalse(module.verify(self.root/'out')['success'])
    def test_timeout_overwrites_previous_success_summary(self):
        from unittest.mock import patch
        import subprocess
        module=self.load_script('verify_candidate');out=self.root/'out';out.mkdir()
        (out/'result.json').write_text(json.dumps(self.evidence()),encoding='utf-8')
        with patch.object(module.platform,'platform',return_value='fixture'), patch.object(module.subprocess,'run',side_effect=subprocess.TimeoutExpired('fixture',180)):
            self.assertFalse(module.verify(out)['success'])
        report=json.loads((out/'result.json').read_text(encoding='utf-8'))
        self.assertEqual(report['steps'][0]['error'],'local_check_timeout')

    def test_timeout_keeps_current_progress_without_accepting_incomplete_tests(self):
        from unittest.mock import patch
        import subprocess
        module=self.load_script('verify_candidate');out=self.root/'out'
        def stalled(args,**kwargs):
            target=Path(args[args.index('--report')+1])
            target.with_suffix('.progress.log').write_text('START artificial_stalled_test\n',encoding='utf-8')
            raise subprocess.TimeoutExpired(args,600)
        with patch.object(module.platform,'platform',return_value='fixture'),patch.object(module.subprocess,'run',side_effect=stalled):
            report=module.verify(out)
        self.assertFalse(report['success']);self.assertEqual(report['tests'],{})
        self.assertIn('artificial_stalled_test',(out/'unit-tests.progress.log').read_text(encoding='utf-8'))
    def test_root_and_git_directory_cannot_be_output(self):
        from vibe_job_radar.qualification import output_directory
        for path in (self.root,self.root/'.git'/'unsafe',self.root/'docs'/'output'):
            with self.assertRaises(ValueError):output_directory(self.root,path)


if __name__=='__main__':unittest.main()
