"""Small authored bundles exercise publication rejection, never native certification."""
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPT=Path(__file__).resolve().parents[1]/'scripts/prepare_posix_release.py'


class PosixReleaseTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('posix_release_fixture',SCRIPT)
        self.mod=importlib.util.module_from_spec(spec)
        with patch.object(sys,'path',[str(SCRIPT.parent),*sys.path]):
            spec.loader.exec_module(self.mod)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.sha='a'*40
        self.run=dict(id=100,run_attempt=2,head_sha=self.sha,status='completed',conclusion='success',
                      run_started_at='2026-10-15T11:00:00Z',updated_at='2026-10-15T11:10:00Z')
        self.jobs={'total_count':2,'jobs':[
            dict(id=201,name='linux-x64',run_id=100,head_sha=self.sha,status='completed',conclusion='success',
                 started_at='2026-10-15T10:00:00Z',completed_at='2026-10-15T10:10:00Z'),
            dict(id=202,name='macos-arm64',run_id=100,head_sha=self.sha,status='completed',conclusion='success',
                 started_at='2026-10-15T11:00:00Z',completed_at='2026-10-15T11:10:00Z')]}
        rows=[]
        for index,platform in enumerate(self.mod.PLATFORMS):
            for kind in ('candidate','evidence'):
                rows.append(dict(id=300+len(rows),name=f'posix-{kind}-{platform}',
                    digest='sha256:'+'b'*64,size_in_bytes=123,expired=False,
                    workflow_run=dict(id=100,head_sha=self.sha),
                    created_at=f'2026-10-15T{10+index}:09:00Z'))
        rows.append({**rows[-1],'id':399,'created_at':'2026-10-15T10:09:00Z'})
        self.listing=dict(total_count=len(rows),artifacts=rows)

    def bundle(self, embedded_sha=None):
        from posix_artifacts import pack
        from test_native_component_portable import good_component
        from test_url_pac_evidence import good_url_proof
        from test_domain_pac_evidence import good_domain_proof
        from test_ntlm_evidence import good_ntlm_proof
        from os_credentials_acceptance import FIELDS
        credential_proof=dict(schema_version=1,provider='secret-service',frozen_executable=True,
                              artificial_vault=True,success=True,**{name:True for name in FIELDS})
        bundle=self.root/'VibeJobRadar';bundle.mkdir()
        executable=bundle/'VibeJobRadar';executable.write_bytes(b'authored inert bytes');executable.chmod(0o755)
        # Windows cannot preserve Unix execute bits; the archive metadata is
        # explicitly authored below without executing this artificial payload.
        stamp=dict(revision=embedded_sha or self.sha,raw_sha256='c'*64,
                   executable_sha256=self.mod.file_hash(executable))
        capabilities=dict(python_required=False,browser_bundled=True,signed=False,notarized=False,
                          live_sites_certified=False,clean_machine_certified=False)
        metadata=dict(status='posix_portable_candidate',version='0.11.0',platform='linux-x64',
                      source=dict(sha256='c'*64),runtime_source=stamp,
                      bundled_browser_directory='browsers',automatic_update=False,os_credential_store=True,
                      **capabilities)
        (bundle/'PORTABLE.json').write_text(json.dumps(metadata),encoding='utf-8')
        archive=self.root/'fixture.tar.gz';pack(bundle,archive)
        # Repack the inert fixture to set the executable mode portably.
        import io,tarfile
        entries=[]
        with tarfile.open(archive,'r:gz') as content:
            for member in content:
                data=content.extractfile(member).read() if member.isfile() else None
                if member.name=='VibeJobRadar/VibeJobRadar':member.mode=0o755
                entries.append((member,data))
        with tarfile.open(archive,'w:gz') as content:
            for member,data in entries:content.addfile(member,io.BytesIO(data) if data is not None else None)
        digest=self.mod.file_hash(archive)
        name=f'vibe-job-radar-0.11.0-{digest[:16]}-linux-x64.tar.gz'
        archive=archive.rename(self.root/name)
        proof=dict(success=True,source_revision=self.sha,source_sha256='c'*64,version='0.11.0',
                   platform='linux-x64',checks=list(self.mod.CHECKS),runtime_environment=dict(source=stamp),
                   native_component=good_component('bundled'),url_pac=good_url_proof(),
                   domain_pac=good_domain_proof(),ntlm_component=good_ntlm_proof()['component'],
                   system_credentials=credential_proof,source_system_credentials={**credential_proof,'frozen_executable':False},**capabilities)
        proof_path=self.root/'runtime-verification.json';proof_path.write_text(json.dumps(proof),encoding='utf-8')
        manifest=dict(success=True,status='posix_portable_candidate',version='0.11.0',platform='linux-x64',
                      source_revision=self.sha,source_sha256='c'*64,file=name,sha256=digest,
                      size_bytes=archive.stat().st_size,runtime_evidence_sha256=self.mod.file_hash(proof_path),
                      runtime_checks=9,automatic_update=False,os_credential_store=True,
                      files=self.mod.archive_inventory(archive),**capabilities)
        return archive,manifest,proof_path

    def test_partial_rerun_reuses_successful_job_window_and_keeps_failed_artifact(self):
        selected=self.mod.select_platform_artifacts(self.run,self.listing,self.jobs)
        self.assertEqual({row['id'] for row in selected.values()},{300,301,302,303})
        self.assertEqual(len(self.listing['artifacts']),5)

    def test_wrong_or_incomplete_producer_and_duplicate_artifact_are_rejected(self):
        variants=[]
        for key,value in (('conclusion','failure'),('head_sha','f'*40),('name','unexpected')):
            jobs=copy.deepcopy(self.jobs);jobs['jobs'][0][key]=value;variants.append((self.listing,jobs))
        jobs=copy.deepcopy(self.jobs);jobs['jobs'].pop();variants.append((self.listing,jobs))
        listing=copy.deepcopy(self.listing);listing['artifacts'].append({**listing['artifacts'][0],'id':444})
        listing['total_count']+=1;variants.append((listing,self.jobs))
        for listing,jobs in variants:
            with self.subTest(listing=len(listing['artifacts']),jobs=jobs),self.assertRaises(ValueError):
                self.mod.select_platform_artifacts(self.run,listing,jobs)

    def test_inert_archive_has_matching_source_permissions_and_nested_proofs(self):
        archive,manifest,proof=self.bundle()
        result=self.mod.validate_candidate(archive,manifest,proof,self.sha,'linux-x64')
        self.assertTrue(result['success'])

    def test_archive_bytes_platform_and_claimed_capabilities_cannot_drift(self):
        archive,manifest,proof=self.bundle()
        cases=[('platform','macos-arm64'),('sha256','f'*64),('source_revision','f'*40),
               ('automatic_update',True),('signed',True),('os_credential_store',False),('runtime_checks',8)]
        for key,value in cases:
            with self.subTest(key=key),self.assertRaises(ValueError):
                self.mod.validate_candidate(archive,{**manifest,key:value},proof,self.sha,'linux-x64')
        archive.write_bytes(archive.read_bytes()+b'changed')
        with self.assertRaises(ValueError):
            self.mod.validate_candidate(archive,manifest,proof,self.sha,'linux-x64')

    def test_missing_actual_component_evidence_is_rejected_even_with_new_proof_digest(self):
        archive,manifest,proof=self.bundle()
        original=json.loads(proof.read_text(encoding='utf-8'))
        for field in ('native_component','url_pac','domain_pac','ntlm_component','system_credentials','source_system_credentials','checks'):
            value=copy.deepcopy(original);value.pop(field)
            proof.write_text(json.dumps(value),encoding='utf-8')
            adjusted={**manifest,'runtime_evidence_sha256':self.mod.file_hash(proof)}
            with self.subTest(field=field),self.assertRaises(ValueError):
                self.mod.validate_candidate(archive,adjusted,proof,self.sha,'linux-x64')

    def test_embedded_source_mismatch_is_rejected_after_outer_hashes_match(self):
        archive,manifest,proof=self.bundle(embedded_sha='f'*40)
        with self.assertRaisesRegex(ValueError,'Embedded'):
            self.mod.validate_candidate(archive,manifest,proof,self.sha,'linux-x64')
