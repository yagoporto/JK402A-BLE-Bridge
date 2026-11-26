param(
    [Parameter(Mandatory = $true)]
    [string]$InstallDir
)

$ErrorActionPreference = "Stop"

function Find-Python3 {
    $candidates = @()

    $cmdPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmdPython) { $candidates += $cmdPython.Source }

    $cmdPy = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($cmdPy) {
        try {
            $pyPath = & $cmdPy.Source -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $pyPath) { $candidates += $pyPath.Trim() }
        } catch {}
    }

    $searchRoots = @(
        (Join-Path $env:LocalAppData "Programs\Python"),
        (Join-Path $env:ProgramFiles "Python*"),
        "C:\Python3*"
    )
    foreach ($root in $searchRoots) {
        Get-ChildItem -Path $root -Filter "python.exe" -Recurse -Depth 2 -ErrorAction SilentlyContinue |
            ForEach-Object { $candidates += $_.FullName }
    }

    foreach ($exe in ($candidates | Select-Object -Unique)) {
        if (-not (Test-Path $exe)) { continue }
        try {
            $ver = & $exe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
            if ($LASTEXITCODE -eq 0 -and $ver) {
                $parts = $ver.Trim().Split('.')
                if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10) {
                    return $exe
                }
            }
        } catch {}
    }
    return $null
}

Write-Host "[1/3] Verificando instalacao do Python 3.10+..."
$pythonExe = Find-Python3

if (-not $pythonExe) {
    Write-Host "Python nao encontrado. Baixando instalador oficial do Python 3.13.2 (64-bit)..."
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $pyUrl = "https://www.python.org/ftp/python/3.13.2/python-3.13.2-amd64.exe"
    $pyInstaller = Join-Path $env:TEMP "python-3.13.2-amd64.exe"

    Invoke-WebRequest -Uri $pyUrl -OutFile $pyInstaller -UseBasicParsing

    Write-Host "Instalando Python 3.13.2 silenciosamente..."
    $proc = Start-Process -FilePath $pyInstaller -ArgumentList "/quiet InstallAllUsers=0 PrependPath=1 Include_test=0" -Wait -PassThru
    Remove-Item $pyInstaller -Force -ErrorAction SilentlyContinue

    if ($proc.ExitCode -ne 0) {
        throw "Falha ao instalar o Python (codigo $($proc.ExitCode))."
    }

    $pythonExe = Find-Python3
    if (-not $pythonExe) {
        throw "Python foi instalado, mas o executavel nao foi localizado."
    }
}

Write-Host "Python encontrado: $pythonExe"

$venvDir = Join-Path $InstallDir ".venv"
$venvPython = Join-Path $venvDir "Scripts\python.exe"

if (-not (Test-Path $venvPython)) {
    Write-Host "[2/3] Criando ambiente virtual (.venv)..."
    & $pythonExe -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw "Falha ao criar .venv." }
}

Write-Host "[3/3] Instalando dependencias (bleak, pystray, pillow)..."
$reqFile = Join-Path $InstallDir "requirements.txt"
& $venvPython -m pip install --upgrade pip --quiet
& $venvPython -m pip install -r $reqFile --quiet
if ($LASTEXITCODE -ne 0) { throw "Falha ao instalar dependencias Python." }

Write-Host "Ambiente configurado com sucesso!"
