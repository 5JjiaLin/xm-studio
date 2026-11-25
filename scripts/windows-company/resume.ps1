param([string]$Root = 'D:\XMStudio')
. (Join-Path $PSScriptRoot 'common.ps1')
$Root = Resolve-CompanyRoot $Root
Invoke-CompanyOps $Root 'resume'
