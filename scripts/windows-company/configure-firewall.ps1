param([string]$Root = 'D:\XMStudio')
. (Join-Path $PSScriptRoot 'common.ps1')
$Root = Resolve-CompanyRoot $Root
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run firewall setup in an elevated PowerShell.' }
$settings = Get-Content -LiteralPath (Join-Path $Root 'config/deployment.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$proxy = Get-Content -LiteralPath (Join-Path $Root 'config/caddy.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$ip = $null
if (-not [Net.IPAddress]::TryParse($settings.server_ip, [ref]$ip) -or $ip.AddressFamily -ne 'InterNetwork') { throw 'Set the confirmed IPv4 address first.' }
if (-not (Get-NetIPAddress -AddressFamily IPv4 -IPAddress $settings.server_ip -ErrorAction SilentlyContinue)) { throw 'Confirmed server IP is not assigned to this computer.' }
if ($proxy.bind_ip -ne $settings.server_ip) { throw 'Caddy bind_ip must match the confirmed server IP.' }
if (-not $settings.client_subnets -or $settings.client_subnets.Count -eq 0) { throw 'Authorized employee subnets are required.' }
foreach ($subnet in $settings.client_subnets) {
    if ($subnet -notmatch '^\d{1,3}(\.\d{1,3}){3}/\d{1,2}$') { throw 'Use explicit IPv4 CIDR employee subnets.' }
    $parts = $subnet.Split('/')
    $address = $null
    if (-not [Net.IPAddress]::TryParse($parts[0], [ref]$address) -or [int]$parts[1] -lt 16 -or [int]$parts[1] -gt 32) {
        throw 'Verify a narrow employee subnet (IPv4 prefix /16 through /32).'
    }
}
if ($settings.task_prefix -notmatch '^[A-Za-z0-9_-]{1,40}$') { throw 'Invalid deployment rule prefix.' }
$ruleName = $settings.task_prefix + '-HTTPS'
$existing = Get-NetFirewallRule -Name $ruleName -ErrorAction SilentlyContinue
if ($existing) { Remove-NetFirewallRule -Name $ruleName }
New-NetFirewallRule -Name $ruleName -DisplayName $ruleName -Direction Inbound -Action Allow -Enabled True `
    -Profile Any -Protocol TCP -LocalPort 443 -LocalAddress $settings.server_ip `
    -RemoteAddress $settings.client_subnets -Program $proxy.executable | Out-Null
