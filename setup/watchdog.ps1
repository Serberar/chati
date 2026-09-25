<#
Vigila Ollama, ComfyUI, el orquestador y el agente de codigo (OpenCode). Si
alguno deja de responder, lo reinicia automaticamente. Corre en bucle
infinito - se registra para arrancar sola al iniciar sesion via Task
Scheduler (ver install_watchdog.ps1).

Log de reinicios: watchdog.log, en esta misma carpeta.
#>

# AiRoot (codigo, orchestrator/) se calcula a partir de donde vive este
# script; DataRoot (ComfyUI - ver install.ps1) por defecto en AppData,
# salvo que se haya instalado en modo "todo junto" (ver ROADMAP.md punto
# 3b / 8b).
$AiRoot = Split-Path -Parent $PSScriptRoot
$DataRoot = if ($env:CHATI_DATA_ROOT) { $env:CHATI_DATA_ROOT }
            elseif ($env:LOCALAPPDATA) { "$env:LOCALAPPDATA\ChatiIA" }
            else { $AiRoot }
$LogFile = "$PSScriptRoot\watchdog.log"
$CheckIntervalSeconds = 30

function Write-Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') - $msg"
    Add-Content -Path $LogFile -Value $line
}

function Test-Url($url) {
    try {
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Ensure-Ollama {
    if (-not (Test-Url "http://localhost:11434/api/tags")) {
        Write-Log "Ollama caido. Reiniciando..."
        Get-Process -Name "ollama*" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
        # Bug conocido de Ollama con GPUs Blackwell (RTX 50, esta laptop incluida):
        # flash attention se activa sola y hace crashear el modelo al cargar en GPU
        # (ollama/ollama#18276, #18232). Desactivarla es el fix documentado.
        $env:OLLAMA_FLASH_ATTENTION = "0"
        Start-Process -FilePath (Get-Command ollama -ErrorAction SilentlyContinue).Source -ArgumentList "serve" -WindowStyle Hidden
        Write-Log "Ollama reiniciado."
    }
}

function Stop-ByPort($port) {
    # Mata lo que tenga el puerto realmente escuchando (y su padre, si aplica).
    # Mas fiable que buscar por texto de linea de comandos: en este Windows,
    # "venv\Scripts\python.exe" arranca en pareja padre+hijo (el lanzador
    # reexecuta contra el interprete base), y matar solo uno de los dos deja
    # el otro huerfano sin servir nada.
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" -ErrorAction SilentlyContinue
        if ($proc -and $proc.ParentProcessId) {
            Stop-Process -Id $proc.ParentProcessId -Force -ErrorAction SilentlyContinue
        }
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

function Ensure-ComfyUI {
    if (-not (Test-Url "http://127.0.0.1:8188/system_stats")) {
        Write-Log "ComfyUI caido. Reiniciando..."
        Stop-ByPort 8188
        Start-Sleep -Seconds 2
        Start-Process -FilePath "$DataRoot\ComfyUI\venv\Scripts\python.exe" -ArgumentList "main.py" `
            -WorkingDirectory "$DataRoot\ComfyUI" -WindowStyle Hidden
        Write-Log "ComfyUI reiniciado (tardara ~20-30s en responder)."
    }
}

function Ensure-Orchestrator {
    if (-not (Test-Url "http://127.0.0.1:8899/health")) {
        Write-Log "Orquestador caido. Reiniciando..."
        Stop-ByPort 8899
        Start-Sleep -Seconds 2
        Start-Process -FilePath "$AiRoot\orchestrator\venv\Scripts\python.exe" `
            -ArgumentList "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8899" `
            -WorkingDirectory "$AiRoot\orchestrator" -WindowStyle Hidden
        Write-Log "Orquestador reiniciado."
    }
}

function Ensure-OpenCode {
    if (-not (Test-Url "http://127.0.0.1:8901/")) {
        Write-Log "Agente de codigo (OpenCode) caido. Reiniciando..."
        Stop-ByPort 8901
        Start-Sleep -Seconds 1
        Start-Process -FilePath "$env:APPDATA\npm\opencode.cmd" -ArgumentList "web", "--port", "8901", "--hostname", "127.0.0.1" `
            -WorkingDirectory $AiRoot -WindowStyle Hidden
        Write-Log "Agente de codigo reiniciado."
    }
}

function Ensure-DailyBackup {
    $marker = "$PSScriptRoot\last_backup.txt"
    $today = Get-Date -Format "yyyy-MM-dd"
    $last = if (Test-Path $marker) { Get-Content $marker -Raw } else { "" }
    if ($last.Trim() -ne $today) {
        Write-Log "Backup diario pendiente. Ejecutando..."
        try {
            & powershell -ExecutionPolicy Bypass -File "$PSScriptRoot\backup.ps1" *>> $LogFile
            Write-Log "Backup diario completado."
        } catch {
            Write-Log "Backup diario fallo: $_"
        }
    }
}

Write-Log "Watchdog iniciado."

while ($true) {
    Ensure-Ollama
    Ensure-ComfyUI
    Ensure-Orchestrator
    Ensure-OpenCode
    Ensure-DailyBackup
    Start-Sleep -Seconds $CheckIntervalSeconds
}
