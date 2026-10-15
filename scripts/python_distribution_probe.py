"""Exercise an installed wheel from outside the checkout, using artificial data only."""
import hashlib
import http.client
import importlib.metadata
import json
import os
from pathlib import Path
import socket
import sys
import threading
from unittest.mock import patch


def main():
    config=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    import vibe_job_radar
    from vibe_job_radar import __version__
    from vibe_job_radar.workbench import LocalServer
    from vibe_job_radar.workspace import Workspace
    package=Path(vibe_job_radar.__file__).resolve().parent
    prefix=Path(sys.prefix).resolve()
    assert package.is_relative_to(prefix), 'source checkout shadowed the installed wheel'
    assert __version__==config['version']==importlib.metadata.version('vibe-job-radar')
    for name,digest in config['package_files'].items():
        file=package.parent/name
        assert hashlib.sha256(file.read_bytes()).hexdigest()==digest,name
    root=Path(config['workspace'])
    prior=root/'installation-proof.json'
    original_connect=socket.socket.connect
    def local_only(sock, address):
        if not isinstance(address,tuple) or address[0]!='127.0.0.1':
            raise AssertionError('unexpected external connection')
        return original_connect(sock,address)
    with patch.object(socket.socket,'connect',local_only):
        workspace=Workspace(root)
        server=LocalServer(workspace,public_client=None)
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        thread.start()
        def request(path,data=None,authorized=True):
            conn=http.client.HTTPConnection(server.authority,timeout=15)
            headers={'Origin':server.origin}
            if authorized:headers['X-Radar-Token']=server.token
            body=None
            if data is not None:body=json.dumps(data).encode();headers['Content-Type']='application/json'
            try:
                conn.request('POST' if data is not None else 'GET',path,body,headers)
                reply=conn.getresponse();raw=reply.read()
                assert reply.status==200,(path,reply.status)
                return json.loads(raw) if path.startswith('/api/') else raw
            finally:conn.close()
        try:
            for route in ['/', '/advanced','/guided','/quality','/app.js','/advanced.js','/guided.js',
                          '/quality.js','/report-sources.js','/collection-help.js','/network-settings.js',
                          '/public-schedule.js','/public-history.js','/team.js','/release-updates.js',
                          '/public-queue.js','/windows-startup.js']:
                assert request(route,authorized=False),route
            assert request('/api/network/state')['schema_version']>=1
            if not prior.exists():
                captured=request('/api/job',{'title':'时间序列算法工程师','company':'ARTIFICIAL INSTALLATION FIXTURE',
                    'text':'负责时间序列预测。要求熟练使用 Cursor 进行 AI 辅助编程，必须编写单元测试并进行代码审查。',
                    'platform':'boss','url':'https://www.zhipin.com/job_detail/installation-fixture.html',
                    'rights_note':'Locally authored test fixture only, not market data',
                    'evidence_level':'full_text','full_text_confirmed':True})
                assert captured['new_snapshots']==1
                report=request('/api/analyze',{'roles':['time_series']})
                assert report['manifest']['stats']['full_text_job_groups']==1
                assert report['requirements'] and 'requirements_zh.csv' in report['files']
                prior.write_text(json.dumps({'id':report['id'],'requirements':report['requirements']}),encoding='utf-8')
                stage='import_and_original_report'
            else:
                saved=json.loads(prior.read_text(encoding='utf-8'))
                report=request('/api/report/'+saved['id'])
                assert report['requirements']==saved['requirements']
                assert workspace.status()['counts']['records']==1
                stage='independent_process_reopen'
        finally:
            server.shutdown();server.server_close();thread.join(5)
            assert not thread.is_alive()
    print(json.dumps({'success':True,'stage':stage,'version':__version__,'platform':sys.platform,
                      'installed_payload_verified':True,'external_requests':0}))


if __name__=='__main__':main()
