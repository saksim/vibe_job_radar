"""Platform release and handoff boundaries using authored, inert archives."""
import copy
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

from vibe_job_radar import release_posix as native, release_updates as updates
from vibe_job_radar import release_install as install, release_platform as platforms
from vibe_job_radar import posix_artifacts
from vibe_job_radar.workspace import Workspace


class NativeReleaseFixture:
    def __init__(self):
        self.data={};self.manifests={};self.proofs={};self.calls=[]
        for platform in platforms.POSIX_PLATFORMS:
            executable=b'INERT AUTHOR DATA, DO NOT EXECUTE'
            stamp=dict(revision='a'*40,raw_sha256='b'*64,executable_sha256=updates.digest(executable))
            metadata=dict(version='0.13.0',platform=platform,source=dict(sha256='b'*64),
                runtime_source=stamp,automatic_update=True,upgrade_protocol=2)
            stream=io.BytesIO()
            with tarfile.open(fileobj=stream,mode='w:gz') as archive:
                row=tarfile.TarInfo('VibeJobRadar');row.type=tarfile.DIRTYPE;row.mode=0o755;archive.addfile(row)
                for name,data in [('VibeJobRadar',executable),('PORTABLE.json',json.dumps(metadata).encode())]:
                    row=tarfile.TarInfo('VibeJobRadar/'+name);row.size=len(data);row.mode=0o755 if name=='VibeJobRadar' else 0o600
                    archive.addfile(row,io.BytesIO(data))
            package=stream.getvalue();sha=updates.digest(package)
            name='vibe-job-radar-0.13.0-'+sha[:16]+'-'+platform+'.tar.gz';self.data[name]=package
            rows=posix_artifacts.archive_inventory(io.BytesIO(package))
            common=dict(version='0.13.0',platform=platform,source_revision='a'*40,source_sha256='b'*64,
                success=True,python_required=False,browser_bundled=True,signed=False,notarized=False)
            proof=dict(**common,checks=list(native.NATIVE_CHECKS),runtime_environment=dict(source=stamp),
                automatic_update=dict(schema_version=1,**{k:True for k in native.HANDOFF_FIELDS}))
            self.proofs[platform]=proof
            self.manifests[platform]=dict(**common,status='posix_portable_candidate',file=name,sha256=sha,
                size_bytes=len(package),runtime_checks=len(native.NATIVE_CHECKS),files=rows,automatic_update=True)
        paths=updates.WORKFLOWS|{'.github/workflows/'+n for n in ('python-distribution.yml','live-public-example.yml','posix-portable.yml')}
        main_ci={p:dict(id=i+1,attempt=1) for i,p in enumerate(sorted(paths))}
        self.data['CI_EVIDENCE.json']=json.dumps(dict(repository=updates.REPOSITORY,source_revision='a'*40,
            rebuilt_for_release=False,main_ci=main_ci,native_run=main_ci['.github/workflows/posix-portable.yml'])).encode()
        self.data['CHANGES.md']='\n'.join('## '+h for h in ('解决了什么','提高了什么','验证结果','剩余问题')).encode()
        self.data['INSTALL_AND_LIMITATIONS.md']=b'Authored test notes'
        self.latest_value=dict(id=100,tag_name='v0.13.0',draft=False,prerelease=False,
            html_url=updates.WEB+'/releases/tag/v0.13.0',target_commitish='a'*40)
        self.refresh()

    def refresh(self):
        for platform in platforms.POSIX_PLATFORMS:
            raw=json.dumps(self.proofs[platform]).encode();self.data[platform+'-verification.json']=raw
            self.manifests[platform]['runtime_evidence_sha256']=updates.digest(raw)
            self.data[platform+'-manifest.json']=json.dumps(self.manifests[platform]).encode()
        self.data['SHA256SUMS.txt']=''.join(updates.digest(data)+'  '+name+'\n'
            for name,data in sorted(self.data.items()) if name!='SHA256SUMS.txt').encode()
        tag='desktop-v0.13.0'
        self.release_value=dict(id=101,tag_name=tag,draft=False,prerelease=False,target_commitish='a'*40,
            html_url=updates.WEB+'/releases/tag/'+tag,assets=[dict(id=i+1,name=name,size=len(data),
                digest='sha256:'+updates.digest(data),state='uploaded',
                browser_download_url=updates.WEB+'/releases/download/'+tag+'/'+name)
                for i,(name,data) in enumerate(sorted(self.data.items()))])

    def latest(self):self.calls.append('latest');return copy.deepcopy(self.latest_value)
    def release(self,tag):self.calls.append(tag);return copy.deepcopy(self.release_value)
    def small(self,asset,cancelled):self.calls.append(asset['name']);return self.data[asset['name']]
    def stream(self,asset,destination,cancelled,progress):
        self.calls.append(asset['name']);destination.write(self.data[asset['name']]);progress(asset['size'])


