"""Explicit candidate signing, before hashing and executable qualification."""
from __future__ import annotations
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit
from vibe_job_radar.publisher_signing import inspect, is_signed, require, run_powershell

AZURE_MODULE_VERSION = '0.1.20'
AZURE_SIGN = r"""
Import-Module ArtifactSigning -RequiredVersion 0.1.20
$params = @{
    Endpoint = $env:VJR_AZURE_SIGN_ENDPOINT
    CodeSigningAccountName = $env:VJR_AZURE_SIGN_ACCOUNT
    CertificateProfileName = $env:VJR_AZURE_SIGN_PROFILE
    Files = $env:VJR_SIGNATURE_TARGET
    FileDigest = 'SHA256'
    TimestampRfc3161 = 'http://timestamp.acs.microsoft.com'
    TimestampDigest = 'SHA256'
    ExcludeEnvironmentCredential = $true
    ExcludeWorkloadIdentityCredential = $true
    ExcludeManagedIdentityCredential = $true
    ExcludeSharedTokenCacheCredential = $true
    ExcludeVisualStudioCredential = $true
    ExcludeVisualStudioCodeCredential = $true
    ExcludeAzureCliCredential = $false
    ExcludeAzurePowerShellCredential = $true
    ExcludeAzureDeveloperCliCredential = $true
    ExcludeInteractiveBrowserCredential = $true
    Timeout = 180
}
Invoke-ArtifactSigning @params
"""


def sign_candidate(executable, *, environ=None):
    env = dict(os.environ if environ is None else environ)
    mode = env.get('VJR_WINDOWS_SIGNING_MODE', 'unsigned') or 'unsigned'
    require(mode in {'unsigned', 'azure', 'store'})
    # PRs, forks and non-main dispatches must never consume signing identity.
    if mode != 'unsigned' and env.get('GITHUB_ACTIONS') == 'true':
        require(env.get('GITHUB_EVENT_NAME') == 'push' and env.get('GITHUB_REF') == 'refs/heads/main'
                and env.get('GITHUB_REPOSITORY') == 'saksim/vibe_job_radar')
    if mode == 'azure':
        endpoint = urlsplit(env.get('VJR_AZURE_SIGN_ENDPOINT', ''))
        require(endpoint.scheme == 'https' and not endpoint.username and not endpoint.password
                and endpoint.port in (None, 443) and not endpoint.query and not endpoint.fragment
                and endpoint.path in ('', '/') and isinstance(endpoint.hostname, str)
                and re.fullmatch(r'[a-z0-9-]+\.codesigning\.azure\.net', endpoint.hostname))
        require(all(re.fullmatch(r'[A-Za-z0-9-]{1,100}', env.get(key, ''))
                    for key in ('VJR_AZURE_SIGN_ACCOUNT', 'VJR_AZURE_SIGN_PROFILE')))
        require(0 < len(env.get('VJR_SIGNING_SUBJECT', '')) <= 2048)
        run_powershell(AZURE_SIGN, {**env, 'VJR_SIGNATURE_TARGET': str(Path(executable).resolve())},
                       pwsh=True, timeout=240)
    elif mode == 'store':
        tool = Path(env.get('VJR_SIGNTOOL', ''))
        thumbprint = env.get('VJR_SIGNING_THUMBPRINT', '')
        stamp = urlsplit(env.get('VJR_SIGNING_TIMESTAMP_URL', ''))
        require(tool.is_absolute() and tool.is_file() and tool.name.lower() == 'signtool.exe'
                and re.fullmatch('[0-9A-Fa-f]{40}', thumbprint)
                and stamp.scheme in {'https', 'http'} and stamp.hostname
                and not stamp.username and not stamp.password and not stamp.fragment
                and 0 < len(env.get('VJR_SIGNING_SUBJECT', '')) <= 2048)
        try:
            signed = subprocess.run([str(tool), 'sign', '/s', 'My', '/sha1', thumbprint,
                                     '/fd', 'SHA256', '/tr', stamp.geturl(), '/td', 'SHA256',
                                     str(Path(executable).resolve())], env=env, capture_output=True, timeout=240,
                                    creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            require(signed.returncode == 0)
        except (OSError, subprocess.TimeoutExpired):
            require(False)
    result = inspect(executable)
    require(is_signed(result) == (mode != 'unsigned'))
    if mode != 'unsigned':
        require(result['subject'] == env['VJR_SIGNING_SUBJECT'])
    return result
