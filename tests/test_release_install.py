"""Automatic update orchestration without executing authored fixture bytes."""
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import zipfile

from vibe_job_radar import release_install as install, release_updates as updates
from vibe_job_radar.workspace import Workspace, InputError
from vibe_job_radar.windows_startup import WindowsStartup
from test_release_updates import ReleaseFixture


class Registry:
    def __init__(self):self.values={}
    def read(self,name):return self.values.get(name)
    def create(self,name,command):self.values[name]=(1,command)
    def replace(self,name,old,new):
        if self.values.get(name)!=(1,old):raise InputError('changed')
        self.values[name]=(1,new)


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve();self.workspace=Workspace(self.root/'data')
        self.addCleanup(self.workspace.close_network)
        self.updates=self.workspace.root/'updates';self.updates.mkdir()
        self.old=self.root/'old'/'VibeJobRadar.exe';self.old.parent.mkdir();self.old.write_bytes(b'OLD TEST BYTES')
        (self.old.parent/'PORTABLE.json').write_text('{"upgrade_protocol":1}',encoding='utf8')
        self.selected=dict(version='0.5.0',revision='a'*40,source_sha256='b'*64)
        self.package=self.updates/'fixture.zip'
        meta=dict(version='0.5.0',platform='windows-x64',upgrade_protocol=1,
                  runtime_source={'revision':'a'*40},source={'sha256':'b'*64})
        with zipfile.ZipFile(self.package,'w') as archive:
            archive.writestr('VibeJobRadar/VibeJobRadar.exe',b'AUTHORED BYTES, NEVER EXECUTE')
            archive.writestr('VibeJobRadar/PORTABLE.json',json.dumps(meta))
            archive.writestr('VibeJobRadar/_internal/library.dat',b'VERIFIED LIBRARY')
        self.selected['sha256']=updates.file_digest(self.package)
        self.directory=updates.stage_package(self.package,self.selected,self.updates,threading.Event())
        self.journal=self.updates/('install-'+'c'*24);self.journal.mkdir()
        self.path=self.journal/'request.json'
        self.request=dict(schema_version=1,workspace=str(self.workspace.root),directory=str(self.directory),
            package=str(self.package),selected=self.selected,parent_pid=321,parent_created=123,
            old_executable=str(self.old),old_version=install.__version__,nonce='d'*64,no_browser=True)
        install.write_json(self.path,self.request)
        (self.workspace.root/'private.txt').write_bytes(b'KEEP PRIVATE ORIGINAL')

    def test_backup_preserves_wal_rows_original_bytes_and_user_manifest_name(self):
        (self.workspace.root/'BACKUP.json').write_bytes(b'user-owned')
        (self.workspace.root/'empty-user-directory').mkdir()
        database=self.workspace.root/'retained.sqlite'
        with closing(sqlite3.connect(database)) as conn:
            conn.execute('PRAGMA journal_mode=WAL');conn.execute('PRAGMA wal_autocheckpoint=0')
            conn.execute('CREATE TABLE original(value)');conn.execute('INSERT INTO original VALUES(42)');conn.commit()
            db_bytes=database.read_bytes();wal_bytes=Path(str(database)+'-wal').read_bytes()
            backup=install.backup_workspace(self.workspace.root,self.updates)
            self.assertEqual(database.read_bytes(),db_bytes)
            self.assertEqual(Path(str(database)+'-wal').read_bytes(),wal_bytes)
        with closing(sqlite3.connect(backup/'workspace'/'retained.sqlite')) as conn:
            self.assertEqual(conn.execute('SELECT value FROM original').fetchall(),[(42,)])
        self.assertEqual((backup/'workspace'/'BACKUP.json').read_bytes(),b'user-owned')
        self.assertTrue((backup/'workspace'/'empty-user-directory').is_dir())
        self.assertFalse((backup/'workspace'/'updates').exists())
        self.assertTrue(install.read_json(backup/'BACKUP.json')['complete'])

    def test_changed_source_aborts_owned_backup_and_keeps_other_files(self):
        operator=self.updates/'operator.txt';operator.write_bytes(b'KEEP')
        real=install.inventory;calls=0
        def changed(root,**kwargs):
            nonlocal calls
            result=real(root,**kwargs);calls+=1
            if calls==2:result['new-file']=(1,2,3,4)
            return result
        with patch.object(install,'inventory',side_effect=changed):
            with self.assertRaises(updates.UpdateError):install.backup_workspace(self.workspace.root,self.updates)
        self.assertEqual(list(self.updates.glob('backup-*')),[])
        self.assertEqual(operator.read_bytes(),b'KEEP')
        self.assertEqual((self.workspace.root/'private.txt').read_bytes(),b'KEEP PRIVATE ORIGINAL')

    def test_staging_tampering_or_extra_dependency_cannot_be_executed(self):
        install.verify_directory(self.directory,self.package,self.selected)
        library=self.directory/'_internal'/'library.dat';library.write_bytes(b'REPLACED')
        with self.assertRaises(updates.UpdateError):install.verify_directory(self.directory,self.package,self.selected)
        library.write_bytes(b'VERIFIED LIBRARY');extra=self.directory/'extra.dll';extra.write_bytes(b'EXTRA')
        with self.assertRaises(updates.UpdateError):install.verify_directory(self.directory,self.package,self.selected)
        self.assertEqual(self.old.read_bytes(),b'OLD TEST BYTES')

    def run_helper(self, *, fail_start=False, fail_commit=False):
        parent=MagicMock(created=123);parent.exited.return_value=True
        child=MagicMock(pid=654);child.poll.return_value=1 if fail_start else None
        def spawn(args):
            self.assertEqual(Path(args[0]),self.directory/'VibeJobRadar.exe')
            if not fail_start:
                install.write_json(self.journal/'ready.json',dict(status='ready',nonce='d'*64,version='0.5.0',pid=654))
            return child
        real_write=install.write_json
        def write(path,data):
            if fail_commit and Path(path).name=='current.json':raise OSError('authored commit failure')
            return real_write(path,data)
        with patch.object(install,'supported',return_value=True),patch.object(install.sys,'executable',str(self.old)), \
                patch.object(install,'WindowsProcess',return_value=parent),patch.object(install,'spawn',side_effect=spawn) as execute, \
                patch('vibe_job_radar.windows_startup.WindowsRun') as startup, \
                patch.object(install,'write_json',side_effect=write):
            code=install.apply_update(self.path)
        return code,child,execute,startup

    def test_ready_process_commits_after_backup_and_old_entry_selects_verified_new_program(self):
        code,child,execute,startup=self.run_helper()
        self.assertEqual(code,0);self.assertEqual(execute.call_count,1)
        record=install.read_json(self.updates/'current.json');self.assertTrue(Path(record['backup']).is_dir())
        self.assertEqual(install.read_json(self.journal/'status.json')['status'],'committed')
        with patch.object(install,'supported',return_value=True),patch.object(install.sys,'executable',str(self.old)), \
                patch.object(install,'__version__','0.4.0'):
            command=install.installed_command(self.workspace.root,['--workspace',str(self.workspace.root),'--public-worker'])
            self.assertEqual(command,[str(self.directory/'VibeJobRadar.exe'),'--workspace',str(self.workspace.root),'--public-worker'])
            (self.directory/'VibeJobRadar.exe').write_bytes(b'CHANGED')
            with self.assertRaises(updates.UpdateError):install.installed_command(self.workspace.root,[])
        self.assertEqual((self.workspace.root/'private.txt').read_bytes(),b'KEEP PRIVATE ORIGINAL')

    def test_failed_launch_does_not_activate_or_restore_over_original_data(self):
        code,child,execute,startup=self.run_helper(fail_start=True)
        self.assertEqual(code,2);startup.assert_not_called()
        self.assertFalse((self.updates/'current.json').exists())
        self.assertEqual(install.read_json(self.journal/'status.json')['status'],'failed')
        self.assertEqual(len(list(self.updates.glob('backup-*'))),1)
        self.assertEqual((self.workspace.root/'private.txt').read_bytes(),b'KEEP PRIVATE ORIGINAL')

    def test_activation_failure_preserves_startup_and_stops_owned_child(self):
        code,child,execute,startup=self.run_helper(fail_commit=True)
        self.assertEqual(code,2);startup.assert_not_called()
        child.terminate.assert_called_once()
        self.assertFalse((self.updates/'current.json').exists())

    def test_stale_parent_identity_stops_before_backup_or_execution(self):
        with patch.object(install,'supported',return_value=True),patch.object(install.sys,'executable',str(self.old)), \
                patch.object(install,'WindowsProcess',return_value=MagicMock(created=999)),patch.object(install,'spawn') as execute:
            self.assertEqual(install.apply_update(self.path),2)
            execute.assert_not_called()
        self.assertEqual(list(self.updates.glob('backup-*')),[])

    def test_owned_startup_keeps_its_command_and_follows_committed_new_version(self):
        registry=Registry()
        startup=WindowsStartup(self.workspace,registry=registry,executable=self.old,portable=True,platform='win32')
        from vibe_job_radar.windows_startup import MODE_CONSENT
        state=startup.state();startup.enable(dict(revision=state['revision'],consent=True,
            consent_version=MODE_CONSENT,mode='public_worker'))
        before=copy.deepcopy(registry.values);receipt=startup.receipt_path.read_bytes()
        self.assertEqual(self.run_helper()[0],0)
        new=WindowsStartup(self.workspace,registry=registry,executable=self.directory/'VibeJobRadar.exe',portable=True,platform='win32')
        state=new.state()
        self.assertEqual(state['status'],'registered');self.assertTrue(state['uses_retained_launcher'])
        self.assertEqual(state['mode'],'public_worker');self.assertTrue(state['can_disable'])
        self.assertEqual(registry.values,before);self.assertEqual(startup.receipt_path.read_bytes(),receipt)
        registry.values[startup.name]=(1,'FOREIGN')
        self.assertEqual(new.state()['status'],'conflict')
        self.assertEqual(registry.read(startup.name),(1,'FOREIGN'))

    def test_release_api_needs_separate_consent_and_can_use_already_prepared_zip(self):
        fixture=ReleaseFixture();installer=MagicMock()
        installer.install.return_value={'status':'restarting','message':'test handoff'}
        service=updates.ReleaseUpdates(self.workspace,transport_factory=lambda:fixture,current_version='0.3.0',installer=installer)
        self.addCleanup(service.close)
        service.check({'consent':True});service._thread.join(10)
        request=dict(consent=True,release_id=42,sha256=service.state()['sha256'])
        with self.assertRaises(InputError):service.install(request)
        service.download(request);service._thread.join(10)
        self.assertEqual(service.state()['status'],'ready')
        downloaded=fixture.calls.count(next(name for name in fixture.data if name.endswith('.zip')))
        service.install({**request,'consent_version':install.CONSENT});service._thread.join(10)
        self.assertEqual(service.state()['status'],'restarting')
        self.assertEqual(fixture.calls.count(next(name for name in fixture.data if name.endswith('.zip'))),downloaded)
        installer.install.assert_called_once()

    def test_last_install_is_offline_redacted_and_preserves_invalid_record(self):
        install.write_json(self.updates/'latest-install.json',dict(request=str(self.path)))
        self.assertEqual(self.run_helper()[0],0)
        result=install.last_install(self.workspace.root)
        self.assertEqual(result['status'],'committed');self.assertNotIn('nonce',result)
        self.assertTrue(Path(result['backup']).is_dir())
        (self.updates/'latest-install.json').write_text('UNRELATED USER DATA',encoding='utf8')
        self.assertEqual(install.last_install(self.workspace.root)['status'],'unreadable')
        self.assertEqual((self.updates/'latest-install.json').read_text(encoding='utf8'),'UNRELATED USER DATA')

    def test_busy_workbench_is_preserved_and_idle_handoff_stops_dispatchers(self):
        from vibe_job_radar.workbench import LocalServer
        server=LocalServer(self.workspace,public_client=None)
        self.addCleanup(server.server_close)
        server.public_schedule.start();server.public_queue.start()
        for field in ('_closing_backends','_closure_uncertain'):
            with patch.object(server.guided,field,True):
                with self.assertRaises(InputError):server.upgrade_preflight()
        with patch.object(server.public_tasks,'busy',return_value=True):
            with self.assertRaises(InputError):server.upgrade_quiesce()
        self.assertFalse(server.mutation_lock.locked())
        self.assertTrue(server.public_queue.is_running())
        server.upgrade_quiesce()
        self.assertTrue(server.mutation_lock.locked())
        self.assertFalse(server.public_queue.is_running());self.assertFalse(server.public_schedule.is_running())
        server.mutation_lock.release()

    def test_release_requires_complete_automatic_handoff_evidence(self):
        from automatic_update_acceptance import valid_evidence,FIELDS
        good=dict(schema_version=1,**{key:True for key in FIELDS})
        self.assertTrue(valid_evidence(good))
        for key in FIELDS:
            self.assertFalse(valid_evidence({**good,key:False}))
        self.assertFalse(valid_evidence({**good,'schema_version':True}))
