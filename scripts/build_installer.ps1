$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$DistDir = Join-Path $ProjectRoot "dist"
New-Item -ItemType Directory -Path $DistDir -Force | Out-Null

$Makensis = "C:\Program Files (x86)\NSIS\makensis.exe"
if (-not (Test-Path $Makensis)) {
    $cmd = Get-Command makensis.exe -ErrorAction SilentlyContinue
    if (-not $cmd) { throw "NSIS (makensis.exe) nao encontrado." }
    $Makensis = $cmd.Source
}

$NsiPath = Join-Path $ProjectRoot "installer\jk402a_bridge.nsi"
& $Makensis $NsiPath
if ($LASTEXITCODE -ne 0) {
    throw "makensis.exe falhou com codigo $LASTEXITCODE."
}

Write-Host "Instalador gerado em: $(Join-Path $DistDir 'JK402A-BLE-Bridge-Setup.exe')"
