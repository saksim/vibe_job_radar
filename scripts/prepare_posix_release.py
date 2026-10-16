"""Publish qualified Linux/macOS bundles without rebuilding or changing Latest."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import shutil
import tarfile

from prepare_portable_release import (api, gh, require, read_json, write_json, file_hash,
                                     qualified_runs, require_unused_release)
from release_artifacts import select_artifacts, download_artifact
from release_summary import validate_summary
from posix_artifacts import archive_inventory, TOP
from build_windows_portable import native_component_valid
from url_pac_acceptance import valid_evidence as valid_url_pac
from domain_pac_acceptance import valid_evidence as valid_domain_pac
from ntlm_proxy_acceptance import valid_component as valid_ntlm

ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = ('linux-x64', 'macos-arm64')
CHECKS = (
    'isolated_path_unicode_workspace_and_doctor',
    'frozen_chromium_blank_guard_and_cleanup',
    'frozen_ntlmv2_protocol_worker_and_cleanup',
    'resources_and_frozen_source_identity',
    'frozen_url_and_domain_pac_workers',
    'authored_full_jd_and_original_report',
    'independent_process_report_and_runtime_reopen',
    'immutable_extracted_payload',
)


def select_platform_artifacts(selected, listing, job_listing):
    """Reused successful jobs keep their original production windows on rerun."""
    jobs = job_listing.get('jobs')
    require(isinstance(jobs, list) and len(jobs) == job_listing.get('total_count') == 2,
            'Expected both native producer jobs')
    chosen = {}
    for platform in PLATFORMS:
        matching = [j for j in jobs if j.get('name') == platform]
        require(len(matching) == 1, 'Missing or ambiguous native producer')
        job = matching[0]
        require(job.get('run_id') == selected['id'] and job.get('head_sha') == selected['head_sha']
                and job.get('status') == 'completed' and job.get('conclusion') == 'success',
                'Native producer not successful on selected source')
        window = {**selected, 'run_started_at': job.get('started_at'),
                  'updated_at': job.get('completed_at')}
        names = ('posix-candidate-' + platform, 'posix-evidence-' + platform)
        chosen.update(select_artifacts(window, listing, names))
    require(len({a['id'] for a in chosen.values()}) == 4, 'Duplicate native artifact identity')
    return chosen


def validate_candidate(archive, manifest, proof_path, sha, platform):
    version = manifest.get('version', '')
    require(re.fullmatch(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)', version),
            'Invalid stable native package version')
    require(manifest.get('platform') == platform and platform in PLATFORMS
            and manifest.get('success') is True and manifest.get('status') == 'posix_portable_candidate'
            and manifest.get('source_revision') == sha
            and isinstance(manifest.get('source_sha256'), str)
            and re.fullmatch('[0-9a-f]{64}', manifest['source_sha256']), 'Native manifest identity mismatch')
    archive = Path(archive)
    require(archive.is_file() and not archive.is_symlink()
            and type(manifest.get('size_bytes')) is int and 0 < manifest['size_bytes'] <= 600_000_000
            and archive.stat().st_size == manifest['size_bytes'], 'Native archive size mismatch')
    digest = file_hash(archive)
    require(manifest.get('sha256') == digest
            and manifest.get('file') == archive.name
            and archive.name == f'vibe-job-radar-{version}-{digest[:16]}-{platform}.tar.gz',
            'Native archive identity mismatch')
    proof = read_json(proof_path)
    require(file_hash(proof_path) == manifest.get('runtime_evidence_sha256')
            and proof.get('success') is True and proof.get('source_revision') == sha
            and proof.get('source_sha256') == manifest['source_sha256']
            and proof.get('platform') == platform and proof.get('version') == version
            and tuple(proof.get('checks', [])) == CHECKS
            and type(manifest.get('runtime_checks')) is int and manifest['runtime_checks'] == len(CHECKS),
            'Native runtime evidence mismatch')
    for value in (manifest, proof):
        require(value.get('python_required') is False and value.get('browser_bundled') is True
                and all(value.get(key) is False for key in
                        ('signed', 'notarized', 'live_sites_certified', 'clean_machine_certified')),
                'Unsupported native certification')
    require(manifest.get('automatic_update') is False and manifest.get('os_credential_store') is False,
            'Unsupported platform-specific capability')
    require(native_component_valid(proof.get('native_component'), 'bundled')
            and valid_url_pac(proof.get('url_pac')) and valid_domain_pac(proof.get('domain_pac'))
            and valid_ntlm(proof.get('ntlm_component')), 'Incomplete frozen component evidence')
    rows = archive_inventory(archive)
    require(rows == manifest.get('files'), 'Native archive contents differ')
    metadata_name = TOP + '/PORTABLE.json'
    require(rows.get(metadata_name, {}).get('kind') == 'file'
            and rows[metadata_name]['size'] <= 8_000_000, 'Embedded native metadata missing')
    with tarfile.open(archive, 'r:gz') as content:
        metadata = json.load(content.extractfile(metadata_name))
    stamp = metadata.get('runtime_source')
    require(metadata.get('status') == 'posix_portable_candidate'
            and metadata.get('platform') == platform and metadata.get('version') == version
            and metadata.get('source', {}).get('sha256') == manifest['source_sha256']
            and isinstance(stamp, dict) and stamp.get('revision') == sha
            and stamp.get('raw_sha256') == manifest['source_sha256']
            and stamp.get('executable_sha256') == rows.get(TOP + '/VibeJobRadar', {}).get('sha256')
            and rows.get(TOP + '/VibeJobRadar', {}).get('mode', 0) & 0o111
            and proof.get('runtime_environment', {}).get('source') == stamp,
            'Embedded executable/source identity mismatch')
    require(metadata.get('python_required') is False and metadata.get('browser_bundled') is True
            and metadata.get('bundled_browser_directory') == 'browsers'
            and all(metadata.get(key) is False for key in
                    ('signed', 'notarized', 'live_sites_certified', 'clean_machine_certified',
                     'automatic_update', 'os_credential_store')), 'Embedded capability mismatch')
    return proof


def qualification(repo, selected):
    sha = selected.get('head_sha', '')
    require(selected.get('path') == '.github/workflows/posix-portable.yml'
            and selected.get('head_repository', {}).get('full_name') == repo
            and selected.get('head_branch') == 'main' and selected.get('event') == 'push'
            and selected.get('status') == 'completed' and selected.get('conclusion') == 'success'
            and isinstance(sha, str) and re.fullmatch('[0-9a-f]{40}', sha),
            'Select a successful native package run on exact main')
    require(api(repo, 'git/ref/heads/main').get('object', {}).get('sha') == sha,
            'Main advanced; qualify current main')
    runs = api(repo, f'actions/runs?head_sha={sha}&event=push&per_page=100')
    require(runs.get('total_count') == len(runs.get('workflow_runs', [])), 'Incomplete main run listing')
    windows = [r for r in runs['workflow_runs'] if r.get('path') == '.github/workflows/windows-portable.yml'
               and r.get('head_sha') == sha]
    require(len(windows) == 1, 'Missing or ambiguous Windows qualification')
    qualified = qualified_runs(repo, windows[0])
    require(qualified.get(selected['path']) == {'id': selected['id'], 'attempt': selected['run_attempt']},
            'Native qualification changed')
    return qualified


def prepare(repo, run_id, out, notes, *, publish=False):
    require(isinstance(repo, str) and re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)
            and type(run_id) is int and run_id > 0, 'Invalid repository or run')
    notes = validate_summary(notes)
    out = Path(out).resolve()
    require(not out.exists(), 'Output exists; inspect prior publication')
    selected = api(repo, f'actions/runs/{run_id}')
    qualified = qualification(repo, selected)
    sha = selected['head_sha']
    def selection():
        return select_platform_artifacts(selected,
            api(repo, f'actions/runs/{run_id}/artifacts?per_page=100'),
            api(repo, f'actions/runs/{run_id}/jobs?filter=latest&per_page=100'))
    bound = selection()
    out.mkdir(parents=True)
    write_json(out/'selected-artifacts.json', bound)
    for name, row in bound.items():
        download_artifact(repo, row, out/name)
    refreshed = api(repo, f'actions/runs/{run_id}')
    require(refreshed.get('head_sha') == sha and refreshed.get('run_attempt') == selected['run_attempt']
            and refreshed.get('conclusion') == 'success' and selection() == bound,
            'Native artifacts changed while downloading')
    payload = out/'assets';payload.mkdir()
    manifests, proofs = {}, {}
    for platform in PLATFORMS:
        evidence = out/('posix-evidence-' + platform)
        manifest = read_json(evidence/'manifest.json')
        files = list((out/('posix-candidate-' + platform)).iterdir())
        require(len(files) == 1, 'Native artifact must contain exactly one archive')
        proof_path = evidence/'runtime-verification.json'
        proofs[platform] = validate_candidate(files[0], manifest, proof_path, sha, platform)
        manifests[platform] = manifest
        shutil.copyfile(files[0], payload/files[0].name)
        shutil.copyfile(evidence/'manifest.json', payload/(platform+'-manifest.json'))
        shutil.copyfile(proof_path, payload/(platform+'-verification.json'))
    versions = {m['version'] for m in manifests.values()}
    require(len(versions) == 1 and len({m['source_sha256'] for m in manifests.values()}) == 1,
            'Native packages do not share a version and source')
    version = versions.pop()
    stable = api(repo, 'releases/tags/v'+version)
    python = api(repo, 'releases/tags/python-v'+version)
    for release, count in ((stable, 8), (python, 6)):
        require(release.get('draft') is False and release.get('prerelease') is False
                and release.get('target_commitish') == sha and len(release.get('assets', [])) == count,
                'Publish matching qualified Windows and Python releases first')
    require(api(repo, 'releases/latest').get('id') == stable['id'], 'Matching Windows release is not Latest')
    tag = 'desktop-v'+version
    require_unused_release(repo, tag)
    write_json(payload/'CI_EVIDENCE.json', dict(schema_version=1, repository=repo, source_revision=sha,
        native_run=dict(id=run_id, attempt=selected['run_attempt']), artifacts=bound,
        main_ci=qualified, rebuilt_for_release=False, clean_machine_certified=False))
    (payload/'CHANGES.md').write_text(notes, encoding='utf-8')
    install = (ROOT/'docs/POSIX_DISTRIBUTION.md').read_text(encoding='utf-8').replace('VERSION', version)
    (payload/'INSTALL_AND_LIMITATIONS.md').write_text(install, encoding='utf-8')
    (payload/'SHA256SUMS.txt').write_text(''.join(f'{file_hash(p)}  {p.name}\n'
        for p in sorted(payload.iterdir())), encoding='utf-8')
    require(len(list(payload.iterdir())) == 10, 'Expected ten native release assets')
    result = dict(tag=tag, source_revision=sha, version=version, published=False)
    write_json(out/'prepared.json', result)
    if not publish:
        return result
    require(qualification(repo, api(repo, f'actions/runs/{run_id}')) == qualified and selection() == bound,
            'Main qualification changed before publication')
    body = notes + '\n\nLinux x64 / macOS arm64 自带 Python、Chromium 的完整程序包。未签名、未公证；需要匹配系统库，个人干净设备仍待验收。\n\n[Windows 自动升级版]('+stable['html_url']+') · [Python 安装包]('+python['html_url']+')。本配套 Release 不替换 Latest。'
    write_json(out/'create-draft-request.json', dict(tag_name=tag, target_commitish=sha,
        name='Linux / macOS desktop '+version, body=body, draft=True, prerelease=False))
    created = json.loads(gh('api', '--method', 'POST', f'repos/{repo}/releases',
                           '--input', str(out/'create-draft-request.json')))
    write_json(out/'created-draft.json', created)
    gh('release', 'upload', tag, '--repo', repo, *map(str, sorted(payload.iterdir())))
    uploaded = api(repo, f"releases/{created['id']}")
    expected = {p.name:p for p in payload.iterdir()}
    require(uploaded.get('draft') is True and uploaded.get('tag_name') == tag
            and uploaded.get('target_commitish') == sha and len(uploaded.get('assets', [])) == 10
            and {a['name'] for a in uploaded['assets']} == set(expected), 'Uploaded native identity mismatch')
    for asset in uploaded['assets']:
        file = expected[asset['name']]
        require(asset.get('state') == 'uploaded' and asset.get('size') == file.stat().st_size
                and asset.get('digest') == 'sha256:'+file_hash(file), 'Uploaded native bytes mismatch')
    require(api(repo, 'git/ref/heads/main').get('object', {}).get('sha') == sha,
            'Main advanced; keep draft unpublished')
    write_json(out/'publish-request.json', dict(draft=False, prerelease=False, make_latest='false'))
    published = json.loads(gh('api', '--method', 'PATCH', f"repos/{repo}/releases/{created['id']}",
                             '--input', str(out/'publish-request.json')))
    write_json(out/'published-release.json', published)
    require(published.get('draft') is False and published.get('prerelease') is False,
            'Native publication outcome uncertain')
    require(api(repo, 'releases/latest').get('id') == stable['id'], 'Windows Latest changed')
    current = api(repo, f"releases/{stable['id']}")
    require(current.get('target_commitish') == sha and current.get('draft') is False
            and {a['id'] for a in current['assets']} == {a['id'] for a in stable['assets']},
            'Windows release changed before companion link')
    write_json(out/'windows-link-request.json', dict(body=current.get('body','')+
        '\n\n<!-- posix-companion -->\n[Linux/macOS 完整程序包]('+published['html_url']+
        ')：独立正式 Tag，内含 Python 和 Chromium，完整包手动更换。\n<!-- /posix-companion -->\n'))
    linked = json.loads(gh('api', '--method', 'PATCH', f"repos/{repo}/releases/{stable['id']}",
                          '--input', str(out/'windows-link-request.json')))
    require(linked.get('id') == stable['id'] and published['html_url'] in linked.get('body',''),
            'Native companion link not confirmed')
    result.update(published=True, url=published['html_url'])
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--run-id', type=int, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--change-notes', type=Path, required=True)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    print(json.dumps(prepare(args.repository, args.run_id, args.out,
        args.change_notes.read_text(encoding='utf-8'), publish=args.publish), ensure_ascii=True))
