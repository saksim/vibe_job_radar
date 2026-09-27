"""Old source -> candidate -> old source, on an artificial temporary workspace.

No user workspace or provider is used. Each phase runs the chosen source in a
fresh process. This verifies formats and preservation, not live native login.
"""
from __future__ import annotations
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def phase(source, workspace_path, mode):
    sys.path.insert(0, str(source/'src'))
    from vibe_job_radar.workspace import Workspace
    from vibe_job_radar.evidence_ui import EvidenceService
    from vibe_job_radar.guided.adapters import builtins
    from vibe_job_radar.guided.service import GuidedService
    from vibe_job_radar.guided.saved_session import SavedSession
    from vibe_job_radar.network import SafeHTTP
    import vibe_job_radar
    assert Path(vibe_job_radar.__file__).resolve().parent == source/'src/vibe_job_radar'
    state_path = workspace_path/'compatibility-fixture.json'
    def stable_files():
        return {p.relative_to(workspace_path).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
                for p in workspace_path.rglob('*') if p.is_file()
                and p.suffix not in {'.sqlite','.lock'} and not p.name.endswith(('-wal','-shm'))
                and p != state_path}
    def database_rows(path, table):
        with closing(sqlite3.connect(path)) as db:
            return db.execute('SELECT * FROM '+table+' ORDER BY rowid').fetchall()
    def session():
        return SavedSession(workspace_path, builtins().get('liepin'), backend='native',
                            browser='bundled', network='compatibility-fixture-network')
    with patch('socket.create_connection', side_effect=AssertionError('offline compatibility check')), \
            patch.object(SafeHTTP, 'request', side_effect=AssertionError('no provider requests')):
        workspace = Workspace(workspace_path)
        if mode == 'old-create':
            workspace.add_job({'title':'时间序列算法工程师','company':'ARTIFICIAL COMPATIBILITY FIXTURE',
                'text':'人工兼容测试，不是真实招聘。要求使用 Cursor 进行 AI 辅助编程，编写单元测试并审查代码。',
                'platform':'boss','url':'https://www.zhipin.com/job_detail/compatibility-fixture.html',
                'rights_note':'Artificial offline test only','evidence_level':'full_text','full_text_confirmed':True})
            report = workspace.analyze({})
            evidence = EvidenceService(workspace)
            row = next(r for r in evidence.catalogue({'run_id':report['id']})['rows']
                       if r['review_status']=='rule_accepted' and r['strength'] not in {'prohibited','not_required'})
            evidence.save({'expected_revision':0,'name':'ARTIFICIAL PERSON','project':'Compatibility fixture',
                'contribution':'人工测试记录，非本人真实经历','scope':'offline','run_id':report['id'],
                'external_url':'https://evidence.fixture.test/compatibility',
                'capabilities':[row['capability']],'requirement_ids':[row['requirement_id']],
                'metrics':[],'review_status':'draft','attested':False})
            guided = GuidedService(workspace)
            try:
                with patch.object(guided, '_submit'):
                    task = guided.create({'platform':'liepin','keyword':'架构师','roles':['architect'],
                        'max_pages':1,'max_jobs':1,'consent':True,'rights_note':'Artificial offline checkpoint',
                        'backend':'native','native_consent':True})
                guided.ledger.reserve('liepin','request')
                quota_path = str(guided.ledger.path.relative_to(workspace_path))
            finally:
                guided.close()
            lease = session()
            try:
                lease.save([{'name':'fixture','value':'ARTIFICIAL-NOT-A-LOGIN','domain':'www.liepin.com',
                    'path':'/','expires':-1,'httpOnly':True,'secure':True,'sameSite':'Lax'}])
            finally:
                lease.close()
            state = {'old_report':report['id'],'task':task['id'],'quota_path':quota_path,
                     'jobs':database_rows(workspace.db,'records'),
                     'evidence':database_rows(evidence.db,'revisions'),
                     'quota':database_rows(workspace_path/quota_path,'visits'),'files':stable_files()}
            state_path.write_text(json.dumps(state), encoding='utf-8')
            return {'phase':mode,'records':1,'reports':1,'native_checkpoint_created':True}
        state = json.loads(state_path.read_text(encoding='utf-8'))
        assert json.loads(json.dumps(database_rows(workspace.db,'records'))) == state['jobs']
        assert json.loads(json.dumps(database_rows(workspace_path/'evidence/history.sqlite','revisions'))) == state['evidence']
        assert json.loads(json.dumps(database_rows(workspace_path/state['quota_path'],'visits'))) == state['quota']
        before = stable_files()
        assert all(before.get(key) == value for key,value in state['files'].items()), 'existing file changed'
        assert workspace.report(state['old_report'])['manifest']['stats']['full_text_job_groups'] == 1
        guided = GuidedService(workspace)
        try:
            tasks = {t['id']:t for t in guided.state()['jobs']}
            assert tasks[state['task']]['backend'] == 'native'
            assert tasks[state['task']]['status'] == 'interrupted'
            assert not guided._backends, 'opening workspace launched a browser'
        finally:
            guided.close()
        lease = session()
        try:
            assert lease.restore()['cookies'][0]['value'] == 'ARTIFICIAL-NOT-A-LOGIN'
        finally:
            lease.close()
        if mode == 'candidate':
            report = workspace.analyze({})
            assert 'software_acquisition_capabilities' in report['manifest']
            state['candidate_report'] = report['id']
            state_path.write_text(json.dumps(state), encoding='utf-8')
        else:
            report = workspace.report(state['candidate_report'])
            assert report['manifest']['stats']['full_text_job_groups'] == 1
            assert EvidenceService(workspace).state()['revision'] == 1
        after = stable_files()
        assert all(after.get(key) == value for key,value in state['files'].items()), 'existing file changed'
        with closing(sqlite3.connect(workspace.db)) as db:
            assert db.execute('PRAGMA user_version').fetchone()[0] == 1
        return {'phase':mode,'original_rows_reports_evidence_and_quota_preserved':True,
                'native_checkpoint_loaded_offline':True,'synthetic_cookie_restored':True,'schema':1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--old-source', type=Path)
    parser.add_argument('--out', type=Path, default=ROOT/'browser-acceptance/compatibility/results.json')
    parser.add_argument('--phase', choices=('old-create','candidate','rollback'))
    parser.add_argument('--source', type=Path)
    parser.add_argument('--workspace', type=Path)
    args = parser.parse_args()
    if args.phase:
        print(json.dumps(phase(args.source.resolve(), args.workspace.resolve(), args.phase)))
        return
    if not args.old_source or not (args.old_source/'src/vibe_job_radar').is_dir():
        parser.error('--old-source must name the separately checked out old source')
    result = {'success':False,'scope':'Artificial temporary workspace, actual old/candidate source; no live requests',
              'old_source_revision':subprocess.check_output(['git','-C',str(args.old_source),'rev-parse','HEAD'],text=True).strip(),
              'checks':[]}
    try:
        with tempfile.TemporaryDirectory(prefix='vjr-compatibility-') as tmp:
            for mode,source in (('old-create',args.old_source),('candidate',ROOT),('rollback',args.old_source)):
                process = subprocess.run([sys.executable,str(Path(__file__).resolve()),'--phase',mode,
                    '--source',str(source.resolve()),'--workspace',tmp], capture_output=True,text=True,encoding='utf-8',timeout=60)
                if process.returncode:
                    raise RuntimeError(mode+' failed: '+process.stderr[-4000:])
                result['checks'].append(json.loads(process.stdout))
        result['success'] = True
    finally:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(result,ensure_ascii=False,indent=2), encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