class NativeUpdateTests(unittest.TestCase):
    def setUp(self):
        self.fixture=NativeReleaseFixture();self.stop=threading.Event()

    def inspect(self,platform='linux-x64'):
        return native.inspect_release(self.fixture.latest(),self.fixture,self.stop,platform)

    def test_only_current_platform_package_and_evidence_are_selected(self):
        for platform in platforms.POSIX_PLATFORMS:
            with self.subTest(platform=platform):
                self.fixture.calls=[];selected=self.inspect(platform)
                self.assertEqual(selected['platform'],platform)
                self.assertTrue(selected['package']['name'].endswith(platform+'.tar.gz'))
                self.assertTrue(selected['automatic_update'])
                other=next(p for p in platforms.POSIX_PLATFORMS if p!=platform)
                self.assertNotIn(other+'-manifest.json',self.fixture.calls)
                self.assertFalse(any(n.endswith('.tar.gz') for n in self.fixture.calls))
        with patch.object(platforms.sys,'platform','darwin'),patch.object(platforms.platform,'machine',return_value='x86_64'):
            self.assertIsNone(platforms.current_platform())

    def test_companion_identity_and_complete_handoff_proof_cannot_drift(self):
        for field,value in [('target_commitish','c'*40),('draft',True),('tag_name','desktop-v0.14.0')]:
            with self.subTest(field=field):
                self.fixture=NativeReleaseFixture();self.fixture.release_value[field]=value
                with self.assertRaises(updates.UpdateError):self.inspect()
        for flag in native.HANDOFF_FIELDS:
            with self.subTest(flag=flag):
                self.fixture=NativeReleaseFixture();self.fixture.proofs['linux-x64']['automatic_update'][flag]=False
                self.fixture.refresh()
                with self.assertRaises(updates.UpdateError):self.inspect()

    def test_embedded_platform_source_and_full_digest_checked_before_extraction(self):
        selected=self.inspect();data=self.fixture.data[selected['package']['name']]
        for changes in ({'platform':'macos-arm64'},{'source_sha256':'f'*64},{'sha256':'f'*64}):
            with self.subTest(changes=changes),self.assertRaises(updates.UpdateError):
                native.checked_archive(io.BytesIO(data),{**selected,**changes},self.stop)
        self.assertEqual(native.checked_archive(io.BytesIO(data),selected,self.stop),selected['files'])
        self.stop.set()
        with self.assertRaises(updates.UpdateError):native.checked_archive(io.BytesIO(data),selected,self.stop)

    def test_api_selects_native_release_and_retains_original_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace=Workspace(Path(tmp)/'original');keep=workspace.root/'keep.txt';keep.write_bytes(b'KEEP')
            service=updates.ReleaseUpdates(workspace,transport_factory=lambda:self.fixture,
                current_version='0.12.0',target_platform='linux-x64')
            try:
                service.check({'consent':True});service._thread.join(10)
                state=service.state();self.assertEqual(state['status'],'available',state)
                self.assertEqual(state['install_consent_version'],install.POSIX_CONSENT)
                self.assertIn('desktop-v0.13.0',state['release_url'])
                # Windows cannot reproduce Unix modes; native CI executes this
                # same preparation plus the actual frozen-process handoff.
                if os.name!='nt':
                    service.download(dict(consent=True,release_id=state['release_id'],sha256=state['sha256']))
                    service._thread.join(10);state=service.state();self.assertEqual(state['status'],'ready',state)
                    directory=Path(state['directory']);self.assertTrue(os.access(directory/'VibeJobRadar',os.X_OK))
                    install.verify_directory(directory,Path(state['archive']),service._selected)
                    (directory/'VibeJobRadar').write_bytes(b'CHANGED')
                    with self.assertRaises(updates.UpdateError):install.verify_directory(directory,Path(state['archive']),service._selected)
                self.assertEqual(keep.read_bytes(),b'KEEP')
            finally:service.close();workspace.close_network()

    def test_pipe_waits_for_actual_writer_close_and_rejects_non_pipe(self):
        if os.name=='nt':
            # Native pipe polling is exercised on both native source jobs.
            with tempfile.TemporaryFile() as stream,self.assertRaises(updates.UpdateError):
                install.ExitPipe(stream.fileno(),os.fstat(stream.fileno()).st_ino)
            return
        reader,writer=os.pipe();parent=install.ExitPipe(reader,os.fstat(reader).st_ino)
        try:
            self.assertFalse(parent.exited(0));os.close(writer);writer=None
            self.assertTrue(parent.exited(100))
        finally:
            parent.close()
            if writer is not None:os.close(writer)

    def test_linux_child_environment_restores_system_library_path(self):
        with patch.object(install.sys,'platform','linux'),patch.dict(install.os.environ,
                {'LD_LIBRARY_PATH':'/old/private','LD_LIBRARY_PATH_ORIG':'/system/original'},clear=True):
            env=install.launch_environment()
        self.assertEqual(env['LD_LIBRARY_PATH'],'/system/original')
        self.assertNotIn('LD_LIBRARY_PATH_ORIG',env)
        self.assertEqual(env['PYINSTALLER_RESET_ENVIRONMENT'],'1')
