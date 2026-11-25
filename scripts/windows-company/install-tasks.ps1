param(
    [string]$Root = 'D:\XMStudio',
    [Parameter(Mandatory)][PSCredential]$Credential,
    [switch]$StartNow
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Root = Resolve-CompanyRoot $Root
$identity = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $identity.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run task installation in an elevated PowerShell.' }
$settings = Get-Content -LiteralPath (Join-Path $Root 'config/deployment.json') -Raw -Encoding UTF8 | ConvertFrom-Json
if (-not $settings.backup_unc -or -not $settings.backup_unc.StartsWith('\\')) { throw 'Set the confirmed independent backup UNC before registering production tasks.' }
if ($settings.task_prefix -notmatch '^[A-Za-z0-9_-]{1,40}$') { throw 'Invalid task prefix.' }
$release = Get-CompanyRelease $Root
if (-not $release.Path.StartsWith((Join-Path $Root 'releases') + '\', [StringComparison]::OrdinalIgnoreCase)) {
    throw 'Production tasks require a release inside the deployment releases directory.'
}
$account = $Credential.UserName
if ($account -ne $settings.service_account) { throw 'Credential must match deployment.json service_account.' }
# Stable wrappers read current-release.json on every start, including upgrades.
$wrapperSource = Join-Path $release.Path 'scripts/windows-company'
Get-ChildItem -LiteralPath $wrapperSource -Filter '*.ps1' | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $Root ('ops/' + $_.Name)) -Force
}
$shell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Password -RunLevel Limited
$persistent = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -DisallowHardTerminate `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable
$startup = New-ScheduledTaskTrigger -AtStartup
$startup.Delay = 'PT30S'
$plainPassword = $Credential.GetNetworkCredential().Password
try {
    foreach ($component in @('app','caddy')) {
        $arguments = '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Root "{1}" -Foreground -Component {2}' -f (Join-Path $Root 'ops/start.ps1'), $Root, $component
        $action = New-ScheduledTaskAction -Execute $shell -Argument $arguments -WorkingDirectory $Root
        $task = New-ScheduledTask -Action $action -Trigger $startup -Settings $persistent -Principal $principal
        Register-ScheduledTask -TaskName ($settings.task_prefix + '-' + $component) -InputObject $task -User $account -Password $plainPassword -Force | Out-Null
    }
    $backupAction = New-ScheduledTaskAction -Execute $shell -WorkingDirectory $Root -Argument (
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Root "{1}"' -f (Join-Path $Root 'ops/backup.ps1'), $Root)
    $backupTrigger = New-ScheduledTaskTrigger -Daily -At '02:00'
    $backupSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) -DisallowHardTerminate -StartWhenAvailable
    $backupTask = New-ScheduledTask -Action $backupAction -Trigger $backupTrigger -Settings $backupSettings -Principal $principal
    Register-ScheduledTask -TaskName ($settings.task_prefix + '-backup') -InputObject $backupTask -User $account -Password $plainPassword -Force | Out-Null
    $healthAction = New-ScheduledTaskAction -Execute $shell -WorkingDirectory $Root -Argument (
        '-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -Root "{1}" -RecoverStopped' -f (Join-Path $Root 'ops/healthcheck.ps1'), $Root)
    $healthTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 5)
    $healthSettings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 2)
    # Health reads runtime-writable code and configuration. Keep it under the
    # same limited identity, never SYSTEM or an elevated administrator.
    Register-ScheduledTask -TaskName ($settings.task_prefix + '-health') -InputObject (New-ScheduledTask -Action $healthAction -Trigger $healthTrigger -Settings $healthSettings -Principal $principal) -User $account -Password $plainPassword -Force | Out-Null
} finally { $plainPassword = $null }
if ($StartNow) {
    foreach ($component in @('app','caddy')) { Start-ScheduledTask -TaskName ($settings.task_prefix + '-' + $component) }
}
