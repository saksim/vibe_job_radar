"""Signature gates use authored bytes; these tests never execute a payload."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar import publisher_signing as signing, release_updates as updates


def signed_evidence(sha='d'*64):
    return dict(schema_version=1, status='authenticode_verified', executable_sha256=sha,
                certificate_sha256='e'*64, timestamp_certificate_sha256='f'*64,
                subject='CN=ARTIFICIAL PUBLISHER', issuer='CN=ARTIFICIAL ISSUER')


class PublisherSigningTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.exe=self.root/'VibeJobRadar.exe'
        self.exe.write_bytes(b'ARTIFICIAL DATA; NEVER EXECUTE')
        self.proof=signed_evidence(signing.file_sha256(self.exe))

    def probe(self, **changes):
        return dict(status='Valid', kind='Authenticode',
                    **{k:v for k,v in self.proof.items() if k not in {'schema_version','status','executable_sha256'}},
                    **changes)

    def test_metadata_rejects_unproven_identity_and_accepts_legacy_unsigned(self):
        self.assertFalse(signing.is_signed(None))
        self.assertTrue(signing.valid_evidence(self.proof))
        for defect in ({**self.proof,'timestamp_certificate_sha256':''},
                       {**self.proof,'status':'Valid'}, {**self.proof,'schema_version':True},
                       {**self.proof,'subject':'name\ninjected'},
                       {**self.proof,'extra':'claim'}, {'schema_version':1,'status':'signed'}):
            with self.subTest(defect=defect),self.assertRaises(signing.SignatureError):
                signing.evidence(defect)

    def test_system_result_requires_embedded_valid_timestamped_signature(self):
        with patch.object(signing,'run_powershell',return_value=json.dumps(self.probe())):
            self.assertEqual(signing.inspect(self.exe),self.proof)
        for key,value in (('status','NotTrusted'),('status','HashMismatch'),('kind','Catalog'),
                          ('timestamp_certificate_sha256','')):
            response=self.probe();response[key]=value
            with self.subTest(key=key,value=value),patch.object(signing,'run_powershell',return_value=json.dumps(response)):
                with self.assertRaises(signing.SignatureError):signing.inspect(self.exe)
        with patch.object(signing,'run_powershell',return_value='{"status":"NotSigned"}'):
            self.assertEqual(signing.inspect(self.exe),signing.evidence())

    def test_changed_bytes_or_replaced_identity_refused_before_launch(self):
        with patch.object(signing,'inspect',return_value={**self.proof,'certificate_sha256':'a'*64}):
            with self.assertRaises(signing.SignatureError):signing.verify(self.exe,self.proof)
        self.exe.write_bytes(b'TAMPERED')
        with patch.object(signing,'inspect',side_effect=AssertionError('reject hash first')):
            with self.assertRaises(signing.SignatureError):signing.verify(self.exe,self.proof)

    def test_os_probe_does_not_inherit_core_powershell_modules(self):
        from types import SimpleNamespace
        environment={'SystemRoot':'C:/Windows','PSModulePath':'core module',
                     'PSMODULEPATH':'different core module'}
        with patch.object(signing,'os',SimpleNamespace(name='nt',environ=environment)),                 patch.object(Path,'is_file',return_value=True),                 patch.object(signing.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'{}',b'')) as run:
            signing.run_powershell('fixed script',{'VJR_SIGNATURE_TARGET':'test.exe'})
            env=run.call_args.kwargs['env']
            self.assertEqual([k for k in env if k.lower()=='psmodulepath'],['PSMODULEPATH'])
            self.assertTrue(Path(env['PSMODULEPATH']).as_posix().endswith('/WindowsPowerShell/v1.0/Modules'))
            self.assertNotIn('ExecutionPolicy',run.call_args.args[0])

    def test_file_changed_during_system_verification_is_rejected(self):
        def changed(*args,**kwargs):
            self.exe.write_bytes(b'CHANGED DURING PROBE');return json.dumps(self.probe())
        with patch.object(signing,'run_powershell',side_effect=changed):
            with self.assertRaises(signing.SignatureError):signing.inspect(self.exe)

    def test_unsigned_metadata_cannot_hide_signed_embedded_identity(self):
        (self.root/'PORTABLE.json').write_text(json.dumps({'signing':self.proof}),encoding='utf8')
        with self.assertRaises(signing.SignatureError):signing.verify_bundle(self.root,{'signed':False})
        meta={'signing':self.proof,'runtime_source':{'executable_sha256':self.proof['executable_sha256']}}
        (self.root/'PORTABLE.json').write_text(json.dumps(meta),encoding='utf8')
        with patch.object(signing,'inspect',return_value=self.proof):
            signing.verify_bundle(self.root,{'signed':True,'signing':self.proof})


class SigningAdapterTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('signer_fixture',Path(__file__).resolve().parents[1]/'scripts/windows_signing.py')
        self.mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.mod)

    def test_unsigned_default_cannot_claim_already_signed_payload(self):
        with patch.object(self.mod,'inspect',return_value=signing.evidence()):
            self.assertFalse(signing.is_signed(self.mod.sign_candidate('unused',environ={})))
        with patch.object(self.mod,'inspect',return_value=signed_evidence()):
            with self.assertRaises(signing.SignatureError):self.mod.sign_candidate('unused',environ={})

    def test_missing_config_and_untrusted_ci_never_invoke_signing(self):
        for env in ({'VJR_WINDOWS_SIGNING_MODE':'unknown'},{'VJR_WINDOWS_SIGNING_MODE':'azure'},
                    {'VJR_WINDOWS_SIGNING_MODE':'store'},
                    {'VJR_WINDOWS_SIGNING_MODE':'azure','GITHUB_ACTIONS':'true','GITHUB_EVENT_NAME':'pull_request'},
                    {'VJR_WINDOWS_SIGNING_MODE':'azure','GITHUB_ACTIONS':'true','GITHUB_EVENT_NAME':'push',
                     'GITHUB_REF':'refs/heads/dev','GITHUB_REPOSITORY':'saksim/vibe_job_radar'}):
            with self.subTest(env=env),patch.object(self.mod,'run_powershell',side_effect=AssertionError('must not sign')):
                with self.assertRaises(signing.SignatureError):self.mod.sign_candidate('unused',environ=env)

    def test_azure_signing_is_explicit_and_checks_publisher_after_signing(self):
        proof=signed_evidence()
        env={'VJR_WINDOWS_SIGNING_MODE':'azure','VJR_AZURE_SIGN_ENDPOINT':'https://eus.codesigning.azure.net/',
             'VJR_AZURE_SIGN_ACCOUNT':'test-account','VJR_AZURE_SIGN_PROFILE':'test-profile',
             'VJR_SIGNING_SUBJECT':proof['subject']}
        with patch.object(self.mod,'run_powershell') as run,patch.object(self.mod,'inspect',return_value=proof):
            self.assertEqual(self.mod.sign_candidate('unused',environ=env),proof)
            self.assertTrue(run.call_args.kwargs['pwsh'])
            self.assertIn('ExcludeInteractiveBrowserCredential = $true',run.call_args.args[0])
            with self.assertRaises(signing.SignatureError):
                self.mod.sign_candidate('unused',environ={**env,'VJR_SIGNING_SUBJECT':'Wrong publisher'})
        with patch.object(self.mod,'run_powershell',side_effect=signing.SignatureError('refused')),patch.object(self.mod,'inspect') as inspect:
            with self.assertRaises(signing.SignatureError):self.mod.sign_candidate('unused',environ=env)
            inspect.assert_not_called()

    def test_certificate_store_uses_thumbprint_sha256_and_timestamp_no_password(self):
        with tempfile.TemporaryDirectory() as temp:
            tool=Path(temp)/'signtool.exe';tool.write_bytes(b'INERT')
            proof=signed_evidence()
            env={'VJR_WINDOWS_SIGNING_MODE':'store','VJR_SIGNTOOL':str(tool),
                 'VJR_SIGNING_THUMBPRINT':'a'*40,'VJR_SIGNING_TIMESTAMP_URL':'https://timestamp.example.test',
                 'VJR_SIGNING_SUBJECT':proof['subject']}
            with patch.object(self.mod.subprocess,'run',return_value=subprocess.CompletedProcess([],0)) as run,patch.object(self.mod,'inspect',return_value=proof):
                self.assertEqual(self.mod.sign_candidate('unused',environ=env),proof)
                command=run.call_args.args[0]
                self.assertIn('/sha1',command);self.assertIn('/tr',command);self.assertNotIn('/p',command)
                self.assertEqual(command[command.index('/fd')+1],'SHA256')
                self.assertEqual(command[command.index('/td')+1],'SHA256')


class SignedReleaseContractTests(unittest.TestCase):
    def test_signed_release_evidence_is_bound_and_unsigned_legacy_still_supported(self):
        import test_portable_release as fixture_module
        fixture=fixture_module.PortableReleaseTests()
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        proof=signed_evidence()
        proof['executable_sha256']=updates.digest(b'authored data, not an executable')
        fixture.meta['runtime_source']['executable_sha256']=proof['executable_sha256']
        fixture.proof['files']['VibeJobRadar.exe']=proof['executable_sha256']
        fixture.meta['signing']=proof;fixture.proof['signing']=proof
        package,manifest=fixture.archive();manifest['signing']=proof
        selected={**fixture.run,'head_repository':{'full_name':updates.REPOSITORY}}
        qualified=fixture.mod.validate_runs(fixture.repo,fixture.run,fixture.runs)
        output=fixture.root/'signed-release'
        result=fixture.mod.assemble(package,manifest,fixture.evidence,selected,qualified,
                                    fixture.artifacts,output,fixture_module.NOTES,channel='stable')
        self.assertTrue(result['signed'])
        files=list(output.iterdir());tag=result['tag']
        release=dict(id=42,tag_name=tag,html_url=updates.WEB+'/releases/tag/'+tag,draft=False,prerelease=False,
                     assets=[dict(id=i+1,name=f.name,size=f.stat().st_size,state='uploaded',
                                  digest='sha256:'+fixture.mod.file_hash(f),
                                  browser_download_url=updates.WEB+'/releases/download/'+tag+'/'+f.name)
                             for i,f in enumerate(files)])
        class Transport:
            def small(self,asset,cancelled):return (output/asset['name']).read_bytes()
        actual=updates.inspect_release(release,Transport(),threading.Event())
        self.assertTrue(actual['signed']);self.assertEqual(actual['signing'],proof)
        fixture.proof['signing']={**proof,'certificate_sha256':'a'*64}
        package,manifest=fixture.archive();manifest['signing']=proof
        with self.assertRaises(ValueError):fixture.mod.validate_candidate(package,manifest,fixture.evidence,fixture.sha)

    def test_staging_and_handoff_recheck_signed_bytes_and_preserve_failed_workspace(self):
        import test_release_install as fixture_module
        fixture=fixture_module.InstallTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        import zipfile
        exe=b'ARTIFICIAL DATA NEVER EXECUTE';proof=signed_evidence(updates.digest(exe))
        selected={**fixture.selected,'signed':True,'signing':proof}
        meta=dict(version=selected['version'],platform='windows-x64',upgrade_protocol=1,signing=proof,
                  source={'sha256':selected['source_sha256']},
                  runtime_source={'revision':selected['revision'],'executable_sha256':proof['executable_sha256']})
        package=fixture.updates/'signed-fixture.zip'
        with zipfile.ZipFile(package,'w') as archive:
            archive.writestr('VibeJobRadar/VibeJobRadar.exe',exe)
            archive.writestr('VibeJobRadar/PORTABLE.json',json.dumps(meta))
        selected['sha256']=updates.file_digest(package)
        with patch.object(signing,'inspect',return_value=proof) as probe:
            directory=updates.stage_package(package,selected,fixture.updates,threading.Event())
            fixture_module.install.verify_directory(directory,package,selected)
            self.assertEqual(probe.call_count,2)
        request={**fixture.request,'directory':str(directory),'package':str(package),'selected':selected}
        fixture_module.install.write_json(fixture.path,request)
        self.assertEqual(fixture_module.install.load_request(fixture.path)['selected']['signing'],proof)
        before=set(fixture.updates.iterdir())
        with patch.object(signing,'inspect',side_effect=signing.SignatureError('invalid signature')):
            with self.assertRaises(signing.SignatureError):
                updates.stage_package(package,selected,fixture.updates,threading.Event())
        self.assertEqual(set(fixture.updates.iterdir()),before)
        self.assertEqual((fixture.workspace.root/'private.txt').read_bytes(),b'KEEP PRIVATE ORIGINAL')
        self.assertEqual(fixture.old.read_bytes(),b'OLD TEST BYTES')


class SignedReleaseFixture:
    """Synthetic signature records for UI only, never a valid executable."""
    def __new__(cls):
        import io
        import zipfile
        from test_release_updates import ReleaseFixture
        fixture=ReleaseFixture()
        old=next(name for name in fixture.data if name.endswith('.zip'))
        exe=b'AUTHORED TEST DATA, NEVER EXECUTE'
        proof=signed_evidence(updates.digest(exe))
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w') as archive:
            archive.writestr('VibeJobRadar/VibeJobRadar.exe',exe)
            archive.writestr('VibeJobRadar/PORTABLE.json',json.dumps(dict(version='0.4.0',platform='windows-x64',
                source={'sha256':'b'*64},runtime_source={'revision':'a'*40,'executable_sha256':proof['executable_sha256']},
                signing=proof)))
        raw=output.getvalue();sha=updates.digest(raw)
        name='vibe-job-radar-0.4.0-'+sha[:16]+'-windows-x64.zip'
        del fixture.data[old];fixture.data[name]=raw
        manifest=json.loads(fixture.data['manifest.json'])
        manifest.update(file=name,sha256=sha,size_bytes=len(raw),signing=proof)
        fixture.data['manifest.json']=json.dumps(manifest).encode()
        provenance=json.loads(fixture.data['provenance.json'])
        provenance.update(signed=True,signing=proof,candidate_sha256=sha)
        fixture.data['provenance.json']=json.dumps(provenance).encode()
        fixture.data['portable-verification.json']=json.dumps(dict(success=True,signing=proof,
            files={'VibeJobRadar.exe':proof['executable_sha256']})).encode()
        fixture.data.pop('SHA256SUMS.txt')
        fixture.data['SHA256SUMS.txt']=''.join(updates.digest(data)+'  '+name+chr(10) for name,data in sorted(fixture.data.items())).encode()
        fixture.release['assets']=[dict(id=i+1,name=name,size=len(data),digest='sha256:'+updates.digest(data),
            state='uploaded',browser_download_url=updates.WEB+'/releases/download/v0.4.0/'+name)
            for i,(name,data) in enumerate(sorted(fixture.data.items()))]
        fixture.signing=proof
        return fixture
