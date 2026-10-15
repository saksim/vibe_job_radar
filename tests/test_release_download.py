"""Authored ZIP over real local HTTP; TLS/public routing stay in existing tests."""
import copy
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_release_updates import ReleaseFixture
from vibe_job_radar import release_updates as updates
from vibe_job_radar.release_download import partial_path, download_lock
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.workspace import Workspace, InputError


class ResumeDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.workspace=Workspace(self.root/'workspace')
        self.fixture=ReleaseFixture();self.mode='interrupt';self.requests=[];self.offsets=[]
        self.package=next(a for a in self.fixture.release['assets'] if a['name'].endswith('.zip'))
        self.raw=self.fixture.data[self.package['name']];self.cut=137
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_GET(self):
                owner.requests.append({'path':self.path,'range':self.headers.get('Range')})
                if self.path.startswith('/saksim/'):
                    self.send_response(302);self.send_header('Location','https://release-assets.githubusercontent.com/fixture?fresh=1')
                    self.send_header('Content-Length','0');self.end_headers();return
                offset=int(self.headers.get('Range','bytes=0-')[6:-1]);owner.offsets.append(offset)
                status=206 if offset and not owner.mode.startswith('ignore') else 200
                start=offset if status==206 else 0;body=owner.raw[start:]
                self.send_response(status)
                if status==206:
                    first=start+1 if owner.mode=='bad_range' else start
                    self.send_header('Content-Range',f'bytes {first}-{len(owner.raw)-1}/{len(owner.raw)}')
                if owner.mode=='encoding':self.send_header('Content-Encoding','gzip')
                if owner.mode=='multipart':self.send_header('Content-Type','multipart/byteranges; boundary=x')
                self.send_header('Content-Length',str(len(body)))
                self.end_headers()
                if owner.mode=='interrupt':body=body[:owner.cut]
                if owner.mode=='ignore_interrupt':body=body[:41]
                if owner.mode=='corrupt':body=bytes([body[0]^1])+body[1:]
                self.wfile.write(body);self.wfile.flush();self.close_connection=True
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.worker=threading.Thread(target=self.server.serve_forever,daemon=True);self.worker.start()
        self.addCleanup(self.stop_server)
        port=self.server.server_port
        def connect(host,addresses,timeout,network_policy):
            self.assertIn(host,updates.HOSTS)
            return http.client.HTTPConnection('127.0.0.1',port,timeout=timeout)
        self.patches=[patch.object(updates,'PinnedHTTPSConnection',side_effect=connect),
                      patch.object(updates,'validate_public_url',side_effect=self.resolve)]
        for item in self.patches:item.start();self.addCleanup(item.stop)
        self.transport=updates.ReleaseTransport.__new__(updates.ReleaseTransport);self.transport.policy=NetworkPolicy()
        self.fixture.stream=self.transport.stream

    def resolve(self,url,*args,**kwargs):
        from urllib.parse import urlsplit
        parsed=urlsplit(url)
        return parsed.hostname,['93.184.216.34'],parsed.path+('?' + parsed.query if parsed.query else '')

    def stop_server(self):
        self.server.shutdown();self.server.server_close();self.worker.join(3)

    def service(self):
        service=updates.ReleaseUpdates(self.workspace,transport_factory=lambda:self.fixture,current_version='0.3.0')
        self.addCleanup(service.close);return service

    def wait(self,service):
        service._thread.join(10);self.assertFalse(service._thread.is_alive());return service.state()

    def check(self,service):
        service.check({'consent':True});state=self.wait(service);self.assertEqual(state['status'],'available',state)
        return {'consent':True,'release_id':state['release_id'],'sha256':state['sha256']}

    def interrupt(self):
        service=self.service();data=self.check(service);service.download(data);state=self.wait(service)
        self.assertEqual(state['status'],'failed',state);self.assertEqual(state['retained_bytes'],self.cut)
        path=partial_path(self.workspace.root/'updates',service._selected)
        self.assertEqual(path.read_bytes(),self.raw[:self.cut]);self.assertFalse(list(path.parent.glob('v0.4.0-*')))
        with self.assertRaises(InputError):service.download(data)
        return service,path

    def test_real_disconnect_then_independent_process_resumes_same_official_asset(self):
        old,path=self.interrupt();old.close();self.mode='resume'
        fixture_path=self.root/'fixture.json'
        fixture_path.write_text(json.dumps({'release':self.fixture.release,'data':{k:v.hex() for k,v in self.fixture.data.items()}}),encoding='utf8')
        program='''import http.client,json,sys,threading
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch
from vibe_job_radar import release_updates as u
from vibe_job_radar.network_policy import NetworkPolicy
from vibe_job_radar.workspace import Workspace
value=json.loads(Path(sys.argv[1]).read_text(encoding='utf8'))
class Transport(u.ReleaseTransport):
    def __init__(self):self.policy=NetworkPolicy()
    def latest(self):return value['release']
    def small(self,a,c):return bytes.fromhex(value['data'][a['name']])
def resolve(url,*a,**kw):
    p=urlsplit(url);return p.hostname,['93.184.216.34'],p.path+('?' + p.query if p.query else '')
def connection(*a,**kw):return http.client.HTTPConnection('127.0.0.1',int(sys.argv[3]),timeout=3)
s=u.ReleaseUpdates(Workspace(sys.argv[2]),transport_factory=Transport,current_version='0.3.0')
assert s.state()['status']=='idle'
try:
    with patch.object(u,'validate_public_url',side_effect=resolve),patch.object(u,'PinnedHTTPSConnection',side_effect=connection):
        s.check({'consent':True});s._thread.join(5);state=s.state();assert state['status']=='available',state
        s.download({'consent':True,'release_id':state['release_id'],'sha256':state['sha256']})
        s._thread.join(5);state=s.state();assert state['status']=='ready',state
        print(json.dumps({'status':state['status'],'resumed_bytes':state['resumed_bytes'],'archive':state['archive']}))
finally:s.close()
'''
        environment=dict(os.environ);environment['PYTHONPATH']=str(Path(__file__).resolve().parents[1]/'src')
        result=subprocess.run([sys.executable,'-c',program,str(fixture_path),str(self.workspace.root),str(self.server.server_port)],
                              env=environment,capture_output=True,text=True,timeout=15,check=True)
        state=json.loads(result.stdout);self.assertEqual(state['resumed_bytes'],self.cut)
        self.assertEqual(Path(state['archive']).read_bytes(),self.raw);self.assertFalse(path.exists())
        self.assertEqual(self.offsets,[0,self.cut])
        self.assertEqual(len(self.requests),4);self.assertEqual(self.requests[-2]['range'],f'bytes={self.cut}-')

    def test_server_ignoring_range_replaces_prefix_from_same_response(self):
        service,path=self.interrupt();self.mode='ignore'
        service.download(self.check(service));state=self.wait(service)
        self.assertEqual(state['status'],'ready',state);self.assertEqual(Path(state['archive']).read_bytes(),self.raw)
        self.assertEqual(state['resumed_bytes'],0)
        self.assertEqual(self.offsets,[0,self.cut]);self.assertEqual(len(self.requests),4);self.assertFalse(path.exists())

    def test_bad_range_encoding_multipart_and_corrupt_complete_bytes_never_publish(self):
        for defect in ('bad_range','encoding','multipart','corrupt'):
            with self.subTest(defect=defect):
                self.mode='interrupt';service,path=self.interrupt();self.mode=defect
                service.download(self.check(service));state=self.wait(service)
                self.assertEqual(state['status'],'failed',state);self.assertFalse(path.exists())
                self.assertFalse((path.parent/self.package['name']).exists())

    def test_full_response_interruption_preserves_longer_original_prefix(self):
        service,path=self.interrupt();self.mode='ignore_interrupt'
        service.download(self.check(service));state=self.wait(service)
        self.assertEqual(state['status'],'failed',state);self.assertEqual(state['retained_bytes'],self.cut)
        self.assertEqual(path.read_bytes(),self.raw[:self.cut])
        self.assertEqual(list(path.parent.glob('.full-response-*')),[])
        self.mode='resume';service.download(self.check(service));state=self.wait(service)
        self.assertEqual(state['status'],'ready',state);self.assertEqual(self.offsets,[0,self.cut,self.cut])
        self.assertEqual(Path(state['archive']).read_bytes(),self.raw)

    def test_complete_transfer_error_retains_all_bytes_for_offline_validation(self):
        service=self.service();data=self.check(service);original=self.fixture.stream
        def late_failure(asset,out,stop,progress):
            out.write(self.raw);progress(len(self.raw));raise updates.DownloadInterrupted('deadline after final read')
        self.fixture.stream=late_failure
        service.download(data);state=self.wait(service)
        self.assertEqual(state['status'],'failed',state);self.assertEqual(state['retained_bytes'],len(self.raw))
        self.fixture.stream=original;before=len(self.requests)
        service.download(self.check(service));state=self.wait(service)
        self.assertEqual(state['status'],'ready',state);self.assertEqual(len(self.requests),before)
        self.assertEqual(Path(state['archive']).read_bytes(),self.raw)

    def test_asset_identity_change_never_appends_to_old_prefix(self):
        old,path=self.interrupt();self.fixture.release['id']+=1;self.mode='resume'
        new=self.service();new.download(self.check(new));state=self.wait(new)
        self.assertEqual(state['status'],'ready',state);self.assertEqual(self.offsets,[0,0])
        self.assertEqual(path.read_bytes(),self.raw[:self.cut])

    def test_concurrent_downloader_preserves_foreign_prefix_and_makes_no_request(self):
        old,path=self.interrupt();count=len(self.requests);new=self.service();data=self.check(new)
        with download_lock(path.parent):
            new.download(data);state=self.wait(new)
        self.assertEqual(state['status'],'failed',state);self.assertEqual(len(self.requests),count)
        self.assertEqual(path.read_bytes(),self.raw[:self.cut])

    def test_complete_prefix_is_revalidated_without_transfer_and_hardlink_is_rejected(self):
        service,path=self.interrupt();path.write_bytes(self.raw);count=len(self.requests)
        service.download(self.check(service));state=self.wait(service)
        self.assertEqual(state['status'],'ready',state);self.assertEqual(len(self.requests),count)
        # A new identity cannot use another file's hardlink as writable prefix.
        self.fixture.release['id']+=1;new=self.service();data=self.check(new)
        package=Path(state['archive']);package.unlink()
        foreign=self.root/'foreign';foreign.write_bytes(b'original')
        target=partial_path(path.parent,new._selected);os.link(foreign,target)
        new.download(data);state=self.wait(new)
        self.assertEqual(state['status'],'failed',state);self.assertEqual(foreign.read_bytes(),b'original')
        self.assertEqual(target.read_bytes(),b'original');self.assertEqual(len(self.requests),count)

    def test_dns_failure_and_deadline_keep_prefix_for_an_explicit_later_attempt(self):
        service,path=self.interrupt();before=path.read_bytes()
        for error in (updates.FetchError('encrypted_dns_unavailable'),updates.DownloadInterrupted('bounded deadline')):
            with self.subTest(error=type(error).__name__):
                data=self.check(service)
                with patch.object(self.fixture,'stream',side_effect=error):
                    service.download(data);state=self.wait(service)
                self.assertEqual(state['status'],'failed',state)
                self.assertEqual(state['retained_bytes'],self.cut);self.assertEqual(path.read_bytes(),before)
        self.mode='resume';service.download(self.check(service));self.assertEqual(self.wait(service)['status'],'ready')

    def test_stop_keeps_prefix_and_full_digest_still_rejects_a_tampered_prefix(self):
        service,path=self.interrupt();path.write_bytes(b'X'*self.cut);self.mode='resume'
        service.download(self.check(service));state=self.wait(service)
        self.assertEqual(state['status'],'failed',state);self.assertFalse(path.exists())
        service2=self.service();data=self.check(service2)
        def stop_after_prefix(asset,out,stop,progress):
            out.write(self.raw[:self.cut]);stop.set();updates.check_stop(stop)
        self.fixture.stream=stop_after_prefix
        service2.download(data);state=self.wait(service2)
        self.assertEqual(state['status'],'failed',state);self.assertEqual(state['retained_bytes'],self.cut)
        self.assertEqual(partial_path(path.parent,service2._selected).read_bytes(),self.raw[:self.cut])


if __name__=='__main__':unittest.main()
