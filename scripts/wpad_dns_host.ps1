param([Parameter(Mandatory=$true)][ValidateSet('inspect','rules','add','remove')][string]$Action)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
Set-StrictMode -Version Latest
# All names and policy objects are private parent/child IPC, never CI output.
if (-not [Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([Runtime.InteropServices.OSPlatform]::Windows) -or
    $env:CI -cne 'true' -or $env:GITHUB_ACTIONS -cne 'true' -or
    $env:RUNNER_ENVIRONMENT -cne 'github-hosted' -or $env:RUNNER_OS -cne 'Windows' -or
    $env:GITHUB_REPOSITORY -cne 'saksim/vibe_job_radar') { throw 'hosted_Windows_CI_required' }

function Get-OwnedWpadNames {
    $wpadGlobal=Get-DnsClientGlobalSetting -ErrorAction Stop
    $wpadConnections=@(Get-DnsClient -ErrorAction Stop)
    $wpadPrimary=[Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().DomainName
    $wpadRaw=@($wpadPrimary)+@($wpadGlobal.SuffixSearchList)+@($wpadConnections.ConnectionSpecificSuffix)
    $wpadSuffixes=@($wpadRaw | Where-Object { $_ -is [string] -and $_.Trim() } |
        ForEach-Object { $_.Trim().TrimEnd('.').ToLowerInvariant() } | Sort-Object -Unique |
        Where-Object { $_.Length -le 248 -and $_ -match '^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$' })
    if ($wpadSuffixes.Count -lt 1 -or $wpadSuffixes.Count -gt 8) { throw 'bounded_existing_suffixes_required' }
    @(@('wpad')+@($wpadSuffixes | ForEach-Object { 'wpad.'+$_ }) | Sort-Object -Unique)
}
function Get-WpadRules {
    @(Get-DnsClientNrptRule -ErrorAction Stop | Sort-Object Name | ForEach-Object {
        # Get-DnsClientNrptRule exposes these public properties. Do not
        # assume every adapted property also appears in CimInstanceProperties.
        [ordered]@{Name=[string]$_.Name;Comment=[string]$_.Comment
            DisplayName=[string]$_.DisplayName;Namespace=@($_.Namespace | Sort-Object)
            NameServers=@($_.NameServers | ForEach-Object { [string]$_ })
            NameServerValueKinds=@($_.NameServers | ForEach-Object {
                if ($_ -is [Net.IPAddress]) { 'ip_address' } elseif ($_ -is [string]) { 'string' } else { 'other' }
            })}
    })
}
function Assert-WpadOwnedRule($wpadRow,$wpadInput) {
    if ($wpadRow.Name -cne $wpadInput.id -or $wpadRow.Comment -cne $wpadInput.tag -or
        $wpadRow.DisplayName -cne $wpadInput.tag -or
        (@($wpadRow.Namespace | Sort-Object) -join '|') -cne (@($wpadInput.names | Sort-Object) -join '|') -or
        (@($wpadRow.NameServers) -join '|') -cne '127.0.0.1') { throw 'unowned_rule_refused' }
}
try {
    $wpadLine=[Console]::In.ReadLine()
    if ($null -eq $wpadLine -or $wpadLine.Length -gt 16384) { throw 'bounded_input_required' }
    $wpadInput=ConvertFrom-Json -InputObject $wpadLine -ErrorAction Stop
    if ($Action -eq 'rules') {
        $wpadResult=@(Get-WpadRules)
    } elseif ($Action -eq 'inspect') {
        $wpadIdentity=[Security.Principal.WindowsIdentity]::GetCurrent()
        try { $wpadAdmin=([Security.Principal.WindowsPrincipal]::new($wpadIdentity)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) }
        finally { $wpadIdentity.Dispose() }
        $wpadHive=[Microsoft.Win32.RegistryKey]::OpenBaseKey([Microsoft.Win32.RegistryHive]::LocalMachine,[Microsoft.Win32.RegistryView]::Registry64)
        try {
            $wpadKey=$wpadHive.OpenSubKey('SOFTWARE\Microsoft\Windows\CurrentVersion\Internet Settings\WinHttp',$false)
            try {
                $wpadValue=if ($wpadKey) { $wpadKey.GetValue('DisableWpad',$null) } else { $null }
                $wpadPolicyAllowed=($null -eq $wpadValue -or
                    ($wpadKey.GetValueKind('DisableWpad') -eq [Microsoft.Win32.RegistryValueKind]::DWord -and [int64]$wpadValue -eq 0))
            } finally { if ($wpadKey) { $wpadKey.Dispose() } }
        } finally { $wpadHive.Dispose() }
        $wpadService=Get-Service -Name 'WinHttpAutoProxySvc' -ErrorAction Stop
        $wpadResult=@{admin=$wpadAdmin;policy_allowed=$wpadPolicyAllowed;service_enabled=([string]$wpadService.StartType -ne 'Disabled')
            names=@(Get-OwnedWpadNames);rules=@(Get-WpadRules)}
    } else {
        if ($wpadInput.tag -cnotmatch '^vjr-wpad-[0-9a-f]{32}$') { throw 'invalid_ownership_tag' }
        $wpadNames=@($wpadInput.names | Sort-Object -Unique)
        if ($Action -eq 'add') {
            if (($wpadNames -join '|') -cne (@(Get-OwnedWpadNames) -join '|')) { throw 'existing_exact_names_required' }
            if (@(Get-WpadRules).Count -ne 0) { throw 'existing_NRPT_policy_refused' }
            $null=Add-DnsClientNrptRule -Namespace $wpadNames -NameServers @('127.0.0.1') -Comment $wpadInput.tag -DisplayName $wpadInput.tag -ErrorAction Stop
            $wpadResult=@(Get-WpadRules)
        } else {
            if ($wpadInput.id -isnot [string] -or $wpadInput.id -cnotmatch '^\{?[0-9a-fA-F-]{36}\}?$') { throw 'invalid_rule_identity' }
            $wpadRows=@(Get-DnsClientNrptRule -ErrorAction Stop | Where-Object { $_.Name -ceq $wpadInput.id })
            if ($wpadRows.Count -ne 1) { throw 'owned_rule_identity_missing' }
            Assert-WpadOwnedRule $wpadRows[0] $wpadInput
            Remove-DnsClientNrptRule -Name $wpadInput.id -Force -Confirm:$false -ErrorAction Stop
            $wpadResult=@{removed=$true}
        }
    }
    [Console]::Out.WriteLine((ConvertTo-Json -InputObject $wpadResult -Depth 16 -Compress))
    [Console]::Out.Flush()
} catch {
    # Caller records only action + finite failure type, not policy or host data.
    $wpadFailure=[ordered]@{error='host_operation_failed';action=$Action
        line=[int]$_.InvocationInfo.ScriptLineNumber}
    [Console]::Out.WriteLine((ConvertTo-Json -InputObject $wpadFailure -Compress))
    exit 1
}
