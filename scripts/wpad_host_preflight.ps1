param([Parameter(Mandatory=$true)][string]$OutputPath)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest

# Read-only host observation; never a product/WPAD success certificate.
if (-not [Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([Runtime.InteropServices.OSPlatform]::Windows) -or
    $env:CI -cne 'true' -or $env:GITHUB_ACTIONS -cne 'true' -or
    $env:RUNNER_ENVIRONMENT -cne 'github-hosted' -or $env:RUNNER_OS -cne 'Windows' -or
    $env:GITHUB_REPOSITORY -cne 'saksim/vibe_job_radar') {
    throw 'hosted_Windows_CI_required'
}
$wpadRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\')
$wpadOutput = [IO.Path]::GetFullPath($OutputPath)
if (-not $wpadOutput.StartsWith($wpadRoot + '\', [StringComparison]::OrdinalIgnoreCase) -or
    (Test-Path -LiteralPath $wpadOutput)) {
    throw 'fresh_output_inside_project_required'
}
$wpadParent = [IO.Path]::GetDirectoryName($wpadOutput)
$wpadCursor = $wpadParent
while ($wpadCursor -ne $wpadRoot) {
    if ((Test-Path -LiteralPath $wpadCursor) -and
        ((Get-Item -LiteralPath $wpadCursor -Force -ErrorAction Stop).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'output_reparse_path_refused'
    }
    $wpadCursor = [IO.Path]::GetDirectoryName($wpadCursor)
    if (-not $wpadCursor) { throw 'invalid_output_ancestry' }
}
$wpadState = [ordered]@{
    schema_version=1; kind='windows-wpad-host-preflight'; observation_complete=$false
    actual_wpad_discovery=$false; product_qualification=$false
    registry_or_dns_changes=$false; external_requests_sent_by_probe=0
    administrator=$false; policy_known=$false; policy_present=$false; policy_disabled=$null
    service_known=$false; service_start_type=$null; service_status=$null
    suffix_metadata=@(); usable_suffix_count=0; namespace_conflicts=0; nrpt_rule_count=0
    ports=@(); errors=@(); http_listeners=@(); http_listener_read_known=$false
    selected_http_loopback=$null; ready_for_owned_fixture_setup=$false
}
$wpadErrors = [Collections.Generic.List[string]]::new()
try {
    $wpadIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $wpadPrincipal = [Security.Principal.WindowsPrincipal]::new($wpadIdentity)
        $wpadState.administrator = $wpadPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    } finally { $wpadIdentity.Dispose() }
} catch { $wpadErrors.Add('administrator_read_failed') }
try {
    $wpadHive = [Microsoft.Win32.RegistryKey]::OpenBaseKey(
        [Microsoft.Win32.RegistryHive]::LocalMachine,[Microsoft.Win32.RegistryView]::Registry64)
    try {
        $wpadKey = $wpadHive.OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings\WinHttp',$false)
        try {
            $wpadValue = if ($wpadKey) { $wpadKey.GetValue('DisableWpad',$null) } else { $null }
            $wpadState.policy_present = $null -ne $wpadValue
            if ($null -eq $wpadValue) {
                $wpadState.policy_disabled=$false; $wpadState.policy_known=$true
            } elseif ($wpadKey.GetValueKind('DisableWpad') -eq [Microsoft.Win32.RegistryValueKind]::DWord) {
                $wpadState.policy_disabled=([int64]$wpadValue -ne 0); $wpadState.policy_known=$true
            } else { $wpadErrors.Add('unexpected_policy_value_kind') }
        } finally { if ($wpadKey) { $wpadKey.Dispose() } }
    } finally { $wpadHive.Dispose() }
} catch { $wpadErrors.Add('wpad_policy_read_failed') }
try {
    $wpadService = Get-Service -Name 'WinHttpAutoProxySvc' -ErrorAction Stop
    $wpadState.service_start_type=[string]$wpadService.StartType
    $wpadState.service_status=[string]$wpadService.Status
    $wpadState.service_known=$true
} catch { $wpadErrors.Add('service_read_failed') }
$wpadSuffixes = @()
try {
    $wpadGlobal = Get-DnsClientGlobalSetting -ErrorAction Stop
    $wpadConnections = @(Get-DnsClient -ErrorAction Stop)
    $wpadPrimary = [Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().DomainName
    $wpadRaw = @($wpadPrimary) + @($wpadGlobal.SuffixSearchList) + @($wpadConnections.ConnectionSpecificSuffix)
    $wpadSuffixes = @($wpadRaw | Where-Object { $_ -is [string] -and $_.Trim() } |
        ForEach-Object { $_.Trim().TrimEnd('.').ToLowerInvariant() } | Sort-Object -Unique |
        Where-Object { $_.Length -le 248 -and $_ -match '^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$' })
    $wpadState.usable_suffix_count=$wpadSuffixes.Count
    $wpadState.suffix_metadata=@(foreach ($wpadSuffix in $wpadSuffixes) {
        $wpadHash=[Security.Cryptography.SHA256]::Create()
        try {
            $wpadDigest=[BitConverter]::ToString($wpadHash.ComputeHash([Text.Encoding]::UTF8.GetBytes($wpadSuffix))).Replace('-','').ToLowerInvariant()
            [ordered]@{sha256=$wpadDigest; length=$wpadSuffix.Length; labels=$wpadSuffix.Split('.').Count}
        } finally { $wpadHash.Dispose() }
    })
} catch { $wpadErrors.Add('dns_suffix_read_failed') }
try {
    $wpadRules = @(Get-DnsClientNrptRule -ErrorAction Stop)
    $wpadState.nrpt_rule_count=$wpadRules.Count
    foreach ($wpadRule in $wpadRules) {
        foreach ($wpadNamespace in @($wpadRule.Namespace)) {
            $wpadScope=([string]$wpadNamespace).ToLowerInvariant()
            foreach ($wpadSuffix in $wpadSuffixes) {
                $wpadName='wpad.'+$wpadSuffix
                if ($wpadScope -eq '.' -or $wpadScope -eq $wpadName -or
                    ($wpadScope.StartsWith('.') -and $wpadName.EndsWith($wpadScope))) {
                    $wpadState.namespace_conflicts++
                }
            }
        }
    }
} catch { $wpadErrors.Add('nrpt_read_failed') }
$wpadSockets=[Collections.Generic.List[Net.Sockets.Socket]]::new()
try {
    $wpadState.ports=@(foreach ($wpadPort in @(
        @{address='127.0.0.1'; port=53; protocol='udp'; type=[Net.Sockets.SocketType]::Dgram; proto=[Net.Sockets.ProtocolType]::Udp},
        @{address='127.0.0.1'; port=53; protocol='tcp'; type=[Net.Sockets.SocketType]::Stream; proto=[Net.Sockets.ProtocolType]::Tcp},
        @{address='127.0.0.1'; port=80; protocol='tcp'; type=[Net.Sockets.SocketType]::Stream; proto=[Net.Sockets.ProtocolType]::Tcp},
        @{address='127.0.0.2'; port=80; protocol='tcp'; type=[Net.Sockets.SocketType]::Stream; proto=[Net.Sockets.ProtocolType]::Tcp})) {
        $wpadBound=$false; $wpadCode=$null
        try {
            $wpadSocket=[Net.Sockets.Socket]::new([Net.Sockets.AddressFamily]::InterNetwork,$wpadPort.type,$wpadPort.proto)
            $wpadSockets.Add($wpadSocket)
            $wpadSocket.ExclusiveAddressUse=$true
            $wpadSocket.Bind([Net.IPEndPoint]::new([Net.IPAddress]::Parse($wpadPort.address),$wpadPort.port))
            $wpadBound=$true
        } catch {
            $wpadFailure=$_.Exception
            while ($wpadFailure -and $wpadFailure -isnot [Net.Sockets.SocketException]) { $wpadFailure=$wpadFailure.InnerException }
            $wpadCode=if ($wpadFailure) { [string]$wpadFailure.SocketErrorCode } else { 'loopback_bind_failed' }
        }
        [ordered]@{address=$wpadPort.address; port=$wpadPort.port; protocol=$wpadPort.protocol; bound=$wpadBound; error=$wpadCode}
    })
} finally { foreach ($wpadSocket in $wpadSockets) { $wpadSocket.Dispose() } }

try {
    $wpadTcp = @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq 80 })
    $wpadState.http_listeners=@(foreach ($wpadConnection in $wpadTcp) {
        $wpadProcessClass='unavailable'
        try {
            $wpadProcessName=[string](Get-Process -Id $wpadConnection.OwningProcess -ErrorAction Stop).ProcessName
            $wpadProcessClass=if ($wpadProcessName -in @('System','w3wp','svchost','nginx','httpd','node','dotnet','iisexpress')) { $wpadProcessName } else { 'other' }
        } catch {}
        $wpadListenIP=[Net.IPAddress]::Parse($wpadConnection.LocalAddress)
        [ordered]@{
            family=[string]$wpadListenIP.AddressFamily
            loopback=[Net.IPAddress]::IsLoopback($wpadListenIP)
            wildcard=($wpadConnection.LocalAddress -in @('0.0.0.0','::'))
            process_class=$wpadProcessClass
        }
    })
    $wpadState.http_listener_read_known=$true
} catch { $wpadErrors.Add('http_listener_read_failed') }
$wpadAvailableHTTP=@($wpadState.ports | Where-Object { $_.port -eq 80 -and $_.bound })
if ($wpadAvailableHTTP.Count -gt 0) { $wpadState.selected_http_loopback=$wpadAvailableHTTP[0].address }

$wpadState.errors=@($wpadErrors.ToArray())
$wpadState.observation_complete=($wpadErrors.Count -eq 0)
$wpadState.ready_for_owned_fixture_setup=(
    $wpadState.observation_complete -and $wpadState.administrator -and
    $wpadState.policy_known -and -not $wpadState.policy_disabled -and
    $wpadState.service_known -and $wpadState.service_start_type -ne 'Disabled' -and
    $wpadState.usable_suffix_count -gt 0 -and $wpadState.namespace_conflicts -eq 0 -and
    @($wpadState.ports | Where-Object { $_.port -eq 53 -and -not $_.bound }).Count -eq 0 -and
    $null -ne $wpadState.selected_http_loopback)
New-Item -ItemType Directory -Path $wpadParent -Force -ErrorAction Stop | Out-Null
[IO.File]::WriteAllText($wpadOutput,($wpadState | ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
$wpadState | ConvertTo-Json -Depth 8
if (-not $wpadState.observation_complete) { exit 1 }
