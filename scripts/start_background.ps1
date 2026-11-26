$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$Pythonw = if (Test-Path $VenvPythonw) { $VenvPythonw } else { "pythonw.exe" }
$EntryScript = Join-Path $ProjectRoot "jk402a_tray.pyw"

Start-Process -FilePath $Pythonw -ArgumentList "`"$EntryScript`" --minimized" -WorkingDirectory $ProjectRoot -WindowStyle Hidden
Write-Host "JK-402A Bridge iniciado na bandeja do sistema (System Tray)."
Write-Host "Logs em: $(Join-Path $ProjectRoot 'logs\jk402a_bridge.log')"
