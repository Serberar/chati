<#
Restaura una copia de seguridad hecha por backup.ps1 (un .zip de la carpeta
data: usuarios, conversaciones, documentos, caras, CV... todo cifrado).

Lo que hay ahora NO se borra: se aparta a data.antes-de-restaurar_<fecha>, por
si la copia elegida no era la buena.

Uso:  powershell -ExecutionPolicy Bypass -File restore.ps1 -Zip "ruta\ia-personal-data_2026-09-29_0300.zip"
      (sin -Zip, usa la copia mas reciente de la carpeta de copias)
#>

param(
    [string]$Zip = "",
    [string]$CarpetaCopias = "$env:USERPROFILE\OneDrive\IA-Personal-Backups",
    [string]$DataRoot = ""
)

$AiRoot = Split-Path -Parent $PSScriptRoot
$chatiDataRoot = if ($env:CHATI_DATA_ROOT) { $env:CHATI_DATA_ROOT }
                 elseif ([Environment]::GetEnvironmentVariable("CHATI_DATA_ROOT", "User")) {
                     [Environment]::GetEnvironmentVariable("CHATI_DATA_ROOT", "User") }
                 elseif ($env:LOCALAPPDATA) { "$env:LOCALAPPDATA\ChatiIA" }
                 else { $AiRoot }
if (-not $DataRoot) { $DataRoot = $chatiDataRoot }
$data = "$DataRoot\data"
# solo se para el orquestador si se restaura la carpeta que el usa (con otra
# -DataRoot, p.ej. para probar una copia, no se toca lo que esta en marcha)
$isLiveData = [IO.Path]::GetFullPath($DataRoot).TrimEnd("\") -eq [IO.Path]::GetFullPath($chatiDataRoot).TrimEnd("\")

if (-not $Zip) {
    $latest = Get-ChildItem $CarpetaCopias -Filter "ia-personal-data_*.zip" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $latest) { Write-Host "No hay copias en $CarpetaCopias." -ForegroundColor Red; exit 1 }
    $Zip = $latest.FullName
}
if (-not (Test-Path $Zip)) { Write-Host "No existe $Zip" -ForegroundColor Red; exit 1 }

# la copia tiene que tener al menos la base de usuarios: si no, no es de Chati
Add-Type -AssemblyName System.IO.Compression.FileSystem
$entries = [System.IO.Compression.ZipFile]::OpenRead($Zip)
try { $hasUsers = @($entries.Entries | Where-Object { $_.FullName -eq "users.db" }).Count -gt 0 }
finally { $entries.Dispose() }
if (-not $hasUsers) { Write-Host "$Zip no parece una copia de Chati (no tiene users.db)." -ForegroundColor Red; exit 1 }

Write-Host "Restaurando $Zip en $data" -ForegroundColor Cyan

function Stop-ByPort($port) {
    foreach ($c in Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" -ErrorAction SilentlyContinue
        if ($proc -and $proc.ParentProcessId) { Stop-Process -Id $proc.ParentProcessId -Force -ErrorAction SilentlyContinue }
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

$wasRunning = $isLiveData -and [bool](Get-NetTCPConnection -LocalPort 8899 -State Listen -ErrorAction SilentlyContinue)
if ($wasRunning) { Stop-ByPort 8899; Start-Sleep -Seconds 2 }

if (Test-Path $data) {
    $apartado = "$data.antes-de-restaurar_$(Get-Date -Format 'yyyy-MM-dd_HHmmss')"
    Move-Item $data $apartado
    Write-Host "Lo que habia se ha apartado en $apartado" -ForegroundColor Yellow
}
Expand-Archive -Path $Zip -DestinationPath $data -Force
# Las claves no van en la copia (backup.ps1): se recuperan de lo apartado. Sin
# la de OpenCode, Chati creaba otra y el agente (que sigue con la vieja) dejaba
# de funcionar hasta reiniciar el PC (auditoria 2026-10-05).
if ($apartado) {
    foreach ($key in "opencode_password.txt", "registration_key.txt", "api_key.txt") {
        if ((Test-Path "$apartado\$key") -and -not (Test-Path "$data\$key")) {
            Copy-Item "$apartado\$key" "$data\$key"
        }
    }
}
Write-Host "Copia restaurada." -ForegroundColor Green

if ($wasRunning) {
    Start-Process -FilePath "$AiRoot\orchestrator\venv\Scripts\python.exe" `
        -ArgumentList "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8899" `
        -WorkingDirectory "$AiRoot\orchestrator" -WindowStyle Hidden
    Write-Host "Orquestador arrancado. Inicia sesion de nuevo." -ForegroundColor Green
}
