Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Resolve-CompanyRoot([string]$Path) {
    $resolved = [IO.Path]::GetFullPath($Path).TrimEnd('\')
    if ($resolved -notmatch '^[A-Za-z]:\\.+' -or $resolved.Length -lt 6) {
        throw 'Deployment root must be a specific directory on a local drive.'
    }
    if (-not (Test-Path -LiteralPath (Join-Path $resolved 'config/current-release.json'))) {
        throw 'Missing config/current-release.json; prepare the deployment first.'
    }
    return $resolved
}

function Get-CompanyRelease([string]$Root) {
    $record = Get-Content -LiteralPath (Join-Path $Root 'config/current-release.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $release = [IO.Path]::GetFullPath($record.path)
    $python = Join-Path $release '.venv/Scripts/python.exe'
    if (-not (Test-Path -LiteralPath $python)) { throw "Release interpreter missing: $python" }
    return [PSCustomObject]@{ Path = $release; Python = $python }
}

function Invoke-CompanyOps([string]$Root, [string]$Command, [string[]]$Options = @()) {
    $release = Get-CompanyRelease $Root
    $env:PYTHONUTF8 = '1'
    & $release.Python (Join-Path $release.Path 'scripts/company_ops.py') $Command --root $Root @Options
    if ($LASTEXITCODE -ne 0) { throw "Company operation failed ($LASTEXITCODE): $Command" }
}
