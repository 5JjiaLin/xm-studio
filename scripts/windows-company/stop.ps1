param([string]$Root = 'D:\XMStudio', [int]$TimeoutSeconds = 3600)
. (Join-Path $PSScriptRoot 'common.ps1')
$Root = Resolve-CompanyRoot $Root
Invoke-CompanyOps $Root 'stop' @('--timeout', [string]$TimeoutSeconds)
