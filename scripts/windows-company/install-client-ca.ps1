param([Parameter(Mandatory)][string]$CertificatePath, [Parameter(Mandatory)][string]$ExpectedSha256)
$ErrorActionPreference = 'Stop'
if ($ExpectedSha256 -notmatch '^[A-Fa-f0-9]{64}$') { throw 'Provide the separately verified SHA-256 value.' }
$actual = (Get-FileHash -LiteralPath $CertificatePath -Algorithm SHA256).Hash
if ($actual -ne $ExpectedSha256) { throw 'CA certificate checksum mismatch.' }
if ([IO.Path]::GetExtension($CertificatePath) -notin @('.crt','.cer')) { throw 'Use the public root certificate only.' }
Import-Certificate -FilePath $CertificatePath -CertStoreLocation 'Cert:\CurrentUser\Root' | Select-Object Subject,Thumbprint,NotAfter
