param(
    [string]$Root = 'D:\XMStudio',
    [switch]$Foreground,
    [ValidateSet('app','caddy')][string]$Component = 'app'
)
. (Join-Path $PSScriptRoot 'common.ps1')
$Root = Resolve-CompanyRoot $Root
if ($Foreground) {
    if (Test-Path -LiteralPath (Join-Path $Root 'ops/maintenance.json')) { exit 0 }
    $release = Get-CompanyRelease $Root
    $env:PYTHONUTF8 = '1'
    & $release.Python (Join-Path $release.Path 'scripts/company_service.py') $Component --root $Root
    exit $LASTEXITCODE
}
Invoke-CompanyOps $Root 'start'
