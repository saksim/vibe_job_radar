"""Windows publisher signature evidence; checks never execute the target file."""
from __future__ import annotations
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

HEX = re.compile(r'[0-9a-f]{64}')
MESSAGE = '发行者签名核验失败；未启动新版，已有程序和工作区保留。'


class SignatureError(ValueError):
    pass


def require(condition):
    if not condition:
        raise SignatureError(MESSAGE)


def file_sha256(path):
    path = Path(path)
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 1024**3)
    with path.open('rb') as stream:
        value = hashlib.sha256()
        for block in iter(lambda: stream.read(1024*1024), b''):
            value.update(block)
    return value.hexdigest()


def valid_evidence(value):
    if not isinstance(value, dict) or type(value.get('schema_version')) is not int or value['schema_version'] != 1:
        return False
    if value.get('status') == 'unsigned':
        return value == {'schema_version': 1, 'status': 'unsigned'}
    fields = {'schema_version', 'status', 'executable_sha256', 'certificate_sha256',
              'timestamp_certificate_sha256', 'subject', 'issuer'}
    return (set(value) == fields and value.get('status') == 'authenticode_verified'
            and all(isinstance(value[k], str) and HEX.fullmatch(value[k])
                    for k in ('executable_sha256', 'certificate_sha256', 'timestamp_certificate_sha256'))
            and all(isinstance(value[k], str) and 0 < len(value[k]) <= 2048
                    and all(ord(c) >= 32 for c in value[k]) for k in ('subject', 'issuer')))


def evidence(value=None):
    """Missing metadata is supported only as the legacy unsigned format."""
    if value is None:
        return {'schema_version': 1, 'status': 'unsigned'}
    require(valid_evidence(value))
    return dict(value)


def is_signed(value):
    return evidence(value)['status'] == 'authenticode_verified'


def run_powershell(script, variables, *, pwsh=False, timeout=45):
    require(os.name == 'nt')
    if pwsh:
        # Build-only dependency. User update verification uses the OS PowerShell.
        import shutil
        command = shutil.which('pwsh.exe')
    else:
        command = str(Path(os.environ.get('SystemRoot', r'C:\Windows')) /
                      'System32/WindowsPowerShell/v1.0/powershell.exe')
    require(command and Path(command).is_file())
    prefix = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false); "
    encoded = base64.b64encode((prefix + script).encode('utf-16-le')).decode('ascii')
    env = {**os.environ, **variables}
    if not pwsh:
        # A parent PowerShell 7 can export its incompatible Core modules to
        # Windows PowerShell 5.1. Load only the OS's matching built-in modules.
        env = {k: v for k, v in env.items() if k.lower() != 'psmodulepath'}
        env['PSMODULEPATH'] = str(Path(command).parent / 'Modules')
    try:
        result = subprocess.run([command, '-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                env=env, capture_output=True, timeout=timeout,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except (OSError, subprocess.TimeoutExpired):
        raise SignatureError(MESSAGE) from None
    # Never include tool output (possibly provider diagnostics or credentials) in errors.
    require(result.returncode == 0 and len(result.stdout) <= 65536)
    return result.stdout.decode('utf-8-sig').strip()


PROBE = r"""
$s = Get-AuthenticodeSignature -LiteralPath $env:VJR_SIGNATURE_TARGET
function CertHash($cert) {
    if ($null -eq $cert) { return '' }
    $hash = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hash.ComputeHash($cert.RawData))).Replace('-','').ToLowerInvariant() }
    finally { $hash.Dispose() }
}
@{
    status = [string]$s.Status
    kind = [string]$s.SignatureType
    subject = if ($s.SignerCertificate) { $s.SignerCertificate.Subject } else { '' }
    issuer = if ($s.SignerCertificate) { $s.SignerCertificate.Issuer } else { '' }
    certificate_sha256 = CertHash $s.SignerCertificate
    timestamp_certificate_sha256 = CertHash $s.TimeStamperCertificate
} | ConvertTo-Json -Compress
"""


def inspect(path):
    before = file_sha256(path)
    raw = json.loads(run_powershell(PROBE, {'VJR_SIGNATURE_TARGET': str(Path(path).resolve())}))
    require(isinstance(raw, dict) and file_sha256(path) == before)
    if raw.get('status') == 'NotSigned':
        return evidence()
    require(raw.get('status') == 'Valid' and raw.get('kind') == 'Authenticode')
    result = dict(schema_version=1, status='authenticode_verified', executable_sha256=before,
                  **{key: raw.get(key) for key in ('subject', 'issuer', 'certificate_sha256',
                                                  'timestamp_certificate_sha256')})
    require(valid_evidence(result))
    return result


def verify(path, expected):
    expected = evidence(expected)
    require(is_signed(expected))
    require(file_sha256(path) == expected['executable_sha256'])
    require(inspect(path) == expected)
    return expected


def verify_bundle(directory, selected):
    """Bind public metadata to embedded identity, then verify actual signed bytes."""
    expected = evidence(selected.get('signing'))
    require(type(selected.get('signed', False)) is bool
            and selected.get('signed', False) == is_signed(expected))
    path = Path(directory) / 'PORTABLE.json'
    require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 100000)
    metadata = json.loads(path.read_bytes())
    require(isinstance(metadata, dict) and evidence(metadata.get('signing')) == expected)
    if is_signed(expected):
        require(metadata.get('runtime_source', {}).get('executable_sha256') == expected['executable_sha256'])
        verify(Path(directory) / 'VibeJobRadar.exe', expected)
    return expected
