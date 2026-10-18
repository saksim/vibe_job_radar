"""Exercise the extracted POSIX executable, with no Python on its PATH."""
from __future__ import annotations
import argparse
import http.client
import json
import os
from pathlib import Path
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))

from posix_artifacts import inventory, require
from portable_blank_protocol import bounded_lines
from build_windows_portable import native_component_valid
from ntlm_proxy_acceptance import verify_component as verify_ntlm
from url_pac_acceptance import verify_app as verify_url_pac
from domain_pac_acceptance import verify_app as verify_domain_pac


class RunningApp:
    def __init__(self, executable, workspace, cwd, env, *, pass_fds=()):
        command = executable if isinstance(executable,list) else [str(executable)]
        self.process = subprocess.Popen(command + ['--workspace', str(workspace), '--no-browser'],
            cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, start_new_session=True, pass_fds=pass_fds)
        self.entries = queue.Queue(maxsize=1)
        self.diagnostics = []
        def read():
            for raw in bounded_lines(self.process.stdout):
                if raw is None:
                    continue
                match = re.search(rb'http://127\.0\.0\.1:\d+/#token=([A-Za-z0-9_-]+)', raw)
                safe = re.sub(rb'#token=[A-Za-z0-9_-]+',b'#token=<redacted>',raw[:1000]).decode('utf-8','replace').strip()
                self.diagnostics = (self.diagnostics + [safe])[-40:]
                if match:
                    try:
                        self.entries.put_nowait(match.group().decode('ascii'))
                    except queue.Full:
                        pass
            try:self.entries.put_nowait(None)
            except queue.Full:pass
        self.reader = threading.Thread(target=read, daemon=True)
        self.reader.start()
        try:
            entry = urlsplit(self.entries.get(timeout=30))
            self.port, self.token = entry.port, entry.fragment.removeprefix('token=')
            self.origin = 'http://127.0.0.1:' + str(self.port)
        except Exception:
            self.close()
            raise RuntimeError('Packaged local server did not become ready; exit='+str(self.process.returncode)+'; output='+' | '.join(self.diagnostics)) from None

    def call(self, path, data=None, *, expected_status=200):
        connection = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        headers = {'Origin': self.origin, 'X-Radar-Token': self.token}
        body = None
        if data is not None:
            body = json.dumps(data).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        try:
            connection.request('POST' if data is not None else 'GET', path, body, headers)
            response = connection.getresponse()
            raw = response.read(8_000_001)
            require(response.status == expected_status and len(raw) <= 8_000_000, 'Packaged API failed: ' + path + '; status=' + str(response.status) + '; body=' + raw[:2048].decode('utf-8','replace'))
            return json.loads(raw) if path.startswith('/api/') else raw
        finally:
            connection.close()

    json = call

    def close(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(5)
                raise RuntimeError('Packaged server did not close within its deadline')
        self.reader.join(5)
        self.process.stdout.close()
        require(not self.reader.is_alive(), 'Packaged output reader did not close')


def verify(bundle, output, archive):
    bundle = Path(bundle).resolve()
    executable = bundle/'VibeJobRadar'
    require(executable.is_file() and os.access(executable, os.X_OK), 'Executable missing or not executable')
    metadata = json.loads((bundle/'PORTABLE.json').read_text(encoding='utf-8'))
    before = inventory(bundle)
    result = dict(success=False, platform=metadata['platform'], version=metadata['version'],
                  source_revision=metadata['runtime_source']['revision'], source_sha256=metadata['source']['sha256'],
                  checks=[], python_required=False, browser_bundled=True, live_sites_certified=False,
                  clean_machine_certified=False, signed=False, notarized=False)
    try:
        with tempfile.TemporaryDirectory(prefix='posix-portable-') as temporary:
            root = Path(temporary)
            cwd = root/'separate cwd';cwd.mkdir()
            workspace = root/'用户工作区 with spaces'
            empty_path = root/'no-python-path';empty_path.mkdir()
            env = {k:v for k,v in os.environ.items() if k not in {
                'PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','CONDA_PREFIX','PLAYWRIGHT_BROWSERS_PATH'}
                and not k.startswith('VIBE_RADAR_')}
            env.update(PATH=str(empty_path), PYTHONUTF8='1')
            doctor = subprocess.run([str(executable),'--doctor','--workspace',str(workspace)],
                cwd=cwd, env=env, capture_output=True, timeout=30)
            require(doctor.returncode == 0 and json.loads(doctor.stdout)['workspace_writable'], 'Packaged doctor failed')
            result['checks'].append('isolated_path_unicode_workspace_and_doctor')
            native = subprocess.run([str(executable),'--native-browser-check','--native-browser-channel','bundled'],
                cwd=cwd, env=env, capture_output=True, timeout=60)
            require(len(native.stdout) <= 65536, 'Oversized browser evidence')
            proof = json.loads(native.stdout)
            proof['returncode'] = native.returncode
            result['native_component'] = proof
            require(native_component_valid(proof,'bundled'), 'Packaged Chromium proof incomplete')
            result['checks'].append('frozen_chromium_blank_guard_and_cleanup')
            result['ntlm_component'] = verify_ntlm(executable,cwd,env)
            result['checks'].append('frozen_ntlmv2_protocol_worker_and_cleanup')
            from os_credentials_acceptance import verify as verify_credentials
            result['system_credentials'] = verify_credentials([str(executable)],cwd,env,frozen=True)
            result['source_system_credentials'] = json.loads((Path(output).parent/'source-system-credentials.json').read_text(encoding='utf-8'))
            result['checks'].append('native_os_credentials_source_and_frozen')
            app = RunningApp(executable,workspace,cwd,env)
            try:
                for route in ('/','/advanced','/guided','/quality','/app.js','/advanced.js','/guided.js',
                    '/quality.js','/report-sources.js','/collection-help.js','/network-settings.js',
                    '/public-schedule.js','/public-history.js','/team.js','/release-updates.js',
                    '/public-queue.js','/windows-startup.js'):
                    require(bool(app.call(route)), 'Missing bundled resource')
                state = app.json('/api/guided/state')
                environment = state['runtime_environment']
                require(state['runtime']['kind']=='portable' and Path(state['python']).resolve()==executable
                        and environment['source']==metadata['runtime_source']
                        and environment['freshness']=='startup_only', 'Packaged runtime source identity mismatch')
                result['runtime_environment'] = environment
                result['checks'].append('resources_and_frozen_source_identity')
                result['url_pac'] = verify_url_pac(app,frozen_executable=True)
                result['domain_pac'] = verify_domain_pac(app,frozen_executable=True)
                result['checks'].append('frozen_url_and_domain_pac_workers')
                captured = app.json('/api/job',dict(title='时间序列算法工程师',company='ARTIFICIAL POSIX FIXTURE',
                    text='负责时间序列预测。要求熟练使用 Cursor 进行 AI 辅助编程，必须编写单元测试并进行代码审查。',
                    platform='boss',url='https://www.zhipin.com/job_detail/posix-fixture.html',
                    rights_note='Locally authored verification only, not market data',
                    evidence_level='full_text',full_text_confirmed=True))
                require(captured['new_snapshots']==1,'Artificial JD import failed')
                report = app.json('/api/analyze',{'roles':['time_series']})
                require(report['manifest']['stats']['full_text_job_groups']==1 and report['requirements']
                        and 'requirements_zh.csv' in report['files'], 'Original report incomplete')
                result['checks'].append('authored_full_jd_and_original_report')
            finally:
                app.close()
            reopened = RunningApp(executable,workspace,cwd,env)
            try:
                saved = reopened.json('/api/report/'+report['id'])
                require(saved['requirements']==report['requirements']
                        and reopened.json('/api/status')['counts']['records']==1, 'Independent reopen lost data')
                state = reopened.json('/api/guided/state')['runtime_environment']
                require(state['source']==environment['source'] and state['process_id']!=environment['process_id'],
                        'Independent process identity mismatch')
                result['checks'].append('independent_process_report_and_runtime_reopen')
            finally:
                reopened.close()
            from posix_update_acceptance import verify as verify_update
            result['automatic_update']=verify_update(bundle,archive,root/'native upgrade',cwd,env)
            result['checks'].append('native_automatic_update_handoff')
            require(inventory(bundle)==before,'Program bundle changed while running')
            result['checks'].append('immutable_extracted_payload')
            result['success'] = True
    except Exception as error:
        result['error_type'] = type(error).__name__
        raise
    finally:
        Path(output).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--archive',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(verify(args.bundle,args.out,args.archive),ensure_ascii=True))
