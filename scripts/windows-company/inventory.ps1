param([string]$OutputPath)
$ErrorActionPreference = 'Stop'
$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
$system = Get-CimInstance Win32_ComputerSystem
$errors = [System.Collections.Generic.List[string]]::new()
$connections = @()
try { $connections = @(Get-SmbConnection | Select-Object ServerName,ShareName) }
catch { $errors.Add('SMB connection inventory unavailable: ' + $_.Exception.Message) }
$report = [ordered]@{
    CollectedAt = (Get-Date).ToString('o')
    ComputerName = $env:COMPUTERNAME
    User = $identity.Name
    Elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    Domain = $system.Domain
    PartOfDomain = $system.PartOfDomain
    Adapters = @(Get-NetAdapter | Select-Object Name,InterfaceDescription,MacAddress,Status)
    Networks = @(Get-NetIPConfiguration | ForEach-Object {
        [ordered]@{
            InterfaceAlias = $_.InterfaceAlias
            IPv4 = @($_.IPv4Address | Select-Object IPAddress,PrefixLength,PrefixOrigin)
            Gateway = @($_.IPv4DefaultGateway.NextHop)
            DNS = @($_.DNSServer.ServerAddresses)
        }
    })
    Profiles = @(Get-NetConnectionProfile | Select-Object InterfaceAlias,NetworkCategory,IPv4Connectivity)
    DHCPLeases = @(Get-CimInstance Win32_NetworkAdapterConfiguration -Filter 'IPEnabled=True' |
        Select-Object Description,DHCPEnabled,DHCPServer,DHCPLeaseObtained,DHCPLeaseExpires,DNSDomain)
    MappedShares = @(Get-SmbMapping | Select-Object LocalPath,RemotePath,Status)
    SmbConnections = $connections
    Disks = @(Get-CimInstance Win32_LogicalDisk | Select-Object DeviceID,DriveType,ProviderName,Size,FreeSpace)
    Errors = @($errors.ToArray())
    # Discovery cannot establish these company-owned assignments.
    ConfirmedFixedIP = $null
    ConfirmedSiteHost = $null
    AuthorizedClientSubnets = @()
    IndependentBackupTarget = $null
}
$json = $report | ConvertTo-Json -Depth 8
if ($OutputPath) {
    $full = [IO.Path]::GetFullPath($OutputPath)
    [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($full)) | Out-Null
    [IO.File]::WriteAllText($full, $json, [Text.UTF8Encoding]::new($false))
}
$json
