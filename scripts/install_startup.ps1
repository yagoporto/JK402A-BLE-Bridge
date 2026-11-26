param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$StartupDir = [Environment]::GetFolderPath("Startup")
$ShortcutPath = Join-Path $StartupDir "JK402A-BLE-Bridge.lnk"

if ($Uninstall) {
    if (Test-Path $ShortcutPath) {
        Remove-Item $ShortcutPath -Force
        Write-Host "Atalho removido de: $ShortcutPath"
    } else {
        Write-Host "Atalho não encontrado em: $ShortcutPath"
    }
    exit 0
}

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$VenvPythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$Pythonw = if (Test-Path $VenvPythonw) { $VenvPythonw } else { (Get-Command pythonw.exe).Source }

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Pythonw
$Shortcut.Arguments = "`"$(Join-Path $ProjectRoot 'jk402a_tray.pyw')`" --minimized"
$Shortcut.WorkingDirectory = $ProjectRoot
$IconPath = Join-Path $ProjectRoot "assets\logo.ico"
if (Test-Path $IconPath) {
    $Shortcut.IconLocation = "$IconPath,0"
}
$Shortcut.WindowStyle = 7
$Shortcut.Description = "Bridge TCP 9100 -> BLE para impressora JK-402A (System Tray)"
$Shortcut.Save()

Write-Host "Atalho criado com sucesso em: $ShortcutPath"
Write-Host "O bridge iniciará automaticamente na bandeja do Windows (sem janela de terminal)."
