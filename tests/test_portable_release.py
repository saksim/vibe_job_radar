"""Small authored archives, never executables or live-release certification."""
import argparse
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
import zipfile
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1]/'scripts/prepare_portable_release.py'
NOTES = '## 解决了什么\n- #318 发行缺少变更说明。\n## 提高了什么\n- 可追溯实际修复。\n## 验证结果\n- 离线夹具通过。\n## 剩余问题\n- #319 现场验收待完成。\n'


class PortableReleaseTests(unittest.TestCase):
    def setUp(self):
        spec=importlib.util.spec_from_file_location('release_fixture',SCRIPT)
        self.mod=importlib.util.module_from_spec(spec)
        with patch.object(sys,'path',[str(SCRIPT.parent),*sys.path]):
            spec.loader.exec_module(self.mod)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo='example/research';self.sha='a'*40
        self.runs=[{'id':i,'run_attempt':1,'path':path,'head_sha':self.sha,
                    'head_repository':{'full_name':self.repo},'head_branch':'main',
                    'event':'push','status':'completed','conclusion':'success',
                    'run_started_at':'2026-10-15T22:00:00Z','updated_at':'2026-10-15T22:30:00Z'}
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
                             'system_pac_verified','wpad_dns_verified','url_pac_worker_verified','automatic_update_verified',
                             'workspace_proxy_credentials_verified','digest_proxy_credentials_verified','domain_pac_worker_verified','ntlm_proxy_worker_verified')
        self.proof.update({flag:True for flag in self.required_flags})
        from test_native_component_portable import good_component
        from test_url_pac_evidence import good_url_proof
        from test_domain_pac_evidence import good_domain_proof
        from test_ntlm_evidence import good_ntlm_proof
        from test_wpad_dns_evidence import good_evidence
        self.proof.update(native_components={c:good_component(c) for c in ('bundled','chrome')},
                          url_pac=good_url_proof(),domain_pac=good_domain_proof(),ntlm_proxy=good_ntlm_proof(),wpad_dns=good_evidence())
        from automatic_update_acceptance import FIELDS
        self.proof['automatic_update']=dict(schema_version=1,**{key:True for key in FIELDS})
        self.artifacts={name:{'id':i,'digest':'sha256:'+'e'*64,'size_in_bytes':100,
                              'created_at':'2026-10-15T22:29:00Z'}
                        for i,name in enumerate(self.mod.ARTIFACTS,200)}
        self.download_calls=[]
        downloader=patch.object(self.mod,'download_artifact',side_effect=self.download_fixture)
        downloader.start();self.addCleanup(downloader.stop)
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

    def test_release_outcomes_are_in_body_asset_and_checksums(self):
        output=self.root/'with-notes'
        result=self.mod.assemble(self.package,self.manifest,self.evidence,
                                 self.run,{},self.artifacts,output,NOTES)
        self.assertEqual((output/'CHANGES.md').read_text(encoding='utf8'),NOTES)
        self.assertTrue((output/'UPGRADE_AND_LIMITATIONS.md').read_text(encoding='utf8').startswith(NOTES))
        digest=self.mod.file_hash(output/'CHANGES.md')
        self.assertEqual(result['change_notes_sha256'],digest)
        self.assertIn(digest+'  CHANGES.md',(output/'SHA256SUMS.txt').read_text(encoding='utf8'))

    def test_missing_draft_outcomes_fail_before_network_or_output(self):
        output=self.root/'no-notes'
        with patch.object(self.mod,'api',side_effect=AssertionError('no remote work')), self.assertRaises(ValueError):
            self.mod.prepare(self.repo,self.run['id'],output,True)
        self.assertFalse(output.exists())

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
            return {'total_count':2,'artifacts':[{'name':name,'expired':False,
                      'workflow_run':{'id':self.run['id'],'head_sha':self.sha},**row}
                                               for name,row in self.artifacts.items()]}
        if suffix.startswith('git/matching-refs/tags/'):return []
        if suffix=='releases?per_page=100&page=1':return []
        raise AssertionError(suffix)

    def download_fixture(self,repo,record,destination):
        self.assertEqual(repo,self.repo)
        self.download_calls.append((record['id'],destination))
        name=next(name for name,row in self.artifacts.items() if row['id']==record['id'])
        destination.mkdir(parents=True)
        if name==self.mod.ARTIFACTS[0]:shutil.copyfile(self.package,destination/self.package.name)
        else:
            for file,data in (('portable-candidate/manifest.json',self.manifest),
                              ('release-verification/result.json',self.evidence)):
                path=destination/file;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text(json.dumps(data),encoding='utf8')

    def gh_fixture(self,*args):
        if args[:3]==('api','--method','POST'):
            body=json.loads(Path(args[-1]).read_text(encoding='utf8'))
            self.assertTrue(body['draft']);self.assertTrue(body['prerelease'])
            self.assertEqual(body['target_commitish'],self.sha)
            return json.dumps({'id':300,'html_url':'https://example.invalid/authored-draft'})
        if args[:2]==('release','upload'):
            self.assertNotIn('--clobber',args);return ''
        raise AssertionError(args)

    def test_stable_tag_keeps_unsigned_disclosure_and_default_draft(self):
        result=self.mod.assemble(self.package,self.manifest,self.evidence,self.run,{},self.artifacts,
                                 self.root/'stable',NOTES,channel='stable')
        self.assertEqual(result['tag'],'v0.2.0')
        self.assertEqual(result['release_channel'],'stable');self.assertFalse(result['signed'])
        notes=(self.root/'stable'/'UPGRADE_AND_LIMITATIONS.md').read_text(encoding='utf-8')
        self.assertIn('稳定版',notes);self.assertIn('未签名',notes);self.assertNotIn('预发行草稿',notes)
        with patch.object(self.mod,'api',side_effect=AssertionError('no network')):
            with self.assertRaises(ValueError):self.mod.prepare(self.repo,self.run['id'],self.root/'refused',publish=True)
            with self.assertRaises(ValueError):self.mod.prepare(self.repo,self.run['id'],self.root/'refused',channel='other')

    def test_publication_requires_exact_eight_uploaded_assets(self):
        result=self.mod.assemble(self.package,self.manifest,self.evidence,self.run,{},self.artifacts,
                                 self.root/'payload',NOTES,channel='stable')
        payload=self.root/'payload'
        release={'draft':True,'prerelease':False,'tag_name':'v0.2.0','target_commitish':self.sha,
                 'assets':[{'name':p.name,'state':'uploaded','size':p.stat().st_size,
                            'digest':'sha256:'+self.mod.file_hash(p)} for p in payload.iterdir()]}
        self.mod.validate_uploaded(release,payload,result,self.sha,'stable')
        for field in ('missing','digest','size','tag','target','channel'):
            damaged=copy.deepcopy(release)
            if field=='missing':damaged['assets'].pop()
            elif field=='digest':damaged['assets'][0]['digest']='sha256:'+'f'*64
            elif field=='size':damaged['assets'][0]['size']+=1
            elif field=='tag':damaged['tag_name']='v0.1.0'
            elif field=='target':damaged['target_commitish']='e'*40
            else:damaged['prerelease']=True
            with self.subTest(field=field),self.assertRaises(ValueError):
                self.mod.validate_uploaded(damaged,payload,result,self.sha,'stable')

    def test_explicit_stable_publication_happens_only_after_verified_upload(self):
        output=self.root/'publish';events=[]
        def api(repo,suffix):
            if suffix=='releases/300':
                payload=output/'assets';events.append('verify-upload')
                return {'draft':True,'prerelease':False,'tag_name':'v0.2.0','target_commitish':self.sha,
                        'assets':[{'name':p.name,'state':'uploaded','size':p.stat().st_size,
                                   'digest':'sha256:'+self.mod.file_hash(p)} for p in payload.iterdir()]}
            return self.api_fixture(suffix)
        def gh(*args):
            if args[:3]==('api','--method','POST'):
                request=json.loads(Path(args[-1]).read_text(encoding='utf-8'))
                self.assertTrue(request['draft']);self.assertFalse(request['prerelease'])
                self.assertEqual(request['tag_name'],'v0.2.0');events.append('draft')
                return json.dumps({'id':300,'html_url':'https://example.invalid/draft'})
            if args[:2]==('release','upload'):events.append('upload');return ''
            if args[:3]==('api','--method','PATCH'):
                self.assertEqual(events,['draft','upload','verify-upload'])
                request=json.loads(Path(args[-1]).read_text(encoding='utf-8'))
                self.assertEqual(request,{'draft':False,'prerelease':False,'make_latest':'true'})
                events.append('publish')
                return json.dumps({'id':300,'draft':False,'prerelease':False,'tag_name':'v0.2.0',
                                   'html_url':'https://example.invalid/published'})
            return self.gh_fixture(*args)
        with patch.object(self.mod,'api',side_effect=api),patch.object(self.mod,'gh',side_effect=gh):
            result=self.mod.prepare(self.repo,self.run['id'],output,True,NOTES,channel='stable',publish=True)
        self.assertTrue(result['published']);self.assertTrue((output/'published-release.json').is_file())
        self.assertEqual(events[-1],'publish')

    def test_selected_artifacts_only_and_draft_creation_never_rebuilds(self):
        with patch.object(self.mod,'api',side_effect=lambda repo,suffix:self.api_fixture(suffix)), \
             patch.object(self.mod,'gh',side_effect=self.gh_fixture) as command:
            result=self.mod.prepare(self.repo,self.run['id'],self.root/'download',True,NOTES)
        calls=[call.args for call in command.call_args_list]
        self.assertEqual(len(calls),2)
        self.assertIn('draft_url',result)
        self.assertEqual(self.download_calls,
                         [(row['id'],self.root/'download'/name) for name,row in self.artifacts.items()])

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
            self.mod.prepare(self.repo,self.run['id'],self.root/'changed',True,NOTES)
        command.assert_not_called()

    def test_existing_draft_or_tag_blocks_creation_and_keeps_preparation(self):
        for existing in ('tag','draft'):
            with self.subTest(existing=existing):
                def response(repo,suffix):
                    if existing=='tag' and suffix.startswith('git/matching-refs/tags/'):
                        return [{'ref':'refs/tags/'+suffix.split('tags/',1)[1]}]
                    if existing=='draft' and suffix=='releases?per_page=100&page=1':
                        prepared=json.loads((self.root/existing/'prepared.json').read_text(encoding='utf8'))
                        return [{'tag_name':prepared['tag']}]
                    return self.api_fixture(suffix)
                with patch.object(self.mod,'api',side_effect=response), \
                     patch.object(self.mod,'gh',side_effect=self.gh_fixture) as command, \
                     self.assertRaises(ValueError):
                    self.mod.prepare(self.repo,self.run['id'],self.root/existing,True,NOTES)
                self.assertTrue((self.root/existing/'prepared.json').is_file())
                command.assert_not_called()


    def test_candidate_tag_prefix_does_not_block_distinct_stable_tag(self):
        def response(repo,suffix):
            if suffix.startswith('git/matching-refs/'):
                return [{'ref':'refs/tags/v0.4.0-candidate.abcdef.1.1'}]
            return []
        with patch.object(self.mod,'api',side_effect=response):
            self.mod.require_unused_release(self.repo,'v0.4.0')

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
                    self.mod.prepare(self.repo,self.run['id'],self.root/defect,True,NOTES)
                command.assert_not_called()

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
                            self.mod.prepare(self.repo,self.run['id'],output,True,NOTES)
                        command.assert_not_called()
                    else:
                        result=self.mod.prepare(self.repo,self.run['id'],output,True,NOTES)
                        self.assertIn('draft_url',result)
                self.assertEqual(pages,[1,2])


    def test_green_flags_cannot_replace_nested_runtime_acceptance(self):
        for key in ('native_components','url_pac','domain_pac','wpad_dns'):
            for defect in ('missing','malformed','failed'):
                with self.subTest(key=key,defect=defect):
                    original=copy.deepcopy(self.proof)
                    if defect=='missing':self.proof.pop(key)
                    elif defect=='malformed':self.proof[key]={'success':True}
                    elif key=='native_components':
                        self.proof[key]['chrome']['cleanup']['bridge_exited']=False
                    elif key=='url_pac':self.proof[key]['calendar_invalid_rejected']=False
                    elif key=='domain_pac':self.proof[key]['canonical_root_matched']=False
                    else:self.proof[key]['fixture']['nrpt_cleanup']['restored']=False
                    package,manifest=self.archive()
                    with self.assertRaisesRegex(ValueError,'acceptance details'):
                        self.mod.validate_candidate(package,manifest,self.evidence,self.sha)
                    self.proof=original


if __name__=='__main__':
    unittest.main()
