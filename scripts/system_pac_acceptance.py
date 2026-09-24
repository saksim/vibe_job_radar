"""Explicit ephemeral-CI-only PAC source fixture; never change a personal host."""
from contextlib import contextmanager
import os
from pathlib import Path
import sys


@contextmanager
def configured_source():
    if os.name!='nt' or os.environ.get('GITHUB_ACTIONS')!='true' or os.environ.get('CI')!='true':
        raise ValueError('system PAC registry acceptance requires ephemeral Windows CI')
    import winreg
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tests'))
    from test_system_pac import SourceServer
    fixture=SourceServer()
    fixture.body=b'function FindProxyForURL(url,host){return "SOCKS5 127.0.0.1:1080; DIRECT";}'
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                r'Software\Microsoft\Windows\CurrentVersion\Internet Settings',0,
                winreg.KEY_QUERY_VALUE|winreg.KEY_SET_VALUE) as key:
            try: previous=winreg.QueryValueEx(key,'AutoConfigURL')
            except FileNotFoundError: previous=None
            try:
                winreg.SetValueEx(key,'AutoConfigURL',0,winreg.REG_SZ,fixture.url)
                yield fixture
            finally:
                if previous is None: winreg.DeleteValue(key,'AutoConfigURL')
                else: winreg.SetValueEx(key,'AutoConfigURL',0,previous[1],previous[0])
                try: restored=winreg.QueryValueEx(key,'AutoConfigURL')
                except FileNotFoundError: restored=None
                if restored!=previous: raise AssertionError('CI PAC configuration restoration failed')
    finally:fixture.close()


def verify_app(app, fixture):
    state=app.json('/api/network/state')
    if state['system_pac']['config_id']!=fixture.source.config_id or fixture.requests:
        raise AssertionError('actual WinHTTP configured-source preview failed or downloaded implicitly')
    saved=app.json('/api/network/system-pac',dict(revision=state['revision'],consent=True,
        config_id=state['system_pac']['config_id']))
    if saved['schema_version']!=4 or fixture.requests:raise AssertionError('PAC grant was not offline')
    checked=app.json('/api/network/pac/check',{'revision':saved['revision']})
    if (not checked['passed'] or checked['transport']!='loopback_socks5_proxy'
            or checked['target_requested'] or len(fixture.requests)!=1):
        raise AssertionError('frozen configured PAC fetch/evaluation worker failed')
    for _ in range(2):app.json('/api/network/state')
    if len(fixture.requests)!=1:raise AssertionError('PAC status silently fetched source')
    rollback=app.json('/api/network/proxy',dict(mode='auto',endpoint='',consent=False,revision=saved['revision']))
    if rollback['schema_version']!=2:raise AssertionError('configured PAC rollback failed')
