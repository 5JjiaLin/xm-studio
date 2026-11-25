param([string]$Root = 'D:\XMStudio', [string]$Destination, [int]$TimeoutSeconds = 3600)
. (Join-Path $PSScriptRoot 'common.ps1')
$Root = Resolve-CompanyRoot $Root
if (-not $Destination) {
    $settings = Get-Content -LiteralPath (Join-Path $Root 'config/deployment.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $Destination = $settings.backup_unc
}
if (-not $Destination -or -not $Destination.StartsWith('\\')) { throw 'Confirmed independent UNC backup directory is required.' }
Invoke-CompanyOps $Root 'backup' @('--destination', $Destination, '--timeout', [string]$TimeoutSeconds)
