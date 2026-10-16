"""Ephemeral vault fixture; local maintainers must explicitly opt in."""
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile

PASSWORD='ARTIFICIAL-CI-VAULT-PASSWORD'


def _ci():
    hosted = os.environ.get('GITHUB_ACTIONS')=='true' and os.environ.get('RUNNER_OS') in {'Linux','macOS'}
    if not hosted and os.environ.get('RADAR_TEST_LOCAL_VAULT_ALLOWED') != 'explicit-isolated-fixture':
        raise ValueError('Local native builds require --isolated-vault; use an isolated desktop session')


def _run(args, **kwargs):
    return subprocess.run(args,check=True,capture_output=True,timeout=20,**kwargs).stdout.decode('utf-8')


def control(action):
    _ci()
    if action not in {'lock','unlock'}:raise ValueError('Invalid fixture action')
    root=Path(os.environ['RADAR_TEST_VAULT_ROOT']).resolve()
    if not root.is_dir() or root.name[:17]!='radar-vault-test-':raise ValueError('Not the isolated fixture')
    if sys.platform=='darwin':
        keychain=root/'fixture.keychain-db'
        _run(['/usr/bin/security',action+'-keychain']+(['-p',PASSWORD] if action=='unlock' else [])+[str(keychain)])
    elif sys.platform=='linux':
        if action=='unlock':
            _run(['/usr/bin/gnome-keyring-daemon','--replace','--unlock','--components=secrets',
                  '--control-directory='+str(root/'control')],input=PASSWORD.encode())
            import secretstorage
            from secretstorage.collection import Collection
            connection=secretstorage.dbus_init()
            try:
                if Collection(connection).is_locked():raise ValueError('Artificial default collection did not unlock')
            finally:connection.close()
        else:
            import secretstorage
            from secretstorage.collection import Collection
            connection=secretstorage.dbus_init()
            try:Collection(connection).lock()
            finally:connection.close()
    else:raise ValueError('Unsupported fixture OS')


@contextmanager
def _linux_bus():
    """Own a separate bus even if a local maintainer has a desktop bus already."""
    import select
    process=subprocess.Popen(['/usr/bin/dbus-daemon','--session','--nofork','--print-address=1'],
        stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,start_new_session=True)
    try:
        if not select.select([process.stdout],[],[],5)[0]:raise ValueError('Fixture D-Bus startup timeout')
        address=process.stdout.readline(4096).decode('ascii').strip()
        if not address.startswith('unix:') or len(address)>2048:raise ValueError('Invalid fixture D-Bus address')
        yield address
    finally:
        if process.poll() is None:
            process.terminate()
            try:process.wait(3)
            except subprocess.TimeoutExpired:process.kill();process.wait(3)
        process.stdout.close()


@contextmanager
def native_vault(*, allow_local=False):
    old_env=dict(os.environ)
    if allow_local:os.environ['RADAR_TEST_LOCAL_VAULT_ALLOWED']='explicit-isolated-fixture'
    _ci()
    with tempfile.TemporaryDirectory(prefix='radar-vault-test-') as temporary, ExitStack() as services:
        root=Path(temporary).resolve()
        os.environ['RADAR_TEST_VAULT_ROOT']=str(root)
        original_default=None; original_list=None; created=False
        try:
            if sys.platform=='linux':
                os.environ['DBUS_SESSION_BUS_ADDRESS']=services.enter_context(_linux_bus())
                for name in ('data','config','runtime','control'):(root/name).mkdir(mode=0o700)
                os.environ.update(XDG_DATA_HOME=str(root/'data'),XDG_CONFIG_HOME=str(root/'config'),
                                  XDG_RUNTIME_DIR=str(root/'runtime'),GNOME_KEYRING_CONTROL=str(root/'control'))
                output=_run(['/usr/bin/gnome-keyring-daemon','--unlock','--components=secrets',
                             '--control-directory='+str(root/'control')],input=PASSWORD.encode())
                for line in output.splitlines():
                    if line.startswith('GNOME_KEYRING_CONTROL='):
                        os.environ['GNOME_KEYRING_CONTROL']=line.partition('=')[2]
            elif sys.platform=='darwin':
                original_default=shlex.split(_run(['/usr/bin/security','default-keychain','-d','user']))[0]
                original_list=shlex.split(_run(['/usr/bin/security','list-keychains','-d','user']))
                keychain=str(root/'fixture.keychain-db')
                _run(['/usr/bin/security','create-keychain','-p',PASSWORD,keychain]);created=True
                _run(['/usr/bin/security','default-keychain','-d','user','-s',keychain])
                _run(['/usr/bin/security','set-keychain-settings','-lut','21600',keychain])
                control('unlock')
            else:raise ValueError('Unsupported fixture OS')
            yield
        finally:
            if sys.platform=='darwin':
                try:
                    if original_default:_run(['/usr/bin/security','default-keychain','-d','user','-s',original_default])
                    if original_list is not None:_run(['/usr/bin/security','list-keychains','-d','user','-s',*original_list])
                finally:
                    if created:_run(['/usr/bin/security','delete-keychain',str(root/'fixture.keychain-db')])
            os.environ.clear();os.environ.update(old_env)
