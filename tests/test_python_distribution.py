"""Release gates reject mismatched or incomplete installed-package evidence."""
import copy
import hashlib
import json
import tempfile
from unittest.mock import patch
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from prepare_python_release import validate_install
import prepare_python_release as release


class PythonDistributionProofTests(unittest.TestCase):
    def test_report_reopen_and_source_identity_are_required_on_each_platform(self):
        manifest={'version':'0.7.0','source_revision':'a'*40,'source_sha256':'b'*64}
        wheel={'sha256':'c'*64}
        for platform in ('linux','win32','darwin'):
            proof={'success':True,'platform':platform,**manifest,'wheel_sha256':wheel['sha256'],
                'steps':['offline_install','five_installed_entrypoints'],
                'observations':[{'success':True,'stage':stage,'version':'0.7.0','platform':platform,
                    'installed_payload_verified':True,'external_requests':0}
                    for stage in ('import_and_original_report','independent_process_reopen')]}
            validate_install(proof,manifest,platform,wheel)
            variants=[]
            for field,value in [('success',False),('source_revision','d'*40),('wheel_sha256','e'*64),
                                ('platform','different'),('steps',['offline_install'])]:
                changed=copy.deepcopy(proof);changed[field]=value;variants.append(changed)
            changed=copy.deepcopy(proof);changed['observations'].pop();variants.append(changed)
            changed=copy.deepcopy(proof);changed['observations'][1]['success']=False;variants.append(changed)
            changed=copy.deepcopy(proof);changed['observations'][0]['external_requests']=1;variants.append(changed)
            for changed in variants:
                with self.subTest(platform=platform,changed=changed),self.assertRaises(ValueError):
                    validate_install(changed,manifest,platform,wheel)



    def test_advancing_main_never_publishes_stale_artifacts(self):
        sha='a'*40;version='0.7.0'
        for advance_at in (2,3):
            with self.subTest(advance_at=advance_at),tempfile.TemporaryDirectory() as temp:
                out=Path(temp)/'release'
                packages={f'vibe_job_radar-{version}-py3-none-any.whl':b'wheel',
                          f'vibe_job_radar-{version}.tar.gz':b'sdist'}
                assets=[{'name':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()}
                        for name,data in packages.items()]
                manifest={'success':True,'status':'python_distribution_candidate','version':version,
                          'source_revision':sha,'source_sha256':'b'*64,'assets':assets}
                selected={'path':'.github/workflows/python-distribution.yml','event':'push',
                          'head_branch':'main','status':'completed','conclusion':'success',
                          'head_repository':{'full_name':'owner/repo'},'head_sha':sha,'run_attempt':1}
                windows={'id':6,'path':'.github/workflows/windows-portable.yml',
                         'head_sha':sha,'conclusion':'success'}
                artifact_rows=[{'id':i,'name':name,'expired':False,'digest':'sha256:'+str(i)*64}
                               for i,name in enumerate(release.ARTIFACTS,1)]
                stable={'id':8,'draft':False,'prerelease':False,'target_commitish':sha,
                        'assets':[{'id':i} for i in range(8)],'html_url':'https://example.test/windows'}
                main_reads=[];mutations=[]

                def api(repo,endpoint):
                    if endpoint=='git/ref/heads/main':
                        main_reads.append(endpoint)
                        return {'object':{'sha':'d'*40 if len(main_reads)>=advance_at else sha}}
                    if endpoint=='actions/runs/7':return selected
                    if endpoint=='actions/runs/6':return windows
                    if endpoint.startswith('actions/runs?'):return {'workflow_runs':[windows]}
                    if endpoint=='actions/runs/7/artifacts?per_page=100':
                        return {'total_count':5,'artifacts':artifact_rows}
                    if endpoint in ('releases/tags/v'+version,'releases/latest'):return stable
                    if endpoint=='releases/9':
                        return {'draft':True,'tag_name':'python-v'+version,'target_commitish':sha,
                                'assets':[{'name':p.name,'state':'uploaded','size':p.stat().st_size,
                                           'digest':'sha256:'+release.file_hash(p)}
                                          for p in (out/'assets').iterdir()]}
                    raise AssertionError(endpoint)

                def gh(*args):
                    if args[:2]==('run','download'):
                        name=args[args.index('--name')+1];dest=Path(args[args.index('--dir')+1])
                        dest.mkdir(parents=True)
                        if name=='python-distribution-candidate':
                            (dest/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
                            for filename,data in packages.items():(dest/filename).write_bytes(data)
                        elif name=='python-sdist-proof':
                            proof={'success':True,'product_payload_identical':True,'source_revision':sha,
                                   'version':version,'source_sha256':'b'*64,'sdist_sha256':assets[1]['sha256']}
                            (dest/'result.json').write_text(json.dumps(proof),encoding='utf-8')
                        else:
                            platform=release.PLATFORMS[name.removeprefix('python-installed-')]
                            proof={'success':True,'platform':platform,**manifest,'wheel_sha256':assets[0]['sha256'],
                                   'steps':['offline_install','five_installed_entrypoints'],
                                   'observations':[{'success':True,'stage':stage,'version':version,
                                       'platform':platform,'installed_payload_verified':True,'external_requests':0}
                                       for stage in ('import_and_original_report','independent_process_reopen')]}
                            (dest/'result.json').write_text(json.dumps(proof),encoding='utf-8')
                        return ''
                    mutations.append(args)
                    if args[:3]==('api','--method','POST'):return json.dumps({'id':9})
                    if args[:2]==('release','upload'):return ''
                    raise AssertionError('unexpected publication: '+repr(args))

                notes=''.join('## '+section+'\n已验证 #372。\n\n' for section in
                              ('解决了什么','提高了什么','验证结果','剩余问题'))
                with patch.object(release,'api',side_effect=api),patch.object(release,'gh',side_effect=gh), \
                     patch.object(release,'qualified_runs',return_value={'qualified':True}), \
                     patch.object(release,'require_unused_release'):
                    with self.assertRaisesRegex(ValueError,'main advanced'):
                        release.prepare('owner/repo',7,out,notes,publish=True)
                self.assertEqual(len(main_reads),advance_at)
                self.assertEqual(len(mutations),0 if advance_at==2 else 2)
                self.assertFalse((out/'published-release.json').exists())
                if advance_at==3:self.assertTrue((out/'created-draft.json').exists())

if __name__=='__main__':unittest.main()
