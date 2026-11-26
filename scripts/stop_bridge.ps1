$ErrorActionPreference = "Stop"

$Processes = Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -in @("python.exe", "pythonw.exe")) -and
    (($_.CommandLine -like "*jk402a_bridge*") -or ($_.CommandLine -like "*jk402a_tray*"))
}

if (-not $Processes) {
    Write-Host "Nenhum processo do bridge JK-402A em execução."
    exit 0
}

foreach ($Proc in $Processes) {
    Stop-Process -Id $Proc.ProcessId -Force
    Write-Host "Processo encerrado: PID $($Proc.ProcessId)"
}
