"""Small authored archives, never executables or live-release certification."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/prepare_portable_release.py'


class PortableReleaseTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('release_fixture',SCRIPT)
        self.mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.mod)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo='example/research';self.sha='a'*40
        self.runs=[{'id':i,'run_attempt':1,'path':path,'head_sha':self.sha,
                    'head_repository':{'full_name':self.repo},'head_branch':'main',
                    'event':'push','status':'completed','conclusion':'success'}
                   for i,path in enumerate(sorted(self.mod.WORKFLOWS),100)]
        self.run=next(x for x in self.runs if x['path'].endswith('windows-portable.yml'))
        self.source={'algorithm':'sha256-file-manifest-v1','sha256':'b'*64,
                     'files':{'README.md':'c'*64},'file_count':1}
        self.evidence={'success':True,'source_unchanged':True,'source':self.source}
        self.meta={'schema_version':1,'version':'0.2.0','platform':'windows-x64',
                   'status':'portable_candidate_not_release','source':self.source,
                   'browser_bundled':True,'live_sites_certified':False,
                   'runtime_source':{'revision':self.sha,'raw_sha256':'b'*64,'executable_sha256':'d'*64}}
        self.proof={'success':True,'verified_browser':'bundled',
                    'runtime_environment_verified':True,'runtime_environment_restart_verified':True,
                    'files':{'VibeJobRadar.exe':'d'*64}}
        self.required_flags=('startup_registration_verified','startup_worker_verified',
                             'system_pac_verified','wpad_dns_verified','url_pac_worker_verified')
        self.proof.update({flag:True for flag in self.required_flags})
        self.artifacts={name:{'id':i,'digest':None} for i,name in enumerate(self.mod.ARTIFACTS,200)}
        self.package,self.manifest=self.archive()

    def archive(self):
        path=self.root/'fixture.zip'
        with zipfile.ZipFile(path,'w') as z:
            z.writestr('VibeJobRadar/PORTABLE.json',json.dumps(self.meta))
            z.writestr('VibeJobRadar/portable-verification.json',json.dumps(self.proof))
            z.writestr('VibeJobRadar/VibeJobRadar.exe',b'authored data, not an executable')
        sha=self.mod.file_hash(path);dest=self.root/f'vibe-job-radar-0.2.0-{sha[:16]}-windows-x64.zip'
        path.replace(dest)
        manifest={'file':dest.name,'sha256':sha,'source_sha256':'b'*64,'size_bytes':dest.stat().st_size,
                  'status':'portable_candidate_not_release','runtime_verified':True,
                  'live_sites_certified':False,'platform':'windows-x64'}
        manifest.update({flag:True for flag in self.required_flags})
        return dest,manifest

    def test_reuse_candidate_keeps_bytes_unsigned_scope_and_rollback(self):
        qualified=self.mod.validate_runs(self.repo,self.run,self.runs)
        out=self.root/'prepared'
        with patch.object(self.mod,'gh',side_effect=AssertionError('offline assembly')):
            result=self.mod.assemble(self.package,self.manifest,self.evidence,
                                     self.run,qualified,self.artifacts,out)
        self.assertEqual((out/self.package.name).read_bytes(),self.package.read_bytes())
        for key in ('rebuilt_for_release','signed','live_sites_certified','clean_machine_certified'):
            self.assertFalse(result[key])
        self.assertEqual(result['source_revision'],self.sha)
        self.assertIn('备份整个工作区',(out/'UPGRADE_AND_LIMITATIONS.md').read_text(encoding='utf8'))
        sums=(out/'SHA256SUMS.txt').read_text(encoding='utf8')
        for path in out.iterdir():
            if path.name!='SHA256SUMS.txt':self.assertIn(self.mod.file_hash(path)+'  '+path.name,sums)

    def test_failed_pending_pr_fork_or_missing_ci_cannot_prepare_release(self):
        for defect in ('failed','pending','pull_request','fork','missing'):
            with self.subTest(defect=defect):
                runs=copy.deepcopy(self.runs)
                if defect=='failed':runs[0]['conclusion']='failure'
                elif defect=='pending':runs[0]['status']='in_progress'
                elif defect=='pull_request':runs[0]['event']='pull_request'
                elif defect=='fork':runs[0]['head_repository']['full_name']='other/fork'
                else:runs.pop(0)
                with self.assertRaises(ValueError):self.mod.validate_runs(self.repo,self.run,runs)

    def test_manifest_corruption_is_rejected_before_output(self):
        for key,value in (('sha256','f'*64),('size_bytes',0),('file','../bad.zip'),
                          ('runtime_verified',False),('source_sha256','f'*64)):
            with self.subTest(key=key):
                output=self.root/'refused'
                with self.assertRaises(ValueError):
                    self.mod.assemble(self.package,{**self.manifest,key:value},self.evidence,
                                      self.run,{},self.artifacts,output)
                self.assertFalse(output.exists())

    def test_embedded_source_or_runtime_failure_cannot_use_green_outer_manifest(self):
        for defect in ('revision','source','browser','restart'):
            with self.subTest(defect=defect):
                old_meta,old_proof=copy.deepcopy(self.meta),copy.deepcopy(self.proof)
                if defect=='revision':self.meta['runtime_source']['revision']='e'*40
                elif defect=='source':self.meta['source']={**self.source,'sha256':'f'*64}
                elif defect=='browser':self.proof['verified_browser']='msedge'
                else:self.proof['runtime_environment_restart_verified']=False
                package,manifest=self.archive()
                with self.assertRaises(ValueError):
                    self.mod.validate_candidate(package,manifest,self.evidence,self.sha)
                self.meta,self.proof=old_meta,old_proof

    def test_historical_manifest_missing_or_failed_current_acceptance_is_rejected(self):
        for flag in self.required_flags:
            for missing in (True,False):
                with self.subTest(flag=flag,missing=missing):
                    manifest=copy.deepcopy(self.manifest)
                    if missing:manifest.pop(flag)
                    else:manifest[flag]=False
                    with self.assertRaises(ValueError):
                        self.mod.validate_candidate(self.package,manifest,self.evidence,self.sha)

    def test_green_manifest_does_not_replace_structured_windows_acceptance(self):
        for flag in self.required_flags:
            for missing in (True,False):
                with self.subTest(flag=flag,missing=missing):
                    original=copy.deepcopy(self.proof)
                    if missing:self.proof.pop(flag)
                    else:self.proof[flag]=False
                    package,manifest=self.archive()
                    with self.assertRaises(ValueError):
                        self.mod.validate_candidate(package,manifest,self.evidence,self.sha)
                    self.proof=original

    def test_existing_output_is_not_replaced(self):
        output=self.root/'existing';output.mkdir();(output/'keep.txt').write_text('keep',encoding='utf8')
        with self.assertRaises(ValueError):
            self.mod.assemble(self.package,self.manifest,self.evidence,self.run,{},self.artifacts,output)
        self.assertEqual((output/'keep.txt').read_text(encoding='utf8'),'keep')
        self.assertEqual(len(list(output.iterdir())),1)

    def api_fixture(self,suffix):
        if suffix==f"actions/runs/{self.run['id']}":return copy.deepcopy(self.run)
        if suffix.startswith('actions/runs?'):
            return {'total_count':len(self.runs),'workflow_runs':copy.deepcopy(self.runs)}
        if suffix.endswith('/artifacts?per_page=100'):
            return {'total_count':2,'artifacts':[{'name':name,'expired':False,**row}
                                               for name,row in self.artifacts.items()]}
        if suffix.startswith('git/matching-refs/tags/'):return []
        if suffix=='releases?per_page=100&page=1':return []
        raise AssertionError(suffix)

    def gh_fixture(self,*args):
        if args[:2]==('run','download'):
            destination=Path(args[args.index('--dir')+1]);destination.mkdir(parents=True)
            name=args[args.index('--name')+1]
            if name==self.mod.ARTIFACTS[0]:shutil.copyfile(self.package,destination/self.package.name)
            else:
                for file,data in (('portable-candidate/manifest.json',self.manifest),
                                  ('release-verification/result.json',self.evidence)):
                    path=destination/file;path.parent.mkdir(parents=True,exist_ok=True)
                    path.write_text(json.dumps(data),encoding='utf8')
            return ''
        if args[:3]==('api','--method','POST'):
            body=json.loads(Path(args[-1]).read_text(encoding='utf8'))
            self.assertTrue(body['draft']);self.assertTrue(body['prerelease'])
            self.assertEqual(body['target_commitish'],self.sha)
            return json.dumps({'id':300,'html_url':'https://example.invalid/authored-draft'})
        if args[:2]==('release','upload'):
            self.assertNotIn('--clobber',args);return ''
        raise AssertionError(args)

    def test_selected_artifacts_only_and_draft_creation_never_rebuilds(self):
        with patch.object(self.mod,'api',side_effect=lambda repo,suffix:self.api_fixture(suffix)), \
             patch.object(self.mod,'gh',side_effect=self.gh_fixture) as command:
            result=self.mod.prepare(self.repo,self.run['id'],self.root/'download',True)
        calls=[call.args for call in command.call_args_list]
        self.assertEqual(len(calls),4)
        self.assertIn('draft_url',result)
        self.assertEqual([c[c.index('--name')+1] for c in calls if c[:2]==('run','download')],
                         list(self.mod.ARTIFACTS))

    def test_changed_artifact_during_download_stops_before_draft(self):
        queries=[0]
        def response(repo,suffix):
            result=self.api_fixture(suffix)
            if suffix.endswith('/artifacts?per_page=100'):
                queries[0]+=1
                if queries[0]>1:result['artifacts'][0]['id']+=100
            return result
        with patch.object(self.mod,'api',side_effect=response), \
             patch.object(self.mod,'gh',side_effect=self.gh_fixture) as command, \
             self.assertRaises(ValueError):
            self.mod.prepare(self.repo,self.run['id'],self.root/'changed',True)
        self.assertTrue(all(call.args[:2]==('run','download') for call in command.call_args_list))

    def test_existing_draft_or_tag_blocks_creation_and_keeps_preparation(self):
        for existing in ('tag','draft'):
            with self.subTest(existing=existing):
                def response(repo,suffix):
                    if existing=='tag' and suffix.startswith('git/matching-refs/tags/'):return [{'ref':'existing'}]
                    if existing=='draft' and suffix=='releases?per_page=100&page=1':
                        prepared=json.loads((self.root/existing/'prepared.json').read_text(encoding='utf8'))
                        return [{'tag_name':prepared['tag']}]
                    return self.api_fixture(suffix)
                with patch.object(self.mod,'api',side_effect=response), \
                     patch.object(self.mod,'gh',side_effect=self.gh_fixture) as command, \
                     self.assertRaises(ValueError):
                    self.mod.prepare(self.repo,self.run['id'],self.root/existing,True)
                self.assertTrue((self.root/existing/'prepared.json').is_file())
                self.assertTrue(all(call.args[:2]==('run','download') for call in command.call_args_list))


    def test_run_selection_rejects_noncanonical_concurrency_aliases(self):
        self.assertEqual(self.mod.canonical_run_id('123'),123)
        for value in ('00123','+123',' 123','123 ','1_23','0','-1','１２３','1.23'):
            with self.subTest(value=value),self.assertRaises(argparse.ArgumentTypeError):
                self.mod.canonical_run_id(value)

    def test_other_ci_rerun_during_download_stops_before_draft(self):
        for defect in ('pending','failed','new_successful_attempt'):
            with self.subTest(defect=defect):
                queries=[0]
                def response(repo,suffix):
                    result=self.api_fixture(suffix)
                    if suffix.startswith('actions/runs?'):
                        queries[0]+=1
                        if queries[0]>1:
                            run=next(r for r in result['workflow_runs'] if r['id']!=self.run['id'])
                            run['run_attempt']+=1
                            if defect=='pending':run.update(status='in_progress',conclusion=None)
                            elif defect=='failed':run['conclusion']='failure'
                    return result
                with patch.object(self.mod,'api',side_effect=response), \
                     patch.object(self.mod,'gh',side_effect=self.gh_fixture) as command, \
                     self.assertRaises(ValueError):
                    self.mod.prepare(self.repo,self.run['id'],self.root/defect,True)
                self.assertTrue(all(c.args[:2]==('run','download') for c in command.call_args_list))

    def test_release_inventory_after_first_hundred_can_create_or_detect_collision(self):
        for collision in (False,True):
            with self.subTest(collision=collision):
                output=self.root/str(collision)
                pages=[]
                def response(repo,suffix):
                    if suffix.startswith('releases?per_page=100&page='):
                        page=int(suffix.rsplit('=',1)[1]);pages.append(page)
                        if page==1:return [{'tag_name':f'old-{i}'} for i in range(100)]
                        if page==2:
                            prepared=json.loads((output/'prepared.json').read_text(encoding='utf8'))
                            return [{'tag_name':prepared['tag'] if collision else 'older-release'}]
                        raise AssertionError('Unexpected extra release page')
                    return self.api_fixture(suffix)
                with patch.object(self.mod,'api',side_effect=response), \
                     patch.object(self.mod,'gh',side_effect=self.gh_fixture) as command:
                    if collision:
                        with self.assertRaises(ValueError):
                            self.mod.prepare(self.repo,self.run['id'],output,True)
                        self.assertTrue(all(c.args[:2]==('run','download') for c in command.call_args_list))
                    else:
                        result=self.mod.prepare(self.repo,self.run['id'],output,True)
                        self.assertIn('draft_url',result)
                self.assertEqual(pages,[1,2])


if __name__=='__main__':
    unittest.main()
