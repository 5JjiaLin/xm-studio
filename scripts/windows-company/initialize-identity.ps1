param([string]$Root = 'D:\XMStudio', [Parameter(Mandatory)][PSCredential]$Credential)
$ErrorActionPreference = 'Stop'
$principal = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'Run identity setup in an elevated PowerShell.' }
$Root = [IO.Path]::GetFullPath($Root).TrimEnd('\')
if ($Root -notmatch '^[A-Za-z]:\\.+' -or $Root.Length -lt 6) { throw 'Use a specific local deployment directory.' }
$account = $Credential.UserName
$localPrefix = $env:COMPUTERNAME + '\'
if (-not $account.StartsWith($localPrefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Specify COMPUTERNAME\local-service-user.' }
$name = $account.Substring($localPrefix.Length)
if ($name -notmatch '^[A-Za-z][A-Za-z0-9_-]{2,19}$' -or $name -eq $env:USERNAME) { throw 'Use a separate local service identity.' }
$user = Get-LocalUser -Name $name -ErrorAction SilentlyContinue
if (-not $user) {
    $user = New-LocalUser -Name $name -Password $Credential.Password -Description 'XM Studio company runtime' -AccountNeverExpires
}
if (-not $user.Enabled) { throw 'The specified service identity is disabled.' }
$adminGroup = Get-LocalGroup -SID 'S-1-5-32-544'
if (Get-LocalGroupMember -Group $adminGroup | Where-Object { $_.SID -eq $user.SID }) { throw 'The runtime identity must not be an administrator.' }
foreach ($relative in @('', 'releases', 'config', 'data', 'profile', 'ops', 'ops-logs', 'staging', 'caddy-data', 'runtime', 'tools')) {
    $directory = if ($relative) { Join-Path $Root $relative } else { $Root }
    [IO.Directory]::CreateDirectory($directory) | Out-Null
}
# Replace the root DACL, including pre-existing explicit grants. Child ACLs
# must be audited before activation; protected child ACLs do not inherit this.
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($user.SID.Value, 'S-1-5-18', 'S-1-5-32-544')) {
    $rule = [Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new($sid),
        [Security.AccessControl.FileSystemRights]::FullControl,
        [Security.AccessControl.InheritanceFlags]'ContainerInherit, ObjectInherit',
        [Security.AccessControl.PropagationFlags]::None,
        [Security.AccessControl.AccessControlType]::Allow)
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $Root -AclObject $acl
Write-Output ('Runtime identity and inherited root ACL prepared: ' + $account)
