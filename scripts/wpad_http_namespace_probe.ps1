param([Parameter(Mandatory=$true)][string]$OutputPath)
$ErrorActionPreference='Stop'
Set-StrictMode -Version Latest

# Hosted owned HTTP namespace control; never a product/WPAD certificate.
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

$wpadControl = [ordered]@{
    schema_version=1; kind='windows-wpad-http-namespace-control'; success=$false
    actual_wpad_discovery=$false; product_qualification=$false
    registry_dns_firewall_or_other_service_changes=$false; external_requests=0
    loopback_requests=0; explicit_hostname_only=$true; registrations_started=0
    request_routed_to_owned_listener=$false; response_matched=$false
    same_prefix_reused_after_close=$false; live_owned_listeners=0; cleanup_errors=0
    existing_listener_preserved=$false; error=$null; stage='precondition'
}
function Get-WpadHttpListeners {
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop |
        Where-Object { $_.LocalPort -eq 80 } |
        Sort-Object LocalAddress,OwningProcess |
        ForEach-Object { [ordered]@{address=[string]$_.LocalAddress; process=[int64]$_.OwningProcess} }) |
        ConvertTo-Json -Compress -Depth 3
}
$wpadOwned = [Collections.Generic.List[Net.HttpListener]]::new()
$wpadClient=$null; $wpadContextTask=$null; $wpadContext=$null
$wpadBefore=$null; $wpadPrimaryPassed=$false
try {
    $wpadBefore=Get-WpadHttpListeners
    $wpadRows=@($wpadBefore | ConvertFrom-Json)
    if ($wpadRows.Count -eq 0 -or
        @($wpadRows | Where-Object { $_.process -eq 4 -and $_.address -in @('0.0.0.0','::') }).Count -eq 0) {
        throw 'existing_system_http_listener_required'
    }
    $wpadIdentity=[Security.Principal.WindowsIdentity]::GetCurrent()
    try {
        $wpadPrincipal=[Security.Principal.WindowsPrincipal]::new($wpadIdentity)
        if (-not $wpadPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
            throw 'hosted_administrator_required'
        }
    } finally { $wpadIdentity.Dispose() }
    if (-not [Net.HttpListener]::IsSupported) { throw 'http_listener_unavailable' }
    $wpadNonce=[Guid]::NewGuid().ToString('N')
    $wpadOwnedName='vjr-wpad-'+$wpadNonce+'.invalid'
    $wpadOwnedPath='/vjr-control-'+$wpadNonce
    $wpadPrefix='http://'+$wpadOwnedName+':80/'
    $wpadListener=[Net.HttpListener]::new()
    $wpadOwned.Add($wpadListener)
    $wpadListener.AuthenticationSchemes=[Net.AuthenticationSchemes]::Anonymous
    $wpadListener.Prefixes.Add($wpadPrefix)
    $wpadControl.stage='register_explicit_hostname'
    $wpadListener.Start()
    $wpadControl.registrations_started++
    $wpadContextTask=$wpadListener.GetContextAsync()
    $wpadControl.stage='loopback_request'
    $wpadClient=[Net.Sockets.TcpClient]::new([Net.Sockets.AddressFamily]::InterNetwork)
    if (-not $wpadClient.ConnectAsync([Net.IPAddress]::Loopback,80).Wait(5000)) {
        throw 'loopback_connect_timeout'
    }
    $wpadStream=$wpadClient.GetStream()
    $wpadStream.ReadTimeout=5000; $wpadStream.WriteTimeout=5000
    $wpadCRLF=[string][char]13+[char]10
    $wpadRequest='GET '+$wpadOwnedPath+' HTTP/1.1'+$wpadCRLF+'Host: '+$wpadOwnedName+$wpadCRLF+'Connection: close'+$wpadCRLF+$wpadCRLF
    $wpadBytes=[Text.Encoding]::ASCII.GetBytes($wpadRequest)
    $wpadStream.Write($wpadBytes,0,$wpadBytes.Length)
    $wpadControl.loopback_requests++
    $wpadControl.stage='route_to_owned_namespace'
    if (-not $wpadContextTask.Wait(5000)) { throw 'owned_namespace_not_routed' }
    $wpadContext=$wpadContextTask.GetAwaiter().GetResult()
    if ($wpadContext.Request.HttpMethod -cne 'GET' -or
        $wpadContext.Request.Url.Host -cne $wpadOwnedName -or
        $wpadContext.Request.RawUrl -cne $wpadOwnedPath -or
        -not [Net.IPAddress]::IsLoopback($wpadContext.Request.RemoteEndPoint.Address)) {
        throw 'unexpected_owned_request'
    }
    $wpadControl.request_routed_to_owned_listener=$true
    $wpadBody=[Text.Encoding]::ASCII.GetBytes('VJR_OWNED_NAMESPACE_CONTROL')
    $wpadContext.Response.StatusCode=200
    $wpadContext.Response.ContentType='text/plain'
    $wpadContext.Response.ContentLength64=$wpadBody.Length
    $wpadContext.Response.KeepAlive=$false
    $wpadContext.Response.OutputStream.Write($wpadBody,0,$wpadBody.Length)
    $wpadContext.Response.Close()
    $wpadControl.stage='verify_response'
    $wpadReceived=[IO.MemoryStream]::new()
    try {
        $wpadBuffer=[byte[]]::new(1024)
        do {
            $wpadRead=$wpadStream.Read($wpadBuffer,0,$wpadBuffer.Length)
            if ($wpadRead -gt 0) { $wpadReceived.Write($wpadBuffer,0,$wpadRead) }
            if ($wpadReceived.Length -gt 16384) { throw 'response_limit_exceeded' }
        } while ($wpadRead -gt 0)
        $wpadReply=[Text.Encoding]::ASCII.GetString($wpadReceived.ToArray())
        if (-not $wpadReply.StartsWith('HTTP/1.1 200 ') -or
            -not $wpadReply.EndsWith($wpadCRLF+$wpadCRLF+'VJR_OWNED_NAMESPACE_CONTROL')) {
            throw 'control_response_mismatch'
        }
        $wpadControl.response_matched=$true
    } finally { $wpadReceived.Dispose() }
    $wpadClient.Dispose(); $wpadClient=$null
    $wpadControl.stage='close_and_reuse_own_prefix'
    $wpadListener.Close()
    if ($wpadListener.IsListening) { throw 'original_listener_not_closed' }
    $wpadSecond=[Net.HttpListener]::new()
    $wpadOwned.Add($wpadSecond)
    $wpadSecond.AuthenticationSchemes=[Net.AuthenticationSchemes]::Anonymous
    $wpadSecond.Prefixes.Add($wpadPrefix)
    $wpadSecond.Start()
    $wpadControl.registrations_started++
    $wpadControl.same_prefix_reused_after_close=$true
    $wpadSecond.Close()
    $wpadPrimaryPassed=$true
} catch {
    # Only fixed stages/error categories are retained; no raw headers, URL or exception text.
    $wpadControl.error='owned_namespace_control_failed'
    $wpadFailure=$_.Exception
    while ($wpadFailure -and $wpadFailure -isnot [Net.HttpListenerException] -and
           $wpadFailure -isnot [Net.Sockets.SocketException]) { $wpadFailure=$wpadFailure.InnerException }
    if ($wpadFailure -is [Net.HttpListenerException]) { $wpadControl.error='http_listener_error_'+$wpadFailure.NativeErrorCode }
    elseif ($wpadFailure -is [Net.Sockets.SocketException]) { $wpadControl.error='socket_'+[string]$wpadFailure.SocketErrorCode }
} finally {
    if ($wpadClient) { $wpadClient.Dispose() }
    if ($wpadContext) {
        try { $wpadContext.Response.Close() } catch { $wpadControl.cleanup_errors++ }
    }
    foreach ($wpadListener in $wpadOwned) {
        try { $wpadListener.Close() } catch { $wpadControl.cleanup_errors++ }
        if ($wpadListener.IsListening) { $wpadControl.live_owned_listeners++ }
    }
    if ($wpadContextTask -and $wpadContextTask.IsFaulted) { $null=$wpadContextTask.Exception }
    try {
        $wpadAfter=Get-WpadHttpListeners
        $wpadControl.existing_listener_preserved=($null -ne $wpadBefore -and $wpadBefore -ceq $wpadAfter)
    } catch { $wpadControl.cleanup_errors++ }
}
$wpadControl.success=($wpadPrimaryPassed -and $wpadControl.cleanup_errors -eq 0 -and
    $wpadControl.live_owned_listeners -eq 0 -and $wpadControl.existing_listener_preserved)
if ($wpadControl.success) { $wpadControl.stage='complete' }
New-Item -ItemType Directory -Path $wpadParent -Force -ErrorAction Stop | Out-Null
[IO.File]::WriteAllText($wpadOutput,($wpadControl | ConvertTo-Json -Depth 6),[Text.UTF8Encoding]::new($false))
$wpadControl | ConvertTo-Json -Depth 6
if (-not $wpadControl.success) { exit 1 }
