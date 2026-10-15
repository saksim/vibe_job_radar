"""Real frozen-process handoff with the current build, in an isolated workspace.

No fabricated executable, public download, recruiting traffic or registry write.
This checks process/data handoff; newer-version consent is checked by UI tests.
"""
from contextlib import closing
import http.client
import json
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
import threading
import time
import zipfile

FIELDS=('frozen_executable','same_build_handoff','original_data_preserved',
        'backup_verified','new_http_ready','committed','owned_child_cleaned')


def valid_evidence(value):
    return (isinstance(value,dict) and set(value)=={'schema_version',*FIELDS}
            and type(value['schema_version']) is int and value['schema_version']==1
            and all(value[key] is True for key in FIELDS))


def verify(bundle, root, cwd, env):
    from verify_windows_portable import RunningApp
    from vibe_job_radar.release_install import (WindowsProcess,write_json,read_json,
                                                wait_record,launch_environment)
    from vibe_job_radar.release_updates import stage_package,file_digest,io_path
    root.mkdir();workspace=root/'workspace';workspace.mkdir();updates=workspace/'updates';updates.mkdir()
    sentinel=workspace/'authored-private.txt';sentinel.write_bytes(b'KEEP ARTIFICIAL LOCAL DATA')
    db=workspace/'authored.sqlite'
    with closing(sqlite3.connect(db)) as connection:
        connection.execute('CREATE TABLE evidence(value TEXT)')
        connection.execute('INSERT INTO evidence VALUES (?)',('preserved',));connection.commit()
    original={p.name:p.read_bytes() for p in (sentinel,db)}
    package=updates/'built-payload.zip'
    with zipfile.ZipFile(package,'w',zipfile.ZIP_DEFLATED,compresslevel=1) as archive:
        for path in bundle.rglob('*'):
            if path.is_file():archive.write(path,'VibeJobRadar/'+path.relative_to(bundle).as_posix())
    meta=read_json(bundle/'PORTABLE.json')
    selected=dict(version=meta['version'],sha256=file_digest(package),
                  revision=meta['runtime_source']['revision'],source_sha256=meta['source']['sha256'])
    directory=stage_package(package,selected,updates,threading.Event())
    journal=updates/('install-'+secrets.token_hex(12));journal.mkdir();nonce=secrets.token_hex(32)
    old=RunningApp(bundle/'VibeJobRadar.exe',workspace,cwd,env)
    helper=None;child=None;owned_child=False
    proof=dict(schema_version=1,**{key:False for key in FIELDS})
    try:
        parent=WindowsProcess(old.proc.pid)
        try:created=parent.created
        finally:parent.close()
        request=dict(schema_version=1,workspace=str(workspace.resolve()),directory=str(directory),package=str(package),
            selected=selected,parent_pid=old.proc.pid,parent_created=created,
            old_executable=str((bundle/'VibeJobRadar.exe').resolve()),old_version=meta['version'],
            nonce=nonce,no_browser=True)
        path=journal/'request.json';write_json(path,request)
        helper=subprocess.Popen([str(bundle/'VibeJobRadar.exe'),'--apply-update',str(path)],cwd=cwd,
            env={**env,**{k:v for k,v in launch_environment().items() if k=='PYINSTALLER_RESET_ENVIRONMENT'}},
            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        wait_record(journal/'status.json',nonce,'armed',process=helper,timeout=20)
        # Only the verifier-owned old app is stopped. Product UI quiescence has
        # separate busy/idle tests and normally exits via server.shutdown().
        old.close()
        ready=wait_record(journal/'ready.json',nonce,'ready',timeout=60)
        # Retain this exact child handle before checking/terminating it; no PID-based taskkill.
        child=WindowsProcess(ready['pid'])
        from ctypes import wintypes
        import ctypes
        child.api.QueryFullProcessImageNameW.argtypes=[wintypes.HANDLE,wintypes.DWORD,wintypes.LPWSTR,ctypes.POINTER(wintypes.DWORD)]
        buffer=ctypes.create_unicode_buffer(32768);length=wintypes.DWORD(len(buffer))
        assert child.api.QueryFullProcessImageNameW(child.handle,0,buffer,ctypes.byref(length))
        assert Path(buffer.value).resolve()==(directory/'VibeJobRadar.exe').resolve()
        owned_child=True
        assert helper.wait(60)==0
        state=read_json(journal/'status.json');current=read_json(updates/'current.json')
        assert state['status']=='committed' and state['nonce']==nonce and current['nonce']==nonce
        proof.update(frozen_executable=True,same_build_handoff=True,committed=True)
        deadline=time.monotonic()+15
        while True:
            connection=http.client.HTTPConnection('127.0.0.1',ready['port'],timeout=2)
            try:
                connection.request('GET','/api/releases/state');response=connection.getresponse();response.read()
                assert response.status==403;break  # Live authenticated API, no stored session token.
            except OSError:
                if time.monotonic()>deadline:raise
                time.sleep(.1)
            finally:connection.close()
        proof['new_http_ready']=True
        backup=Path(current['backup'])/'workspace'
        assert (backup/sentinel.name).read_bytes()==original[sentinel.name]
        with closing(sqlite3.connect(backup/db.name)) as connection:
            assert connection.execute('SELECT value FROM evidence').fetchall()==[('preserved',)]
        proof['backup_verified']=True
        assert all((workspace/name).read_bytes()==value for name,value in original.items())
        proof['original_data_preserved']=True
    finally:
        old.close()
        if helper is not None and helper.poll() is None:helper.terminate();helper.wait(10)
        if child is not None and not owned_child:child.close()
        if child is not None and owned_child:
            # Open terminate rights only after the retained handle's executable
            # identity has been checked. Compare creation time on the new handle.
            termination=WindowsProcess(ready['pid'],terminate=True)
            try:
                assert termination.created==child.created
                termination.terminate();assert termination.exited(10000)
                proof['owned_child_cleaned']=True
            finally:termination.close();child.close()
    assert valid_evidence(proof)
    return proof
