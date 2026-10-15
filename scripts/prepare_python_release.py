"""Publish existing qualified Python artifacts alongside a verified Windows release."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import shutil
from prepare_portable_release import api, gh, read_json, file_hash, require, qualified_runs, require_unused_release
from release_summary import validate_summary

ROOT=Path(__file__).resolve().parents[1]
ARTIFACTS=('python-distribution-candidate','python-sdist-proof','python-installed-ubuntu-latest',
           'python-installed-windows-latest','python-installed-macos-latest')
PLATFORMS={'ubuntu-latest':'linux','windows-latest':'win32','macos-latest':'darwin'}


def write(path,value):
    with path.open('x',encoding='utf-8') as stream:json.dump(value,stream,ensure_ascii=False,indent=2)


def validate_install(proof,manifest,platform,wheel):
    require(proof.get('success') is True and proof.get('platform')==platform
        and proof.get('version')==manifest['version'] and proof.get('source_revision')==manifest['source_revision']
        and proof.get('source_sha256')==manifest['source_sha256'] and proof.get('wheel_sha256')==wheel['sha256']
        and proof.get('steps')==['offline_install','five_installed_entrypoints'], 'installed proof mismatch')
    observations=proof.get('observations')
    require(isinstance(observations,list) and len(observations)==2
        and [r.get('stage') for r in observations]==['import_and_original_report','independent_process_reopen']
        and all(r.get('success') is True and r.get('platform')==platform
                and r.get('version')==manifest['version'] and r.get('installed_payload_verified') is True
                and type(r.get('external_requests')) is int and r['external_requests']==0 for r in observations),
        'incomplete installed observations')


def prepare(repo,run_id,out,notes,*,publish=False):
    require(re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo) and type(run_id) is int and run_id>0,
            'invalid repository or run')
    notes=validate_summary(notes)
    out=Path(out).resolve();require(not out.exists(),'output exists; inspect previous attempt')
    selected=api(repo,f'actions/runs/{run_id}')
    sha=selected.get('head_sha','')
    require(selected.get('path')=='.github/workflows/python-distribution.yml'
        and selected.get('event')=='push' and selected.get('head_branch')=='main'
        and selected.get('status')=='completed' and selected.get('conclusion')=='success'
        and re.fullmatch('[0-9a-f]{40}',sha), 'select a successful exact-main Python distribution run')
    runs=api(repo,f'actions/runs?head_sha={sha}&event=push&per_page=100')['workflow_runs']
    windows=[r for r in runs if r['path']=='.github/workflows/windows-portable.yml'
             and r['head_sha']==sha and r['conclusion']=='success']
    require(len(windows)==1,'qualified Windows main candidate missing or ambiguous')
    qualification=qualified_runs(repo,windows[0])
    listing=api(repo,f'actions/runs/{run_id}/artifacts?per_page=100')
    require(listing['total_count']==len(listing['artifacts']),'incomplete artifact listing')
    bound={}
    for name in ARTIFACTS:
        matches=[a for a in listing['artifacts'] if a['name']==name and not a['expired']]
        require(len(matches)==1,'required Python artifact missing or ambiguous')
        bound[name]={'id':matches[0]['id'],'digest':matches[0].get('digest')}
    out.mkdir(parents=True)
    for name in ARTIFACTS:
        gh('run','download',str(run_id),'--repo',repo,'--name',name,'--dir',str(out/name))
    after=api(repo,f'actions/runs/{run_id}')
    refreshed=api(repo,f'actions/runs/{run_id}/artifacts?per_page=100')['artifacts']
    require(after['head_sha']==sha and after['run_attempt']==selected['run_attempt']
        and after['conclusion']=='success' and all([(a['id'],a.get('digest')) for a in refreshed
            if a['name']==name and not a['expired']]==[(row['id'],row['digest'])] for name,row in bound.items()),
        'qualification changed while downloading')
    candidate=out/ARTIFACTS[0];manifest=read_json(candidate/'manifest.json')
    version=manifest.get('version','')
    require(re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)',version)
        and manifest.get('success') is True and manifest.get('source_revision')==sha
        and manifest.get('status')=='python_distribution_candidate','candidate identity mismatch')
    assets=manifest.get('assets',[])
    require(isinstance(assets,list) and len(assets)==2 and len({a['name'] for a in assets})==2,
            'expected exactly two packages')
    wheel=next((a for a in assets if a['name']==f'vibe_job_radar-{version}-py3-none-any.whl'),None)
    sdist=next((a for a in assets if a['name'] in {f'vibe-job-radar-{version}.tar.gz',f'vibe_job_radar-{version}.tar.gz'}),None)
    require(wheel is not None and sdist is not None,'unexpected package names')
    for item in assets:
        file=candidate/item['name']
        require(file.is_file() and not file.is_symlink() and file.stat().st_size==item['size']
            and 0<item['size']<=20_000_000 and file_hash(file)==item['sha256'],'package bytes changed')
    proofs={}
    for os_name,platform in PLATFORMS.items():
        proof=read_json(out/('python-installed-'+os_name)/'result.json')
        validate_install(proof,manifest,platform,wheel);proofs[os_name]=proof
    source=read_json(out/'python-sdist-proof/result.json')
    require(source.get('success') is True and source.get('product_payload_identical') is True
        and source.get('source_revision')==sha and source.get('version')==version
        and source.get('source_sha256')==manifest['source_sha256']
        and source.get('sdist_sha256')==sdist['sha256'],'source rebuild proof mismatch')
    stable=api(repo,'releases/tags/v'+version)
    require(stable.get('draft') is False and stable.get('prerelease') is False
        and stable.get('target_commitish')==sha and len(stable.get('assets',[]))==8,
        'publish the matching qualified Windows stable release first')
    require(api(repo,'releases/latest').get('id')==stable['id'], 'matching Windows release is not Latest')
    tag='python-v'+version;require_unused_release(repo,tag)
    payload=out/'assets';payload.mkdir()
    for item in assets:shutil.copyfile(candidate/item['name'],payload/item['name'])
    shutil.copyfile(candidate/'manifest.json',payload/'manifest.json')
    write(payload/'CI_EVIDENCE.json',{'schema_version':1,'repository':repo,'source_revision':sha,
        'python_run':{'id':run_id,'attempt':selected['run_attempt']},'artifacts':bound,
        'windows_ci':qualification,'installed':proofs,'sdist':source,'rebuilt_for_release':False,
        'signed':False,'python_bundled':False,'browser_bundled':False})
    install=(ROOT/'docs/PYTHON_DISTRIBUTION.md').read_text(encoding='utf-8').replace('VERSION',version)
    (payload/'INSTALL.md').write_text(install,encoding='utf-8')
    (payload/'SHA256SUMS.txt').write_text(''.join(f'{file_hash(p)}  {p.name}\n'
        for p in sorted(payload.iterdir()) if p.is_file()),encoding='utf-8')
    result={'tag':tag,'source_revision':sha,'version':version,'published':False,'windows_release':stable['html_url']}
    write(out/'prepared.json',result)
    if not publish:return result
    require(qualified_runs(repo,api(repo,f"actions/runs/{windows[0]['id']}"))==qualification,
        'main qualification changed before publication')
    body=notes+'\n\nPython3.10+安装包；不包含解释器或浏览器，不是免安装桌面程序。\n\n[同版本Windows便携与自动升级]('+stable['html_url']+')。Python附件不加入Windows的8项资产，本Release不替换Latest。'
    request={'tag_name':tag,'target_commitish':sha,'name':'Python packages '+version,
             'body':body,'draft':True,'prerelease':False}
    write(out/'create-draft-request.json',request)
    created=json.loads(gh('api','--method','POST',f'repos/{repo}/releases','--input',str(out/'create-draft-request.json')))
    write(out/'created-draft.json',created)
    gh('release','upload',tag,'--repo',repo,*map(str,sorted(payload.iterdir())))
    uploaded=api(repo,f"releases/{created['id']}")
    expected={p.name:p for p in payload.iterdir()}
    require(uploaded.get('draft') is True and uploaded.get('tag_name')==tag
        and uploaded.get('target_commitish')==sha and len(uploaded['assets'])==len(expected)
        and {a['name'] for a in uploaded['assets']}==set(expected),'uploaded release identity mismatch')
    for asset in uploaded['assets']:
        file=expected[asset['name']]
        require(asset.get('state')=='uploaded' and asset.get('size')==file.stat().st_size
            and asset.get('digest')=='sha256:'+file_hash(file),'uploaded package mismatch')
    write(out/'publish-request.json',{'draft':False,'prerelease':False,'make_latest':'false'})
    published=json.loads(gh('api','--method','PATCH',f"repos/{repo}/releases/{created['id']}",
        '--input',str(out/'publish-request.json')))
    write(out/'published-release.json',published)
    require(published.get('draft') is False and published.get('prerelease') is False,'publication outcome uncertain')
    require(api(repo,'releases/latest').get('id')==stable['id'], 'Windows Latest changed; inspect publication')
    current=api(repo,f"releases/{stable['id']}")
    require(current.get('target_commitish')==sha and current.get('draft') is False
        and {a['id'] for a in current['assets']}=={a['id'] for a in stable['assets']},
        'Windows release changed before companion link')
    link='\n\n<!-- python-companion -->\n[Python3.10+跨平台安装包]('+published['html_url']+')：独立正式Tag，需已有Python；本Windows自动升级资产保持8项。\n<!-- /python-companion -->\n'
    write(out/'windows-link-request.json',{'body':current.get('body','')+link})
    linked=json.loads(gh('api','--method','PATCH',f"repos/{repo}/releases/{stable['id']}",
        '--input',str(out/'windows-link-request.json')))
    write(out/'windows-link-result.json',{'id':linked.get('id'),'link_present':published['html_url'] in linked.get('body','')})
    require(linked.get('id')==stable['id'] and published['html_url'] in linked.get('body',''),'companion link not confirmed')
    result.update(published=True,url=published['html_url'])
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository',required=True);parser.add_argument('--run-id',type=int,required=True)
    parser.add_argument('--out',type=Path,required=True);parser.add_argument('--change-notes',type=Path,required=True)
    parser.add_argument('--publish',action='store_true');args=parser.parse_args()
    print(json.dumps(prepare(args.repository,args.run_id,args.out,args.change_notes.read_text(encoding='utf-8'),
                             publish=args.publish),ensure_ascii=True))
