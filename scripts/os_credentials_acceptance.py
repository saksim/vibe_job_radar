"""Exercise real OS vaults through source and frozen application entry points."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from posix_artifacts import require
from native_test_vault import control

FIELDS = ('four_proxy_modes','independent_reopen','ciphertext_only','team_legacy_migration',
          'team_encrypted_install','team_independent_restore','locked_read_stopped',
          'locked_write_preserved','tampered_payload_rejected','moved_workspace_rejected',
          'clear_stops_route','rollback_preserves_other_settings')
USER, SECRET = 'ARTIFICIAL-VAULT-USER', 'ARTIFICIAL-VAULT-SECRET'
TOKEN = 'vjr_' + 'C' * 43


def valid_evidence(value, *, frozen=True):
    return (isinstance(value,dict) and set(value) == set(FIELDS)|{'schema_version','provider','frozen_executable','artificial_vault','success'}
        and type(value['schema_version']) is int and value['schema_version']==1
        and value['provider'] in {'secret-service','macos-keychain'}
        and value['frozen_executable'] is frozen and value['artificial_vault'] is True
        and value['success'] is True and all(value[name] is True for name in FIELDS))


def verify(command, cwd, env, *, frozen):
    from verify_posix_portable import RunningApp
    proof=dict(schema_version=1,provider='secret-service' if sys.platform=='linux' else 'macos-keychain',
               frozen_executable=frozen,artificial_vault=True,success=False,**{name:False for name in FIELDS})
    with tempfile.TemporaryDirectory(prefix='radar-vault-proof-') as temporary:
        root=Path(temporary); workspace=root/'工作区 spaces'
        config=root/'config.json'; credential=root/'artificial-device.json'
        config.write_text(json.dumps(dict(schema_version=1,origin='https://team.fixture.invalid',
            quota_resources={},public_sources=[],use_public_service=False)),encoding='utf-8')
        credential.write_text(json.dumps(dict(schema_version=1,device_token=TOKEN)),encoding='utf-8')
        def cli(args, *, success=True):
            result=subprocess.run(command+args,cwd=cwd,env=env,capture_output=True,timeout=35)
            require(result.returncode==(0 if success else 2),'Credential CLI failed')
            require(len(result.stdout)<=65536 and len(result.stderr)<=65536,'Oversized credential CLI output')
            require(all(secret.encode() not in result.stdout+result.stderr for secret in (USER,SECRET,TOKEN)),
                    'Credential leaked in CLI')
            return json.loads(result.stdout)
        cli(['--workspace',str(workspace),'--team-install',str(config),'--team-credential',str(credential)])
        network=workspace/'network-preferences.json'; device=workspace/'.team/credential.json'
        require(json.loads(device.read_text())['scheme']=='owner-file','Legacy install changed')
        for mode in ('http_auth','http_digest_auth','http_ntlm_auth','socks5_auth'):
            app=RunningApp(command,workspace,cwd,env)
            try:
                state=app.json('/api/network/state')
                saved=app.json('/api/network/proxy-auth',dict(mode=mode,
                    endpoint=('socks5' if mode=='socks5_auth' else 'http')+'://127.0.0.1:18990',
                    username=USER,password=SECRET,revision=state['revision'],consent=True))
                require(saved['schema_version']==12 and saved['proxy_auth_saved'] is True
                    and saved['policy']['proxy_authentication_configured'] is True,'Native credential save failed')
                original=network.read_bytes()
                require(all(x.encode() not in original+json.dumps(saved).encode() for x in (USER,SECRET)),
                        'Proxy secret in disk/status')
            finally:app.close()
            reopened=RunningApp(command,workspace,cwd,env)
            try:
                restored=reopened.json('/api/network/state')
                require(restored['proxy_mode']==mode and restored['policy']['proxy_authentication_configured'] is True
                    and restored['policy']['proxy_credentials_scope']=='explicit_workspace_system_keyring_loopback'
                    and 'proxy_auth' not in restored and network.read_bytes()==original,'Independent proxy restore failed')
            finally:reopened.close()
        proof.update(four_proxy_modes=True,independent_reopen=True,ciphertext_only=True)
        app=RunningApp(command,workspace,cwd,env)
        try:
            state=app.json('/api/team/state')
            require(state['credential_migration_available'] is True,'Legacy migration unavailable')
            migrated=app.json('/api/team/protect',{'consent':True,'revision':state['credential_revision']})
            require(migrated['restart_required'] is True and migrated['credential_storage']=='system-keyring-aesgcm-v1','Migration failed')
            require(TOKEN.encode() not in device.read_bytes(),'Team token in encrypted file')
            proof['team_legacy_migration']=True
        finally:app.close()
        result=cli(['--workspace',str(workspace),'--team-protect-credential'])
        require(result['restart_required'] is False,'Independent team restore failed')
        proof['team_independent_restore']=True
        other=root/'encrypted install'
        result=cli(['--workspace',str(other),'--team-install',str(config),'--team-credential',str(credential),'--team-encrypted'])
        require(result['credential_storage']=='system-keyring-aesgcm-v1','Explicit encrypted install failed')
        require(cli(['--workspace',str(other),'--team-protect-credential'])['restart_required'] is False,'New device restore failed')
        proof['team_encrypted_install']=True
        before_network=network.read_bytes();before_device=device.read_bytes()
        control('lock')
        try:
            app=RunningApp(command,workspace,cwd,env)
            try:
                state=app.json('/api/network/state')
                require(state['policy']['code']=='credential_store_locked','Locked proxy did not stop')
                error=app.json('/api/network/proxy-auth',dict(mode='http_auth',endpoint='http://127.0.0.1:18990',
                    username=USER,password=SECRET,revision=state['revision'],consent=True),expected_status=400)
                require('锁定' in error['error'],'Locked write missing actionable message')
                require(network.read_bytes()==before_network,'Locked save changed original')
            finally:app.close()
            result=cli(['--workspace',str(workspace),'--team-protect-credential'],success=False)
            require(result['error']=='credential_store_locked' and device.read_bytes()==before_device,'Locked team restore changed file')
            proof.update(locked_read_stopped=True,locked_write_preserved=True)
        finally:control('unlock')
        copied=root/'moved workspace';shutil.copytree(workspace,copied)
        app=RunningApp(command,copied,cwd,env)
        try:
            require(app.json('/api/network/state')['policy']['code']=='credential_store_missing','Moved workspace reused key')
        finally:app.close()
        require(cli(['--workspace',str(copied),'--team-protect-credential'],success=False)['error']=='credential_store_missing',
                'Moved team workspace recreated key')
        proof['moved_workspace_rejected']=True
        changed=json.loads(before_network); payload=base64.b64decode(changed['proxy_auth']['payload'])
        changed['proxy_auth']['payload']=base64.b64encode(bytes([payload[0]^1])+payload[1:]).decode()
        network.write_text(json.dumps(changed),encoding='utf-8')
        app=RunningApp(command,workspace,cwd,env)
        try:require(app.json('/api/network/state')['policy']['code']=='credential_store_invalid','Tampered proxy restored')
        finally:app.close()
        network.write_bytes(before_network);proof['tampered_payload_rejected']=True
        app=RunningApp(command,workspace,cwd,env)
        try:
            state=app.json('/api/network/state')
            cleared=app.json('/api/network/proxy-auth/clear',{'revision':state['revision'],'consent':True})
            require(cleared['policy']['code']=='workspace_proxy_auth_missing','Clear allowed anonymous fallback')
            proof['clear_stops_route']=True
            restored=app.json('/api/network/proxy',dict(mode='auto',endpoint='',consent=False,revision=cleared['revision']))
            require(restored['schema_version']==2 and device.read_bytes()==before_device,'Rollback changed other credential')
            proof['rollback_preserves_other_settings']=True
        finally:app.close()
        proof['success']=True
        require(valid_evidence(proof,frozen=frozen),'Incomplete OS credential proof')
    return proof
