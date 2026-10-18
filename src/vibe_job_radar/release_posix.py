"""Verified companion releases and native side-by-side extraction."""
import hashlib
import json
import re
import tempfile
from pathlib import Path

from . import posix_artifacts
from .release_platform import POSIX_PLATFORMS
from .release_updates import (WEB, REPOSITORY, WORKFLOWS, HEX, MAX_ZIP, UpdateError,
    require, version, digest, check_stop, remove_owned_tree)

NATIVE_CHECKS = (
    'isolated_path_unicode_workspace_and_doctor',
    'frozen_chromium_blank_guard_and_cleanup',
    'frozen_ntlmv2_protocol_worker_and_cleanup',
    'native_os_credentials_source_and_frozen',
    'resources_and_frozen_source_identity',
    'frozen_url_and_domain_pac_workers',
    'authored_full_jd_and_original_report',
    'independent_process_report_and_runtime_reopen',
    'native_automatic_update_handoff',
    'immutable_extracted_payload',
)
HANDOFF_FIELDS = ('frozen_executable', 'same_build_handoff', 'original_data_preserved',
    'backup_verified', 'new_http_ready', 'committed', 'owned_child_cleaned',
    'parent_exit_pipe', 'internal_links_preserved', 'executable_permissions_preserved')


def valid_handoff(value):
    return (isinstance(value, dict) and set(value) == {'schema_version', *HANDOFF_FIELDS}
        and type(value['schema_version']) is int and value['schema_version'] == 1
        and all(value[key] is True for key in HANDOFF_FIELDS))


def inspect_release(latest, transport, cancelled, platform, *, metadata_cache=None):
    require(platform in POSIX_PLATFORMS and isinstance(latest, dict)
        and latest.get('draft') is False and latest.get('prerelease') is False)
    selected = latest.get('tag_name', '')
    require(isinstance(selected, str) and selected.startswith('v'))
    selected = selected[1:]; version(selected)
    require(latest.get('html_url') == WEB+'/releases/tag/v'+selected)
    revision = latest.get('target_commitish')
    require(isinstance(revision, str) and re.fullmatch('[0-9a-f]{40}', revision))
    tag = 'desktop-v'+selected
    release = transport.release(tag)
    require(isinstance(release, dict) and release.get('draft') is False
        and release.get('prerelease') is False and type(release.get('id')) is int
        and release['id'] > 0 and release.get('tag_name') == tag
        and release.get('target_commitish') == revision
        and release.get('html_url') == WEB+'/releases/tag/'+tag)
    assets = release.get('assets'); require(isinstance(assets, list) and len(assets) == 10)
    by_name = {}
    for asset in assets:
        require(isinstance(asset, dict) and isinstance(asset.get('name'), str))
        name = asset['name']
        require(name not in by_name and '/' not in name and chr(92) not in name
            and asset.get('state') == 'uploaded' and type(asset.get('size')) is int
            and 0 < asset['size'] <= MAX_ZIP and type(asset.get('id')) is int and asset['id'] > 0
            and asset.get('browser_download_url') == WEB+'/releases/download/'+tag+'/'+name
            and isinstance(asset.get('digest'), str) and re.fullmatch('sha256:[0-9a-f]{64}',asset['digest']))
        by_name[name] = asset
    small = {'CI_EVIDENCE.json', 'CHANGES.md', 'INSTALL_AND_LIMITATIONS.md', 'SHA256SUMS.txt'}
    small |= {p+s for p in POSIX_PLATFORMS for s in ('-manifest.json','-verification.json')}
    require(small <= set(by_name))
    packages = {}
    for p in POSIX_PLATFORMS:
        matches = [a for n,a in by_name.items() if re.fullmatch(
            'vibe-job-radar-'+re.escape(selected)+'-[0-9a-f]{16}-'+p+r'[.]tar[.]gz',n)]
        require(len(matches) == 1); packages[p] = matches[0]
    require(set(by_name) == small | {p['name'] for p in packages.values()})
    needed = small - {p+s for p in POSIX_PLATFORMS if p != platform
                     for s in ('-manifest.json','-verification.json')}
    data = {}
    for name in sorted(needed):
        check_stop(cancelled); asset = by_name[name]; require(asset['size'] <= 1_000_000)
        key = (asset['digest'],asset['size'])
        raw = metadata_cache.pop(key,None) if metadata_cache is not None else None
        if not (isinstance(raw,bytes) and len(raw)==asset['size'] and 'sha256:'+digest(raw)==asset['digest']):
            raw = transport.small(asset,cancelled)
        require(isinstance(raw,bytes) and len(raw)==asset['size'] and 'sha256:'+digest(raw)==asset['digest'])
        if metadata_cache is not None:
            metadata_cache[key]=raw
            while len(metadata_cache)>8:metadata_cache.popitem(last=False)
        data[name]=raw
    sums={}
    for line in data['SHA256SUMS.txt'].decode('utf-8').splitlines():
        match=re.fullmatch(r'([0-9a-f]{64})  ([^/\\]+)',line)
        require(match is not None and match[2] not in sums);sums[match[2]]=match[1]
    require(set(sums)==set(by_name)-{'SHA256SUMS.txt'}
        and all(by_name[n]['digest']=='sha256:'+sha for n,sha in sums.items()))
    manifest=json.loads(data[platform+'-manifest.json'])
    proof=json.loads(data[platform+'-verification.json']); ci=json.loads(data['CI_EVIDENCE.json'])
    package=packages[platform]; source=manifest.get('source_sha256')
    require(isinstance(source,str) and HEX.fullmatch(source)
        and manifest.get('status')=='posix_portable_candidate'
        and manifest.get('file')==package['name'] and manifest.get('sha256')==sums[package['name']]
        and manifest.get('size_bytes')==package['size']
        and package['name']==f"vibe-job-radar-{selected}-{manifest['sha256'][:16]}-{platform}.tar.gz"
        and manifest.get('runtime_evidence_sha256')==sums[platform+'-verification.json']
        and isinstance(manifest.get('files'),dict))
    for value in (manifest,proof):
        require(value.get('success') is True and value.get('version')==selected
            and value.get('platform')==platform and value.get('source_revision')==revision
            and value.get('source_sha256')==source and value.get('python_required') is False
            and value.get('browser_bundled') is True and value.get('signed') is False
            and value.get('notarized') is False)
    # Read the already-published 0.12 proof for manual preparation as well.
    automatic=manifest.get('automatic_update'); require(type(automatic) is bool)
    checks=NATIVE_CHECKS if automatic else tuple(c for c in NATIVE_CHECKS if c!='native_automatic_update_handoff')
    require(tuple(proof.get('checks',[]))==checks and manifest.get('runtime_checks')==len(checks)
        and (not automatic or valid_handoff(proof.get('automatic_update'))))
    paths=WORKFLOWS | {'.github/workflows/'+n for n in
        ('python-distribution.yml','live-public-example.yml','posix-portable.yml')}
    qualified=ci.get('main_ci')
    require(ci.get('repository')==REPOSITORY and ci.get('source_revision')==revision
        and ci.get('rebuilt_for_release') is False and isinstance(qualified,dict) and set(qualified)==paths
        and all(isinstance(r,dict) and type(r.get('id')) is int and r['id']>0
            and type(r.get('attempt')) is int and r['attempt']>0 for r in qualified.values())
        and ci.get('native_run')==qualified['.github/workflows/posix-portable.yml'])
    stamp=proof.get('runtime_environment',{}).get('source',{})
    require(stamp.get('revision')==revision and stamp.get('raw_sha256')==source
        and stamp.get('executable_sha256')==manifest['files'].get('VibeJobRadar/VibeJobRadar',{}).get('sha256'))
    notes=data['CHANGES.md'].decode('utf-8')
    for heading in ('解决了什么','提高了什么','验证结果','剩余问题'):require('## '+heading in notes)
    return dict(release_id=release['id'],version=selected,tag=tag,release_url=release['html_url'],
        package=package,sha256=manifest['sha256'],revision=revision,source_sha256=source,
        platform=platform,notes=notes[:30000],signed=False,files=manifest['files'],automatic_update=automatic)


