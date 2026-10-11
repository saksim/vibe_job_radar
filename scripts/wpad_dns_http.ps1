param()
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
Set-StrictMode -Version Latest
if (-not [Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([Runtime.InteropServices.OSPlatform]::Windows) -or
    $env:CI -cne 'true' -or $env:GITHUB_ACTIONS -cne 'true' -or
    $env:RUNNER_ENVIRONMENT -cne 'github-hosted' -or $env:RUNNER_OS -cne 'Windows' -or
    $env:GITHUB_REPOSITORY -cne 'saksim/vibe_job_radar') { throw 'hosted_Windows_CI_required' }
# Console.In is a synchronized TextReader on Windows PowerShell 5.1 and may
# execute ReadLineAsync synchronously. Use an owned unsynchronized pipe reader
# so pending control input cannot block HTTP acceptance or the ready event.
$wpadInputReader=[IO.StreamReader]::new([Console]::OpenStandardInput(),[Text.Encoding]::UTF8)
function Send-WpadEvent($wpadEvent) {
    [Console]::Out.WriteLine((ConvertTo-Json -InputObject $wpadEvent -Compress -Depth 5))
    [Console]::Out.Flush()
}
function Read-WpadCommand($wpadTask) {
    $wpadLine=$wpadTask.GetAwaiter().GetResult()
    if ($null -eq $wpadLine) { return @{command='stop'} }
    if ($wpadLine.Length -gt 8192) { throw 'oversized_command' }
    ConvertFrom-Json -InputObject $wpadLine -ErrorAction Stop
}
function Get-WpadListeners {
    @(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object { $_.LocalPort -eq 80 } |
        Sort-Object LocalAddress,OwningProcess | ForEach-Object {
            [ordered]@{address=[string]$_.LocalAddress;process=[int64]$_.OwningProcess}
        }) | ConvertTo-Json -Compress -Depth 3
}
$wpadListener=$null; $wpadContext=$null; $wpadContextTask=$null
$wpadBefore=$null; $wpadSequence=0; $wpadStopped=$false
$wpadResult=[ordered]@{event='closed';success=$false;stage='input';requests=0;cleanup_errors=0
    listener_closed=$false;existing_listener_preserved=$false;error=$null}
try {
    $wpadInput=Read-WpadCommand ($wpadInputReader.ReadLineAsync())
    $wpadNames=@($wpadInput.names)
    if ($wpadNames.Count -lt 2 -or $wpadNames.Count -gt 9 -or
        @($wpadNames | Sort-Object -Unique).Count -ne $wpadNames.Count -or 'wpad' -cnotin $wpadNames) {
        throw 'invalid_names'
    }
    foreach ($wpadName in $wpadNames) {
        if ($wpadName -isnot [string] -or $wpadName.Length -gt 253 -or
            ($wpadName -cne 'wpad' -and $wpadName -cnotmatch '^wpad(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?){2,}$')) {
            throw 'invalid_name'
        }
    }
    $wpadBefore=Get-WpadListeners
    $wpadListener=[Net.HttpListener]::new()
    $wpadListener.AuthenticationSchemes=[Net.AuthenticationSchemes]::Anonymous
    foreach ($wpadName in $wpadNames) { $wpadListener.Prefixes.Add('http://'+$wpadName+':80/') }
    $wpadResult.stage='register'
    $wpadListener.Start()
    $wpadContextTask=$wpadListener.GetContextAsync()
    $wpadCommandTask=$wpadInputReader.ReadLineAsync()
    Send-WpadEvent @{event='ready';explicit_prefixes=$wpadNames.Count}
    $wpadTimer=[Diagnostics.Stopwatch]::StartNew()
    $wpadResult.stage='serve'
    while ($wpadTimer.Elapsed.TotalSeconds -lt 120) {
        if ($wpadCommandTask.IsCompleted) {
            $wpadCommand=Read-WpadCommand $wpadCommandTask
            if ($wpadCommand.command -cne 'stop') { throw 'unexpected_command' }
            $wpadStopped=$true; break
        }
        if (-not $wpadContextTask.Wait(100)) { continue }
        $wpadContext=$wpadContextTask.GetAwaiter().GetResult()
        $wpadSequence++
        $wpadResult.requests=$wpadSequence
        if ($wpadSequence -gt 16 -or $wpadContext.Request.HttpMethod -cne 'GET' -or
            $wpadContext.Request.Url.Host -cnotin $wpadNames -or $wpadContext.Request.RawUrl -cne '/wpad.dat' -or
            -not [Net.IPAddress]::IsLoopback($wpadContext.Request.RemoteEndPoint.Address) -or
            -not [Net.IPAddress]::IsLoopback($wpadContext.Request.LocalEndPoint.Address)) {
            $wpadContext.Response.StatusCode=403
            $wpadContext.Response.Close();$wpadContext=$null
            throw 'unexpected_request'
        }
        $wpadAgent=if ($wpadContext.Request.UserAgent -ceq 'VibeJobRadar-PAC') { 'project_pac' } elseif (-not $wpadContext.Request.UserAgent) { 'missing' } else { 'other' }
        # Keep this connection open until the parent observes its owner.
        # No raw header, URL, hostname, PID or other process data leaves IPC.
        Send-WpadEvent @{event='request';sequence=$wpadSequence;agent=$wpadAgent
            peer_port=$wpadContext.Request.RemoteEndPoint.Port;local_port=$wpadContext.Request.LocalEndPoint.Port}
        if (-not $wpadCommandTask.Wait(3000)) { throw 'observer_ack_timeout' }
        $wpadCommand=Read-WpadCommand $wpadCommandTask
        if ($wpadCommand.command -ceq 'stop') { $wpadStopped=$true;break }
        if ($wpadCommand.command -cne 'ack' -or $wpadCommand.sequence -ne $wpadSequence) { throw 'invalid_observer_ack' }
        $wpadCommandTask=$wpadInputReader.ReadLineAsync()
        # Ordinary hosts remain DIRECT. Only the fixed non-routable check name
        # yields SOCKS5; the product check connects to neither proxy nor target.
        $wpadBody=[Text.Encoding]::ASCII.GetBytes('function FindProxyForURL(url,host){if(host=="pac-check.invalid")return "SOCKS5 127.0.0.1:1080; DIRECT";return "DIRECT";}')
        $wpadContext.Response.StatusCode=200
        $wpadContext.Response.ContentType='application/x-ns-proxy-autoconfig'
        $wpadContext.Response.ContentLength64=$wpadBody.Length
        $wpadContext.Response.KeepAlive=$false
        $wpadContext.Response.OutputStream.Write($wpadBody,0,$wpadBody.Length)
        $wpadContext.Response.Close();$wpadContext=$null
        $wpadContextTask=$wpadListener.GetContextAsync()
    }
    if (-not $wpadStopped) { throw 'fixture_lifetime_exceeded' }
} catch {
    $wpadResult.error='owned_http_fixture_failed'
} finally {
    if ($wpadContext) { try { $wpadContext.Response.Close() } catch { $wpadResult.cleanup_errors++ } }
    if ($wpadListener) {
        try { $wpadListener.Close() } catch { $wpadResult.cleanup_errors++ }
        $wpadResult.listener_closed=-not $wpadListener.IsListening
    } else { $wpadResult.listener_closed=$true }
    if ($wpadContextTask -and $wpadContextTask.IsFaulted) { $null=$wpadContextTask.Exception }
    try { $wpadInputReader.Dispose() } catch { $wpadResult.cleanup_errors++ }
    try { $wpadResult.existing_listener_preserved=($null -ne $wpadBefore -and $wpadBefore -ceq (Get-WpadListeners)) }
    catch { $wpadResult.cleanup_errors++ }
}
$wpadResult.success=($wpadStopped -and $null -eq $wpadResult.error -and $wpadResult.cleanup_errors -eq 0 -and
    $wpadResult.listener_closed -and $wpadResult.existing_listener_preserved)
if ($wpadResult.success) { $wpadResult.stage='complete' }
Send-WpadEvent $wpadResult
if (-not $wpadResult.success) { exit 1 }
