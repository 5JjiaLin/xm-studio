param([string]$Root = 'D:\XMStudio', [switch]$RecoverStopped)
. (Join-Path $PSScriptRoot 'common.ps1')
$Root = Resolve-CompanyRoot $Root
$release = Get-CompanyRelease $Root
$env:PYTHONUTF8 = '1'
$checkStarted = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
& $release.Python (Join-Path $release.Path 'scripts/company_health.py') --root $Root
$checkExit = $LASTEXITCODE
if ($RecoverStopped -and -not (Test-Path -LiteralPath (Join-Path $Root 'ops/maintenance.json'))) {
    $state = Get-Content -LiteralPath (Join-Path $Root 'ops/last-health.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($state.checked_at -lt $checkStarted -or -not $state.stopped) { throw 'Missing fresh process state; recovery aborted.' }
    $settings = Get-Content -LiteralPath (Join-Path $Root 'config/deployment.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    # Only restart absent processes. A live but unhealthy process needs diagnosis.
    foreach ($component in @('app','caddy')) {
        if ($state.stopped.$component -is [bool] -and $state.stopped.$component -eq $true) {
            $taskName = $settings.task_prefix + '-' + $component
            $task = Get-ScheduledTask -TaskName $taskName
            $taskInfo = Get-ScheduledTaskInfo -TaskName $taskName
            # A maintenance controller can restart a detached host. If that
            # host later exits, reattach through the registered task only when
            # its previous run ended cleanly. Never reset a failed retry series.
            if ($task.State -eq 'Ready' -and $taskInfo.LastTaskResult -eq 0) {
                Start-ScheduledTask -TaskName $taskName
            } else {
                Write-Warning ('Automatic recovery deferred; inspect scheduled task: ' + $taskName)
            }
        }
    }
}
exit $checkExit
