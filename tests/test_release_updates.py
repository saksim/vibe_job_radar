"""Authored bytes only: no test executes a downloaded program."""
import copy
import hashlib
import io
import json
import os
import stat
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from vibe_job_radar import release_updates as updates
from vibe_job_radar.network import Response
from vibe_job_radar.workspace import Workspace, InputError


class ReleaseFixture:
    def __init__(self, *, entries=(), embedded_version='0.4.0'):
        output=io.BytesIO()
        with zipfile.ZipFile(output,'w',compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('VibeJobRadar/VibeJobRadar.exe',b'AUTHORED TEST DATA, NEVER EXECUTE')
            archive.writestr('VibeJobRadar/PORTABLE.json',json.dumps({'version':embedded_version,'platform':'windows-x64',
                'source':{'sha256':'b'*64},'runtime_source':{'revision':'a'*40}}))
            for name,value in entries:archive.writestr(name,value)
        package=output.getvalue();sha=updates.digest(package)
        filename='vibe-job-radar-0.4.0-'+sha[:16]+'-windows-x64.zip'
        self.data={filename:package,'CHANGES.md':'## 解决了什么\n- 人工测试\n## 提高了什么\n- 人工测试\n## 验证结果\n- 非现场认证\n## 剩余问题\n- 未签名\n'.encode(),
                   'UPGRADE_AND_LIMITATIONS.md':'先停止旧程序并备份工作区。'.encode()}
        self.data['manifest.json']=json.dumps({'file':filename,'sha256':sha,'size_bytes':len(package),
            'source_sha256':'b'*64,'runtime_verified':True}).encode()
        self.data['provenance.json']=json.dumps({'repository':updates.REPOSITORY,'tag':'v0.4.0','release_channel':'stable',
            'signed':False,'rebuilt_for_release':False,'candidate_sha256':sha,'source_sha256':'b'*64,
            'change_notes_sha256':updates.digest(self.data['CHANGES.md']),'source_revision':'a'*40,
            'ci':{path:{'id':i+1,'attempt':1} for i,path in enumerate(updates.WORKFLOWS)}}).encode()
        self.data['portable-verification.json']=b'{"success":true}'
        self.data['source-verification.json']=json.dumps({'success':True,'source_unchanged':True,'source':{'sha256':'b'*64}}).encode()
        self.data['SHA256SUMS.txt']=''.join(updates.digest(data)+'  '+name+'\n' for name,data in sorted(self.data.items())).encode()
        self.release={'id':42,'tag_name':'v0.4.0','draft':False,'prerelease':False,
                      'html_url':updates.WEB+'/releases/tag/v0.4.0',
                      'assets':[{'id':i+1,'name':name,'size':len(data),'digest':'sha256:'+updates.digest(data),'state':'uploaded',
                          'browser_download_url':updates.WEB+'/releases/download/v0.4.0/'+name}
                                for i,(name,data) in enumerate(sorted(self.data.items()))]}
        self.calls=[]

    def latest(self):self.calls.append('latest');return copy.deepcopy(self.release)
    def small(self,asset,cancelled):
        updates.check_stop(cancelled);self.calls.append(asset['name']);return self.data[asset['name']]
    def stream(self,asset,destination,cancelled,progress):
        updates.check_stop(cancelled);self.calls.append(asset['name'])
        destination.write(self.data[asset['name']]);progress(len(self.data[asset['name']]))


class ReleaseUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.workspace=Workspace(self.root/'workspace')
        self.fixture=ReleaseFixture()

    def service(self,fixture=None,version='0.3.0'):
        service=updates.ReleaseUpdates(self.workspace,transport_factory=lambda:fixture or self.fixture,current_version=version)
        self.addCleanup(service.close);return service

    def wait(self,service):
        service._thread.join(10);self.assertFalse(service._thread.is_alive())
        return service.state()

    def test_explicit_check_and_verified_side_by_side_upgrade_preserve_data(self):
        keep=self.workspace.root/'keep.txt';keep.write_text('private original',encoding='utf-8')
        service=self.service();self.assertEqual(service.state()['status'],'idle');self.assertEqual(self.fixture.calls,[])
        for data in ({},{'consent':1},{'consent':False},{'consent':True,'url':'https://other.test'}):
            with self.assertRaises(InputError):service.check(data)
        self.assertEqual(self.fixture.calls,[])
        service.check({'consent':True});state=self.wait(service)
        self.assertEqual(state['status'],'available',state)
        with self.assertRaises(InputError):service.download({'consent':True,'release_id':41,'sha256':state['sha256']})
        service.download({'consent':True,'release_id':42,'sha256':state['sha256']});state=self.wait(service)
        self.assertEqual(state['status'],'ready',state)
        directory=Path(state['directory'])
        self.assertEqual((directory/'VibeJobRadar.exe').read_bytes(),b'AUTHORED TEST DATA, NEVER EXECUTE')
        self.assertTrue((directory.parent/'READY.json').is_file())
        self.assertEqual(keep.read_text(encoding='utf-8'),'private original')
        self.assertEqual(updates.file_digest(Path(state['archive'])),state['sha256'])

    def test_current_or_newer_runtime_cannot_download_a_downgrade(self):
        for version in ('0.4.0','0.5.0'):
            service=self.service(version=version);service.check({'consent':True});state=self.wait(service)
            self.assertEqual(state['status'],'current')
            with self.assertRaises(InputError):
                service.download({'consent':True,'release_id':42,'sha256':state['sha256']})

    def test_foreign_prerelease_and_inconsistent_metadata_are_rejected(self):
        for defect in ('foreign','prerelease','duplicate','digest','checksum','source'):
            fixture=ReleaseFixture()
            if defect=='foreign':fixture.release['assets'][0]['browser_download_url']='https://example.invalid/payload'
            elif defect=='prerelease':fixture.release['prerelease']=True
            elif defect=='duplicate':fixture.release['assets'][0]=fixture.release['assets'][1]
            elif defect=='digest':fixture.release['assets'][0]['digest']='sha256:'+'f'*64
            elif defect=='checksum':fixture.data['SHA256SUMS.txt']+=b'extra invalid line'
            else:fixture.data['source-verification.json']=b'{"success":false}'
            with self.subTest(defect=defect),self.assertRaises(updates.UpdateError):
                updates.inspect_release(fixture.latest(),fixture,threading.Event())
        self.assertFalse((self.workspace.root/'updates').exists())

    def test_bad_download_hash_and_existing_file_are_not_published_or_overwritten(self):
        service=self.service();service.check({'consent':True});state=self.wait(service)
        with patch.object(self.fixture,'stream',side_effect=lambda asset,out,stop,progress:out.write(b'corrupt')):
            service.download({'consent':True,'release_id':42,'sha256':state['sha256']});result=self.wait(service)
        self.assertEqual(result['status'],'failed')
        folder=self.workspace.root/'updates';self.assertEqual(list(folder.iterdir()),[])
        package=folder/service._selected['package']['name'];package.write_bytes(b'operator original')
        service.check({'consent':True});state=self.wait(service)
        service.download({'consent':True,'release_id':42,'sha256':state['sha256']});result=self.wait(service)
        self.assertEqual(result['status'],'failed');self.assertEqual(package.read_bytes(),b'operator original')

    def test_unsafe_archives_and_wrong_embedded_version_leave_no_staged_program(self):
        link=zipfile.ZipInfo('VibeJobRadar/link');link.create_system=3;link.external_attr=(stat.S_IFLNK|0o777)<<16
        defects=[('../escape',b'x'),('VibeJobRadar/../../escape',b'x'),
                 ('VibeJobRadar\\..\\escape',b'x'),('VibeJobRadar/CON.txt',b'x'),
                 ('VibeJobRadar/vibejobradar.exe',b'duplicate Windows name'),(link,b'outside')]
        folder=self.workspace.root/'updates';folder.mkdir()
        for entry in defects:
            fixture=ReleaseFixture(entries=[entry]);info=updates.inspect_release(fixture.latest(),fixture,threading.Event())
            package=folder/info['package']['name'];package.write_bytes(fixture.data[package.name])
            with self.subTest(entry=str(entry[0])),self.assertRaises(updates.UpdateError):
                updates.stage_package(package,info,folder,threading.Event())
            self.assertFalse([p for p in folder.iterdir() if p.is_dir()])
        fixture=ReleaseFixture(embedded_version='0.3.0')
        info=updates.inspect_release(fixture.latest(),fixture,threading.Event());package=folder/info['package']['name']
        package.write_bytes(fixture.data[package.name])
        with self.assertRaises(updates.UpdateError):updates.stage_package(package,info,folder,threading.Event())
        self.assertFalse((self.workspace.root/'escape').exists())

    def test_expansion_limit_and_cancel_keep_existing_workspace(self):
        fixture=self.fixture;info=updates.inspect_release(fixture.latest(),fixture,threading.Event())
        folder=self.workspace.root/'updates';folder.mkdir();package=folder/info['package']['name'];package.write_bytes(fixture.data[package.name])
        with patch.object(updates,'MAX_EXPANDED',4),self.assertRaises(updates.UpdateError):
            updates.stage_package(package,info,folder,threading.Event())
        stopped=threading.Event();stopped.set()
        with self.assertRaises(updates.UpdateError):updates.stage_package(package,info,folder,stopped)
        self.assertEqual(list(folder.iterdir()),[package])

    def test_deep_windows_package_members_prepare_without_system_setting_changes(self):
        name='VibeJobRadar/'+ '/'.join('reference_'+str(i)+'_'+'x'*60 for i in range(3))+'/details.json'
        fixture=ReleaseFixture(entries=[(name,b'long packaged reference')])
        service=self.service(fixture);service.check({'consent':True});state=self.wait(service)
        keep=self.workspace.root/'keep.txt';keep.write_bytes(b'original workspace')
        service.download({'consent':True,'release_id':42,'sha256':state['sha256']});state=self.wait(service)
        self.assertEqual(state['status'],'ready',state)
        directory=Path(state['directory']);created=directory.parent
        identity=(created.stat().st_dev,created.stat().st_ino)
        try:
            logical=created.joinpath(*name.split('/'))
            self.assertGreater(len(str(logical)),260)
            readable='\\\\?\\'+str(logical.resolve()) if os.name=='nt' else logical
            with open(readable,'rb') as saved:self.assertEqual(saved.read(),b'long packaged reference')
            self.assertTrue((created/'READY.json').is_file())
            self.assertEqual(keep.read_bytes(),b'original workspace')
        finally:
            updates.remove_owned_tree(created,identity,self.workspace.root/'updates')
        self.assertFalse(created.exists())

    def test_failure_after_long_member_removes_only_owned_staging_tree(self):
        name='VibeJobRadar/'+ '/'.join('reference_'+str(i)+'_'+'x'*60 for i in range(3))+'/details.json'
        fixture=ReleaseFixture(entries=[(name,b'long packaged reference'),('VibeJobRadar/last.txt',b'last')])
        service=self.service(fixture);service.check({'consent':True});state=self.wait(service)
        keep=self.workspace.root/'keep.txt';keep.write_bytes(b'original workspace')
        original=zipfile.ZipFile.open;completed=[];injected=[]
        def fail_after_long_file(archive,member,*args,**kwargs):
            if getattr(member,'filename',None)=='VibeJobRadar/last.txt':
                self.assertIn(name,completed);injected.append(True)
                raise OSError('authored disk failure after long member')
            value=original(archive,member,*args,**kwargs)
            if getattr(member,'filename',None)==name:completed.append(name)
            return value
        with patch.object(zipfile.ZipFile,'open',fail_after_long_file):
            service.download({'consent':True,'release_id':42,'sha256':state['sha256']});state=self.wait(service)
        self.assertEqual(state['status'],'failed')
        self.assertEqual(injected,[True])
        folder=self.workspace.root/'updates'
        self.assertEqual([p.name for p in folder.iterdir()],[service._selected['package']['name']])
        self.assertEqual(keep.read_bytes(),b'original workspace')
        self.assertEqual(updates.file_digest(folder/service._selected['package']['name']),service._selected['sha256'])

    def test_redirect_cannot_leave_fixed_github_hosts(self):
        transport=updates.ReleaseTransport.__new__(updates.ReleaseTransport)
        transport.client=Mock();transport.client.public_get.return_value=Response(302,{'location':'https://elsewhere.invalid/tool.zip'},b'',updates.WEB)
        asset=self.fixture.release['assets'][0]
        with self.assertRaises(updates.UpdateError):transport.small(asset,threading.Event())
        self.assertEqual(transport.client.public_get.call_count,1)
        for url in ('http://github.com/tool','https://github.com.evil.invalid/tool','https://user@github.com/tool','https://127.0.0.1/tool'):
            with self.assertRaises(updates.UpdateError):updates.safe_url(url)


if __name__=='__main__':unittest.main()
