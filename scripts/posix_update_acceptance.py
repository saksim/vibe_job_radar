"""Actual frozen helper/child handoff; authored workspace, no public downloads."""
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import sqlite3
import subprocess
import threading
import time

from vibe_job_radar.release_posix import HANDOFF_FIELDS, valid_handoff


def verify(bundle, archive, root, cwd, env, proof=None):
    import psutil  # Qualification only, never a product dependency.
    from verify_posix_portable import RunningApp
    from vibe_job_radar.release_install import write_json, read_json, wait_record
    from vibe_job_radar.release_updates import stage_package, file_digest
    from vibe_job_radar.posix_artifacts import inventory
    root.mkdir(); root=root.resolve()
    workspace=root/'工作区 with spaces'; workspace.mkdir()
    updates=workspace/'updates'; updates.mkdir()
    sentinel=workspace/'authored.txt'; sentinel.write_bytes(b'KEEP ARTIFICIAL ORIGINAL')
    database=workspace/'authored.sqlite'
    with closing(sqlite3.connect(database)) as connection:
        connection.execute('CREATE TABLE evidence(value TEXT)')
        connection.execute('INSERT INTO evidence VALUES (?)',('preserved',));connection.commit()
    original={p.name:p.read_bytes() for p in (sentinel,database)}
    package=updates/'same-build.tar.gz';shutil.copyfile(archive,package)
    meta=read_json(bundle/'PORTABLE.json',limit=8_000_000)
    selected=dict(version=meta['version'],sha256=file_digest(package),platform=meta['platform'],
        automatic_update=True,revision=meta['runtime_source']['revision'],source_sha256=meta['source']['sha256'])
    directory=stage_package(package,selected,updates,threading.Event())
    if proof is None:proof={}
    proof.update(schema_version=1,**{key:False for key in HANDOFF_FIELDS})
    original_rows=inventory(bundle); prepared_rows=inventory(directory)
    assert original_rows==prepared_rows
    proof['internal_links_preserved']=all(prepared_rows[n]==r for n,r in original_rows.items() if r['kind']=='symlink')
    proof['executable_permissions_preserved']=os.access(directory/'VibeJobRadar',os.X_OK)
    journal=updates/('install-'+secrets.token_hex(12));journal.mkdir();nonce=secrets.token_hex(32)
    reader,writer=os.pipe();old=None;helper=None;child=None
    def owned_child(ready):
        candidate=psutil.Process(ready['pid'])
        assert candidate.create_time()>=helper_started
        assert Path(candidate.exe()).resolve()==directory/'VibeJobRadar'
        args=candidate.cmdline()
        assert '--upgrade-handoff' in args and str(journal/'request.json') in args
        return candidate
    try:
        old=RunningApp(bundle/'VibeJobRadar',workspace,cwd,env,pass_fds=(writer,))
        os.close(writer);writer=None
        request=dict(schema_version=2,workspace=str(workspace.resolve()),directory=str(directory),package=str(package),
            selected=selected,parent_pid=old.process.pid,parent_created=os.fstat(reader).st_ino,
            old_executable=str((bundle/'VibeJobRadar').resolve()),old_version=meta['version'],nonce=nonce,no_browser=True)
        path=journal/'request.json';write_json(path,request)
        helper=subprocess.Popen([str(bundle/'VibeJobRadar'),'--apply-update',str(path),'--update-parent-fd',str(reader)],
            pass_fds=(reader,),cwd=cwd,env={**env,'PYINSTALLER_RESET_ENVIRONMENT':'1'},start_new_session=True,
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        helper_started=psutil.Process(helper.pid).create_time()
        os.close(reader);reader=None
        wait_record(journal/'status.json',nonce,'armed',process=helper,timeout=20)
        assert not list(updates.glob('backup-*')) and old.process.poll() is None
        old.close()
        ready=wait_record(journal/'ready.json',nonce,'ready',timeout=60)
        child=owned_child(ready)
        assert helper.wait(60)==0
        current=read_json(updates/'current.json');state=read_json(journal/'status.json')
        assert state['status']=='committed' and state['nonce']==current['nonce']==nonce
        proof.update(frozen_executable=True,same_build_handoff=True,committed=True,parent_exit_pipe=True)
        end=time.monotonic()+15
        while True:
            connection=http.client.HTTPConnection('127.0.0.1',ready['port'],timeout=2)
            try:
                connection.request('GET','/api/releases/state');response=connection.getresponse();response.read()
                assert response.status==403;break
            except OSError:
                if time.monotonic()>end:raise
                time.sleep(.1)
            finally:connection.close()
        proof['new_http_ready']=True
        backup=Path(current['backup'])/'workspace'
        assert (backup/sentinel.name).read_bytes()==original[sentinel.name]
        with closing(sqlite3.connect(backup/database.name)) as connection:
            assert connection.execute('SELECT value FROM evidence').fetchall()==[('preserved',)]
        proof['backup_verified']=True
        assert all((workspace/name).read_bytes()==data for name,data in original.items())
        proof['original_data_preserved']=True
    except Exception as error:
        proof['failure']={'error_type':type(error).__name__,
            'helper_exit':helper.poll() if helper is not None else None}
        if (journal/'status.json').is_file():
            saved=read_json(journal/'status.json')
            proof['failure'].update(status=saved.get('status'),helper_error_type=saved.get('error_type'))
        raise
    finally:
        if writer is not None:os.close(writer)
        if reader is not None:os.close(reader)
        if old is not None:old.close()
        if helper is not None and helper.poll() is None:helper.terminate();helper.wait(10)
        if child is not None:
            # psutil retains creation-time identity and checks reuse before signals.
            if child.is_running():child.send_signal(signal.SIGINT)
            try:child.wait(10)
            except psutil.TimeoutExpired:child.kill();child.wait(5)
            proof['owned_child_cleaned']=not child.is_running() or child.status()==psutil.STATUS_ZOMBIE
    assert valid_handoff(proof)
    return proof
