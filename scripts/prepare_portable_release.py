"""Promote an existing verified main candidate to an unsigned release.

No build, executable invocation, live-site traffic, workspace migration or
signing occurs here. Only the explicitly selected run's two artifacts are used.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

from release_summary import validate_summary
from build_windows_portable import native_components_valid
from url_pac_acceptance import valid_evidence as valid_url_pac
from automatic_update_acceptance import valid_evidence as valid_automatic_update
from wpad_dns_evidence import valid_wpad_dns_evidence

WORKFLOWS = {
    '.github/workflows/tests.yml', '.github/workflows/browser.yml',
    '.github/workflows/browser-choice.yml', '.github/workflows/browser-health.yml',
    '.github/workflows/native-browser.yml', '.github/workflows/vm-proxy.yml',
    '.github/workflows/windows-native-tls.yml', '.github/workflows/windows-portable.yml',
}
ARTIFACTS = ('windows-x64-portable-candidate', 'windows-portable-evidence')
REQUIRED_PORTABLE_FLAGS = (
    'startup_registration_verified', 'startup_worker_verified',
    'system_pac_verified', 'wpad_dns_verified', 'url_pac_worker_verified', 'automatic_update_verified',
    'workspace_proxy_credentials_verified', 'digest_proxy_credentials_verified',
)
HEX = re.compile(r'[0-9a-f]{64}')
REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical_run_id(value):
    if not re.fullmatch(r'[1-9][0-9]*', value):
        raise argparse.ArgumentTypeError('Run ID must use canonical positive decimal digits')
    return int(value)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for part in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(part)
    return digest.hexdigest()


def read_json(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 8_000_000,
            'Missing or oversized metadata')
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, value):
    with Path(path).open('x', encoding='utf-8') as out:
        json.dump(value, out, ensure_ascii=False, indent=2)


def gh(*args):
    result = subprocess.run(['gh', *args], capture_output=True, text=True,
                            encoding='utf-8', timeout=600 if args[:2] == ('run', 'download') else 120)
    require(result.returncode == 0, 'GitHub operation failed; inspect workflow step before retrying')
    return result.stdout


def api(repo, suffix):
    return json.loads(gh('api', f'repos/{repo}/{suffix}'))


def validate_runs(repo, selected, runs):
    require(REPO.fullmatch(repo), 'Invalid repository')
    sha = selected.get('head_sha')
    require(isinstance(sha, str) and re.fullmatch('[0-9a-f]{40}', sha), 'Invalid source revision')
    for run in [selected, *runs]:
        require(run.get('head_repository', {}).get('full_name') == repo,
                'Candidate must originate in this repository')
    require(selected.get('path') == '.github/workflows/windows-portable.yml',
            'Select the portable candidate workflow')
    require(type(selected.get('id')) is int and type(selected.get('run_attempt')) is int,
            'Missing run identity')
    qualified = {}
    for path in WORKFLOWS:
        matching = [r for r in runs if r.get('path') == path and r.get('head_sha') == sha
                    and r.get('event') == 'push' and r.get('head_branch') == 'main']
        require(len(matching) == 1, f'Missing or ambiguous main CI: {path}')
        run = matching[0]
        require(run.get('status') == 'completed' and run.get('conclusion') == 'success',
                f'Main CI incomplete or unsuccessful: {path}')
        qualified[path] = {'id': run['id'], 'attempt': run['run_attempt']}
    require(selected['id'] == qualified[selected['path']]['id']
            and selected['run_attempt'] == qualified[selected['path']]['attempt'],
            'Selected run is not the qualified main candidate')
    return qualified


def qualified_runs(repo, selected):
    listing = api(repo, f"actions/runs?head_sha={selected['head_sha']}&per_page=100")
    require(listing['total_count'] == len(listing['workflow_runs']), 'Too many CI runs; narrow selection')
    return validate_runs(repo, selected, listing['workflow_runs'])


def require_unused_release(repo, tag):
    matches = api(repo, f"git/matching-refs/tags/{tag}")
    require(isinstance(matches, list) and all(isinstance(row, dict) and isinstance(row.get('ref'), str)
                                             for row in matches), 'Invalid tag listing')
    require(not any(row['ref'] == 'refs/tags/'+tag for row in matches), 'Release tag already exists')
    page = 1
    while True:
        releases = api(repo, f'releases?per_page=100&page={page}')
        require(isinstance(releases, list), 'Invalid release inventory')
        require(all(r['tag_name'] != tag for r in releases), 'Release draft already exists')
        if len(releases) < 100:
            return
        page += 1


def zip_json(archive, name):
    info = archive.getinfo(name)
    require(info.file_size <= 8_000_000, 'Oversized embedded metadata')
    return json.loads(archive.read(info).decode('utf-8'))


def validate_candidate(candidate, manifest, source_evidence, sha):
    candidate = Path(candidate)
    require(candidate.is_file() and not candidate.is_symlink(), 'Missing candidate')
    require(re.fullmatch(r'vibe-job-radar-[0-9A-Za-z.+-]+-windows-x64\.zip', candidate.name),
            'Unsafe candidate name')
    require(manifest.get('file') == candidate.name, 'Candidate filename mismatch')
    require(manifest.get('size_bytes') == candidate.stat().st_size, 'Candidate size mismatch')
    digest = file_hash(candidate)
    require(manifest.get('sha256') == digest, 'Candidate hash mismatch')
    require(manifest.get('status') == 'portable_candidate_not_release'
            and manifest.get('runtime_verified') is True
            and manifest.get('live_sites_certified') is False
            and manifest.get('platform') == 'windows-x64', 'Candidate is not verified')
    require(all(manifest.get(flag) is True for flag in REQUIRED_PORTABLE_FLAGS),
            'Candidate lacks required Windows acceptance evidence')
    require(source_evidence.get('success') is True
            and source_evidence.get('source_unchanged') is True, 'Source qualification failed')
    with zipfile.ZipFile(candidate) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), 'Duplicate archive entries')
        meta = zip_json(archive, 'VibeJobRadar/PORTABLE.json')
        proof = zip_json(archive, 'VibeJobRadar/portable-verification.json')
    source = meta.get('source', {})
    require(source == source_evidence.get('source')
            and source.get('sha256') == manifest.get('source_sha256')
            and isinstance(source.get('sha256'), str) and HEX.fullmatch(source['sha256']),
            'Source evidence mismatch')
    runtime = meta.get('runtime_source', {})
    require(runtime.get('revision') == sha and runtime.get('raw_sha256') == source['sha256'],
            'Embedded candidate revision differs from selected main run')
    require(meta.get('status') == 'portable_candidate_not_release'
            and meta.get('platform') == 'windows-x64' and meta.get('browser_bundled') is True
            and meta.get('live_sites_certified') is False, 'Unexpected portable metadata')
    require(proof.get('success') is True and proof.get('verified_browser') == 'bundled'
            and proof.get('runtime_environment_verified') is True
            and proof.get('runtime_environment_restart_verified') is True,
            'Executable qualification failed or omitted bundled browser/runtime binding')
    require(all(proof.get(flag) is True for flag in REQUIRED_PORTABLE_FLAGS),
            'Executable lacks required Windows acceptance evidence')
    # Use the same structured acceptance gates as the current producer.
    require(native_components_valid(proof.get('native_components'))
            and valid_url_pac(proof.get('url_pac'))
            and valid_automatic_update(proof.get('automatic_update'))
            and valid_wpad_dns_evidence(proof.get('wpad_dns')),
            'Executable acceptance details are missing or invalid')
    require(runtime.get('executable_sha256') == proof.get('files', {}).get('VibeJobRadar.exe')
            and isinstance(runtime.get('executable_sha256'), str)
            and HEX.fullmatch(runtime['executable_sha256']), 'Executable evidence mismatch')
    version = meta.get('version')
    require(isinstance(version, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+)+(?:[A-Za-z0-9.+-]*)', version),
            'Invalid release version')
    require(candidate.name == f'vibe-job-radar-{version}-{digest[:16]}-windows-x64.zip',
            'Candidate version/hash filename mismatch')
    return {'version': version, 'zip_sha256': digest, 'source_sha256': source['sha256'],
            'portable_evidence': proof}


def assemble(candidate, manifest, source_evidence, selected, qualified, artifacts, output, change_notes=None, *, channel="prerelease"):
    require(channel in {'prerelease', 'stable'}, 'Invalid release channel')
    output = Path(output)
    require(not output.exists() and not output.is_symlink(), 'Output already exists')
    if change_notes is not None:
        change_notes = validate_summary(change_notes)
    result = validate_candidate(candidate, manifest, source_evidence, selected['head_sha'])
    repo = selected['head_repository']['full_name']
    require(REPO.fullmatch(repo), 'Invalid repository')
    tag = f"v{result['version']}-candidate.{selected['head_sha'][:12]}.{selected['id']}.{selected['run_attempt']}"
    if channel == "stable":
        require(re.fullmatch(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})",result["version"]), "Stable release requires a canonical version")
        tag = "v"+result["version"]
    output.mkdir(parents=True)
    archive = output/Path(candidate).name
    shutil.copyfile(candidate, archive)
    require(file_hash(archive) == result['zip_sha256'], 'Candidate changed during copy')
    write_json(output/'manifest.json', manifest)
    write_json(output/'source-verification.json', source_evidence)
    write_json(output/'portable-verification.json', result['portable_evidence'])
    provenance = {'schema_version': 1, 'status': 'unsigned_prerelease_draft' if channel == 'prerelease' else 'unsigned_stable_release_payload',
                  'release_channel': channel,
                  'repository': repo, 'tag': tag, 'source_revision': selected['head_sha'],
                  'source_sha256': result['source_sha256'], 'candidate_sha256': result['zip_sha256'],
                  'candidate_run': {'id': selected['id'], 'attempt': selected['run_attempt']},
                  'ci': qualified, 'artifacts': artifacts, 'rebuilt_for_release': False,
                  'signed': False, 'live_sites_certified': False,
                  'clean_machine_certified': False}
    if change_notes is not None:
        (output/'CHANGES.md').write_text(change_notes, encoding='utf-8')
        provenance['change_notes_sha256'] = file_hash(output/'CHANGES.md')
    write_json(output/'provenance.json', provenance)
    notes = (
        f"# Windows x64 便携候选 {result['version']}\n\n"
        f"发行渠道：{'稳定版' if channel == 'stable' else '预发行'}；未签名；源码提交 {selected['head_sha']}。\n"
        f"复用[已通过的原候选构建](https://github.com/{repo}/actions/runs/{selected['id']})，"
        "发布准备没有重新构建或重复执行测试。\n\n"
        "下载 ZIP 和 SHA256SUMS.txt 后，使用 PowerShell Get-FileHash -Algorithm SHA256 核对文件。"
        "摘要证明文件一致性，不是发行签名。完整解压 VibeJobRadar 目录后启动 VibeJobRadar.exe，"
        "保留随包文件；不要从 ZIP 内运行或只复制 exe。\n\n"
        "升级/回退：停止任务与计划，关闭所有使用同一工作区的程序；如启用过 Windows 登录启动，"
        "先关闭旧登记。备份整个工作区，包括正文、报告、个人材料、会话选择及配额；"
        "把新包解压到另一个目录，继续选择原工作区，保留旧程序与完整备份。"
        "如需回退，先关闭新程序，再使用已核验的旧程序和对应备份。"
        "完整网址 PAC 用户须先在新版切回兼容网络偏好，再回退旧版。\n\n"
        f"[工作区兼容与回退](https://github.com/{repo}/blob/{selected['head_sha']}/docs/WORKSPACE_COMPATIBILITY.md)"
        f" · [完整便携说明](https://github.com/{repo}/blob/{selected['head_sha']}/docs/WINDOWS_PORTABLE.md)\n\n"
        "保留原源码启动、便携启动、手工导入、采集、分析和报告功能。"
        "自动升级需单独确认并先备份，保留原程序；不自动清空或回写工作区。真实账号/滑块短信、三站完整 JD、干净个人设备与新手验收、"
        "连续24小时及真实Windows重新登录仍分别待验收。CI runner自带Python；"
        "移除子进程Python PATH的验证不等于未安装Python的个人电脑已验收。"
        "本机配套Chromium曾有未定位崩溃，明确选择Edge的历史通过也不替代本包现场验收。\n"
    )
    if change_notes is not None:
        notes = change_notes + '\n' + notes
    (output/'UPGRADE_AND_LIMITATIONS.md').write_text(notes, encoding='utf-8')
    checksums = ''.join(f'{file_hash(p)}  {p.name}\n' for p in sorted(output.iterdir()) if p.is_file())
    (output/'SHA256SUMS.txt').write_text(checksums, encoding='utf-8')
    return provenance


def prepare(repo, run_id, out, create_draft=False, change_notes=None, *, channel="prerelease", publish=False):
    require(channel in {"prerelease", "stable"}, "Invalid release channel")
    require(type(publish) is bool and (not publish or create_draft), "Publishing requires explicit draft creation")
    require(REPO.fullmatch(repo) and type(run_id) is int and run_id > 0, 'Invalid run selection')
    if create_draft or change_notes is not None:
        change_notes = validate_summary(change_notes)
    out = Path(out)
    require(not out.exists() and not out.is_symlink(), 'Output already exists; inspect previous attempt')
    selected = api(repo, f'actions/runs/{run_id}')
    require(isinstance(selected.get('head_sha'), str) and re.fullmatch('[0-9a-f]{40}', selected['head_sha']),
            'Invalid selected source revision')
    qualified = qualified_runs(repo, selected)
    listing = api(repo, f'actions/runs/{run_id}/artifacts?per_page=100')
    require(listing['total_count'] == len(listing['artifacts']), 'Incomplete artifact listing')
    artifacts = {}
    for name in ARTIFACTS:
        rows = [a for a in listing['artifacts'] if a['name'] == name and not a['expired']]
        require(len(rows) == 1, 'Missing, expired or ambiguous candidate artifact')
        artifacts[name] = {'id': rows[0]['id'], 'digest': rows[0].get('digest')}
    out.mkdir(parents=True)
    for name in ARTIFACTS:
        gh('run', 'download', str(run_id), '--repo', repo, '--name', name, '--dir', str(out/name))
    current = api(repo, f'actions/runs/{run_id}')
    require(current['run_attempt'] == selected['run_attempt']
            and current['head_sha'] == selected['head_sha'] and current['conclusion'] == 'success',
            'Selected build changed during download')
    refreshed = api(repo, f'actions/runs/{run_id}/artifacts?per_page=100')['artifacts']
    require(all([(a['id'], a.get('digest')) for a in refreshed if a['name'] == name and not a['expired']]
                == [(record['id'], record['digest'])] for name, record in artifacts.items()),
            'Candidate artifacts changed during download')
    packages = list((out/ARTIFACTS[0]).glob('*.zip'))
    require(len(packages) == 1, 'Candidate artifact must contain one ZIP')
    evidence = out/ARTIFACTS[1]
    manifest = read_json(evidence/'portable-candidate/manifest.json')
    source = read_json(evidence/'release-verification/result.json')
    payload = out/'assets'
    result = assemble(packages[0], manifest, source, selected, qualified, artifacts, payload, change_notes, channel=channel)
    write_json(out/'prepared.json', result)
    if create_draft:
        # Never edit/overwrite a tag or an existing draft, including after an
        # uncertain prior attempt. A failed upload leaves that draft inspectable.
        require_unused_release(repo, result['tag'])
        current = api(repo, f'actions/runs/{run_id}')
        require(current['head_sha'] == selected['head_sha']
                and qualified_runs(repo, current) == qualified,
                'Qualifying CI changed during preparation')
        body = (payload/'UPGRADE_AND_LIMITATIONS.md').read_text(encoding='utf-8')
        request = {'tag_name': result['tag'], 'target_commitish': selected['head_sha'],
                   'name': f"Windows portable {result['tag']} (unsigned)",
                   'body': body, 'draft': True, 'prerelease': channel == 'prerelease'}
        write_json(out/'create-draft-request.json', request)
        response = json.loads(gh('api', '--method', 'POST', f'repos/{repo}/releases',
                                 '--input', str(out/'create-draft-request.json')))
        write_json(out/'created-draft.json', response)
        gh('release', 'upload', result['tag'], '--repo', repo, *map(str, sorted(payload.iterdir())))
        result['draft_url'] = response['html_url']
        if publish:
            uploaded = api(repo, f"releases/{response['id']}")
            validate_uploaded(uploaded, payload, result, selected['head_sha'], channel)
            require(qualified_runs(repo, api(repo, f'actions/runs/{run_id}')) == qualified,
                    'Qualifying CI changed before publication')
            request = {'draft': False, 'prerelease': channel == 'prerelease',
                       'make_latest': 'true' if channel == 'stable' else 'false'}
            write_json(out/'publish-request.json', request)
            published = json.loads(gh('api','--method','PATCH',f"repos/{repo}/releases/{response['id']}",
                                      '--input',str(out/'publish-request.json')))
            write_json(out/'published-release.json', published)
            require(published.get('draft') is False and published.get('prerelease') == (channel == 'prerelease')
                    and published.get('tag_name') == result['tag'], 'Publication response mismatch; inspect remote release')
            result['release_url'] = published['html_url']
            result['published'] = True
    return result


def validate_uploaded(release, payload, result, sha, channel):
    require(release.get('draft') is True and release.get('tag_name') == result['tag']
            and release.get('target_commitish') == sha
            and release.get('prerelease') == (channel == 'prerelease'), 'Draft identity mismatch')
    expected = {p.name:p for p in payload.iterdir() if p.is_file()}
    assets = release.get('assets', [])
    require(len(expected) == 8 and len(assets) == 8
            and {a.get('name') for a in assets} == set(expected), 'Incomplete uploaded assets')
    for asset in assets:
        path = expected[asset['name']]
        require(asset.get('state') == 'uploaded' and asset.get('size') == path.stat().st_size
                and asset.get('digest') == 'sha256:'+file_hash(path), 'Uploaded asset digest mismatch')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--run-id', required=True, type=canonical_run_id)
    parser.add_argument('--out', type=Path, default=Path('release-draft'))
    parser.add_argument('--create-draft', action='store_true')
    parser.add_argument('--channel', choices=('prerelease','stable'), default='prerelease')
    parser.add_argument('--publish', action='store_true', help='Publish only after exact uploaded asset and CI verification')
    parser.add_argument('--change-notes', type=Path, help='Reviewed UTF-8 outcomes; required to create a draft')
    args = parser.parse_args()
    try:
        change_notes = None
        if args.change_notes is not None:
            require(args.change_notes.is_file() and not args.change_notes.is_symlink()
                    and args.change_notes.stat().st_size <= 65536, 'Invalid release summary file')
            change_notes = args.change_notes.read_text(encoding='utf-8-sig')
        result = prepare(args.repository, args.run_id, args.out, args.create_draft, change_notes, channel=args.channel, publish=args.publish)
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
        print(f'Release preparation stopped: {error}')
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
