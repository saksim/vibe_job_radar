"""Validate explicit frozen NTLM proof; a boolean flag alone is insufficient."""
import json
import subprocess

COMPONENT_FIELDS = ('correct_accepted', 'wrong_rejected', 'ntlm_v2',
                    'workstation_omitted', 'worker_cleanup_verified')
FIELDS = ('credentials_reopened', 'clear_stops_route', 'original_report_retained')


def valid_component(value, *, require_frozen=True):
    return (isinstance(value, dict) and set(value) == set(COMPONENT_FIELDS) | {
        'schema_version', 'success', 'library', 'frozen_executable', 'network_scope', 'code'}
        and type(value['schema_version']) is int and value['schema_version'] == 1
        and value['success'] is True and value['library'] == 'pyspnego:0.12.4'
        and type(value['frozen_executable']) is bool
        and (not require_frozen or value['frozen_executable'] is True)
        and value['network_scope'] == 'offline-artificial-protocol-no-proxy-connection'
        and value['code'] == 'verified' and all(value[key] is True for key in COMPONENT_FIELDS))


def valid_evidence(value):
    return (isinstance(value, dict) and set(value) == set(FIELDS) | {'component'}
            and valid_component(value['component']) and all(value[key] is True for key in FIELDS))


def verify_component(exe, cwd, env):
    process = subprocess.run([str(exe), '--ntlm-component-check'], cwd=cwd, env=env,
        stdin=subprocess.DEVNULL, capture_output=True, timeout=35,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if process.returncode or len(process.stdout) > 16384:
        raise AssertionError('frozen NTLM component check failed')
    value = json.loads(process.stdout.decode('utf-8'))
    if not valid_component(value):
        raise AssertionError('frozen NTLM component evidence incomplete')
    return value
