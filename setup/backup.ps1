<#
Copia de seguridad de los datos irremplazables de la IA personal:
memoria de conversaciones, base de conocimiento, caras guardadas, CV y la
clave API. NO incluye los modelos (esos se pueden volver a descargar).

Por defecto copia a tu OneDrive PERSONAL (no al de "Grupo INEXO" / trabajo,
a proposito: estos datos son tuyos - conversaciones, fotos, CV - y no deberian
sincronizarse solas a un OneDrive gestionado por un empleador). Cambia
-Destino si prefieres otro sitio (un disco externo, otra nube, etc.).

Para de verdad los archivos de la base de conocimiento (chromadb) quedan
bloqueados por el orquestador mientras esta corriendo - una copia en caliente
los salta en silencio. Por eso este script para el orquestador un momento,
copia, y lo vuelve a arrancar (el watchdog lo recuperaria de todos modos si
algo fallase). Unos segundos de indisponibilidad, backup consistente de
verdad en vez de uno que parece completo y no lo es.

Uso:  powershell -ExecutionPolicy Bypass -File backup.ps1 [-Destino "ruta"]
#>

param(
    [string]$Destino = "$env:USERPROFILE\OneDrive\IA-Personal-Backups",
    [int]$Mantener = 14  # cuantas copias recientes conservar
)

# AiRoot (codigo, orchestrator/) se calcula a partir de donde vive este
# script; DataRoot (los datos de verdad, lo que hace falta copiar - ver
# install.ps1) por defecto en AppData, salvo que se haya instalado en modo
# "todo junto" (ver ROADMAP.md punto 3b / 8b).
$AiRoot = Split-Path -Parent $PSScriptRoot
$DataRoot = if ($env:CHATI_DATA_ROOT) { $env:CHATI_DATA_ROOT }
            elseif ($env:LOCALAPPDATA) { "$env:LOCALAPPDATA\ChatiIA" }
            else { $AiRoot }
$origen = "$DataRoot\data"
$fecha = Get-Date -Format "yyyy-MM-dd_HHmm"
$nombreZip = "ia-personal-data_$fecha.zip"

if (-not (Test-Path $origen)) {
    Write-Host "No existe $origen todavia, nada que copiar." -ForegroundColor Yellow
    exit 0
}

function Test-Url($url) {
    try { (Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200 }
    catch { $false }
}

function Stop-ByPort($port) {
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" -ErrorAction SilentlyContinue
        if ($proc -and $proc.ParentProcessId) { Stop-Process -Id $proc.ParentProcessId -Force -ErrorAction SilentlyContinue }
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

$wasRunning = Test-Url "http://127.0.0.1:8899/health"
if ($wasRunning) {
    Write-Host "Parando el orquestador un momento para un backup consistente..." -ForegroundColor Yellow
    Stop-ByPort 8899
    Start-Sleep -Seconds 2
}

if (-not (Test-Path $Destino)) {
    New-Item -ItemType Directory -Path $Destino -Force | Out-Null
}

$zipPath = Join-Path $Destino $nombreZip
Compress-Archive -Path "$origen\*" -DestinationPath $zipPath -Force
Write-Host "Backup creado: $zipPath" -ForegroundColor Green

if ($wasRunning) {
    Start-Process -FilePath "$AiRoot\orchestrator\venv\Scripts\python.exe" `
        -ArgumentList "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8899" `
        -WorkingDirectory "$AiRoot\orchestrator" -WindowStyle Hidden
    Write-Host "Orquestador reiniciado." -ForegroundColor Green
}

# rotacion: borra copias mas viejas que las ultimas $Mantener
$antiguos = Get-ChildItem $Destino -Filter "ia-personal-data_*.zip" | Sort-Object LastWriteTime -Descending | Select-Object -Skip $Mantener
foreach ($f in $antiguos) {
    Remove-Item $f.FullName -Force
    Write-Host "Backup antiguo eliminado: $($f.Name)" -ForegroundColor DarkGray
}

Set-Content -Path "$PSScriptRoot\last_backup.txt" -Value (Get-Date -Format "yyyy-MM-dd")