def _metadata(stream, rows, info):
    name='VibeJobRadar/PORTABLE.json'; entry=rows.get(name,{})
    require(entry.get('kind')=='file' and entry.get('size',8_000_001)<=8_000_000)
    with posix_artifacts.open_archive(stream) as archive:
        meta=json.load(archive.extractfile(name))
    require(meta.get('version')==info['version'] and meta.get('platform')==info['platform']
        and meta.get('runtime_source',{}).get('revision')==info['revision']
        and meta.get('source',{}).get('sha256')==info['source_sha256']
        and meta.get('runtime_source',{}).get('executable_sha256')==rows.get('VibeJobRadar/VibeJobRadar',{}).get('sha256')
        and rows.get('VibeJobRadar/VibeJobRadar',{}).get('kind')=='file'
        and rows['VibeJobRadar/VibeJobRadar'].get('mode',0)&0o111)
    if info.get('automatic_update') is True:
        require(meta.get('automatic_update') is True and meta.get('upgrade_protocol')==2)
    return meta


def checked_archive(stream, info, cancelled):
    hasher=hashlib.sha256(); stream.seek(0)
    for block in iter(lambda:stream.read(1024*1024),b''):
        check_stop(cancelled);hasher.update(block)
    require(hasher.hexdigest()==info['sha256'])
    rows=posix_artifacts.archive_inventory(stream,progress=lambda:check_stop(cancelled))
    if 'files' in info:require(rows==info['files'])
    _metadata(stream,rows,info)
    return rows


def stage_package(package, info, updates, cancelled):
    require(info.get('platform') in POSIX_PLATFORMS and package.parent.resolve()==updates.resolve()
        and package.is_file() and not package.is_symlink())
    with package.open('rb') as stream:
        rows=checked_archive(stream,info,cancelled)
        created=Path(tempfile.mkdtemp(prefix='v'+info['version']+'-',dir=updates))
        identity=(created.stat().st_dev,created.stat().st_ino)
        try:
            posix_artifacts.extract_verified(stream,created/'payload',rows,progress=lambda:check_stop(cancelled))
            # Keep the existing update layout: updates/v*/VibeJobRadar.
            (created/'payload'/'VibeJobRadar').rename(created/'VibeJobRadar')
            (created/'payload').rmdir()
            (created/'READY.json').write_text(json.dumps(dict(version=info['version'],sha256=info['sha256'],
                revision=info['revision'],platform=info['platform'],signed=False,workspace_migrated=False)),encoding='utf-8')
            return created/'VibeJobRadar'
        except BaseException:
            remove_owned_tree(created,identity,updates);raise


def verify_directory(directory, package, selected, cancelled):
    with package.open('rb') as stream:
        rows=checked_archive(stream,selected,cancelled)
    require(posix_artifacts.inventory(directory)==rows)
    require(json.loads((directory/'PORTABLE.json').read_text(encoding='utf-8')).get('upgrade_protocol')==2)
