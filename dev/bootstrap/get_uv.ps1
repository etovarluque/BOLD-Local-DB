# Downloads uv (https://github.com/astral-sh/uv) into the given folder, for
# the Windows launcher. The version is fixed and the file is checked against
# the SHA-256 published with it.
param([Parameter(Mandatory = $true)][string]$Dest, [string]$Version = "0.12.20")

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"   # the progress bar slows downloads down a lot in PowerShell 5
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "aarch64" } else { "x86_64" }
$name = "uv-$arch-pc-windows-msvc.zip"
$url  = "https://github.com/astral-sh/uv/releases/download/$Version/$name"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
$zip = Join-Path $Dest $name
$sum = "$zip.sha256"

Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
Invoke-WebRequest -Uri "$url.sha256" -OutFile $sum -UseBasicParsing
$expected = ((Get-Content $sum -Raw).Trim() -split "\s+")[0].ToLower()
$actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
if ($expected -ne $actual) {
    Remove-Item $zip, $sum -Force
    throw "The uv download doesn't match its checksum."
}
Expand-Archive -Path $zip -DestinationPath $Dest -Force
Remove-Item $zip, $sum -Force
