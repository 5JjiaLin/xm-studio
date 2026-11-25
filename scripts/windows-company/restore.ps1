param([string]$Root = 'D:\XMStudio', [Parameter(Mandatory)][string]$Snapshot, [Parameter(Mandatory)][string]$Destination)
. (Join-Path $PSScriptRoot 'common.ps1')
$Root = Resolve-CompanyRoot $Root
Invoke-CompanyOps $Root 'restore' @('--snapshot', $Snapshot, '--destination', $Destination)
