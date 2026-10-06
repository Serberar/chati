<#
Vigila Ollama, ComfyUI, el orquestador y el agente de codigo (OpenCode). Si
alguno deja de responder, lo reinicia automaticamente. Corre en bucle
infinito - se registra para arrancar sola al iniciar sesion via Task
Scheduler (ver install_watchdog.ps1).

Log de reinicios: watchdog.log, en <carpeta de datos>\data\logs.
#>

# AiRoot (codigo, orchestrator/) se calcula a partir de donde vive este
# script; DataRoot (ComfyUI - ver install.ps1) por defecto en AppData,
# salvo que se haya instalado en modo "todo junto" (ver ROADMAP.md punto
# 3b / 8b).
$AiRoot = Split-Path -Parent $PSScriptRoot
foreach ($name in "OLLAMA_MODELS", "CHATI_DATA_ROOT") {  # ver Update-EnvFromUser
    $value = [Environment]::GetEnvironmentVariable($name, "User")
    if ($value) { Set-Item -Path "Env:$name" -Value $value }
}
$DataRoot = if ($env:CHATI_DATA_ROOT) { $env:CHATI_DATA_ROOT }
                           elseif ([Environment]::GetEnvironmentVariable("CHATI_DATA_ROOT", "User")) {
                               [Environment]::GetEnvironmentVariable("CHATI_DATA_ROOT", "User") }
            elseif ($env:LOCALAPPDATA) { "$env:LOCALAPPDATA\ChatiIA" }
            else { $AiRoot }
# Registro y fecha de la ultima copia en la carpeta de DATOS: con el instalador
# el script vive en Program Files, donde el usuario no puede escribir; no se
# guardaba la fecha y la copia (que reinicia Chati) se repetia en cada vuelta
# (auditoria 2026-10-05; en el Sandbox no se veia porque alli se es admin).
$StateDir = "$DataRoot\data\logs"
New-Item -ItemType Directory -Path $StateDir -Force -ErrorAction SilentlyContinue | Out-Null
$LogFile = "$StateDir\watchdog.log"
$BackupMarker = "$StateDir\last_backup.txt"
if (-not (Test-Path $BackupMarker) -and (Test-Path "$PSScriptRoot\last_backup.txt")) {
    Copy-Item "$PSScriptRoot\last_backup.txt" $BackupMarker -ErrorAction SilentlyContinue  # de antes del cambio
}
$CheckIntervalSeconds = 30
# Fallos seguidos antes de reiniciar: un servicio ocupado (Ollama cargando un
# modelo, ComfyUI arrancando) puede tardar mas de 5s en contestar sin estar
# caido. Con 1 solo fallo se reiniciaban servicios sanos (visto el 2026-09-28).
$FailuresBeforeRestart = 2
# Ollama y ComfyUI tardan mas de un minuto en arrancar al encender el PC: con
# 2 fallos (1 min) el vigilante los reiniciaba mientras aun arrancaban (visto
# el 2026-09-29, a ComfyUI dos veces seguidas).
$SlowStartFailures = 4
$Failures = @{}

function Write-Log($msg) {
    # rota a los 2 MB (antes crecia sin fin)
    if ((Test-Path $LogFile) -and (Get-Item $LogFile).Length -gt 2MB) {
        Move-Item -Force $LogFile "$LogFile.old"
    }
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') - $msg"
    Add-Content -Path $LogFile -Value $line
}

function Update-EnvFromUser {
    # El vigilante corre desde que se inicia sesion: si despues cambian las
    # variables del usuario (p.ej. OLLAMA_MODELS), las que heredo al arrancar
    # estan viejas. Se releen antes de lanzar nada (bug real 2026-09-28:
    # reinicio Ollama apuntando a una carpeta de modelos antigua y vacia).
    foreach ($name in "OLLAMA_MODELS", "CHATI_DATA_ROOT") {
        $value = [Environment]::GetEnvironmentVariable($name, "User")
        if ($value) { Set-Item -Path "Env:$name" -Value $value }
    }
}

function Test-Down($name, $url, $threshold = $FailuresBeforeRestart, $headers = @{}) {
    # $true solo tras $threshold comprobaciones fallidas seguidas
    if (Test-Url $url $headers) {
        $Failures[$name] = 0
        return $false
    }
    $Failures[$name] = 1 + [int]$Failures[$name]
    if ($Failures[$name] -lt $threshold) {
        Write-Log "$name no responde (aviso $($Failures[$name])/$threshold)."
        return $false
    }
    $Failures[$name] = 0
    Update-EnvFromUser
    return $true
}

function Test-Url($url, $headers = @{}) {
    try {
        $resp = Invoke-WebRequest -Uri $url -Headers $headers -UseBasicParsing -TimeoutSec 5
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

function Get-OpenCodePassword {
    # La misma que usa el orquestador (opencode_client.PASSWORD_FILE): sin
    # contraseña, cualquier web podia mandar comandos al agente (auditoria 2026-09-29)
    $file = "$DataRoot\data\opencode_password.txt"
    if (Test-Path $file) {
        # existe pero no se puede leer (permisos): NO inventar otra - OpenCode
        # arrancaria con una contraseña que nadie conoce (paso el 2026-09-30)
        $current = (Get-Content $file -Raw -ErrorAction Stop).Trim()
        if ($current) { return $current }
    }
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $password = [Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_").TrimEnd("=")
    New-Item -ItemType Directory -Force -Path (Split-Path $file) | Out-Null
    Set-Content -Path $file -Value $password -NoNewline -Encoding ascii
    return $password
}

function Get-OpenCodeHeaders {
    $pair = [Text.Encoding]::ASCII.GetBytes("opencode:" + (Get-OpenCodePassword))
    return @{ Authorization = "Basic " + [Convert]::ToBase64String($pair) }
}

function Ensure-Ollama {
    # Ollama se esta actualizando: no tocar nada. El 2026-09-29 el vigilante
    # mato la actualizacion a medias y Ollama se quedo sin la carpeta cuda_v13
    # (toda la IA en CPU, 8 veces mas lenta).
    if (Get-Process -Name "OllamaSetup*" -ErrorAction SilentlyContinue) {
        $Failures["Ollama"] = 0
        return
    }
    if (Test-Down "Ollama" "http://127.0.0.1:11434/api/tags" $SlowStartFailures) {
        Write-Log "Ollama caido. Reiniciando..."
        # solo el servidor que escucha en el puerto, nunca "ollama*": eso
        # incluye la aplicacion de Ollama, que es la que instala las actualizaciones
        foreach ($c in Get-NetTCPConnection -LocalPort 11434 -State Listen -ErrorAction SilentlyContinue) {
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
        }
        Start-Sleep -Seconds 1
        # Los procesos de modelo (llama-server) sobreviven a su Ollama y siguen
        # ocupando la GPU: el 2026-09-29 uno huerfano la tuvo llena desde la
        # manana y el modelo nuevo iba 3 veces mas lento. Fuera los huerfanos.
        foreach ($p in Get-CimInstance Win32_Process -Filter "Name='llama-server.exe'" -ErrorAction SilentlyContinue) {
            if (-not (Get-Process -Id $p.ParentProcessId -ErrorAction SilentlyContinue)) {
                Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
                Write-Log "Proceso de modelo huerfano cerrado ($($p.ProcessId))."
            }
        }
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
    if (Test-Down "ComfyUI" "http://127.0.0.1:8188/system_stats" $SlowStartFailures) {
        Write-Log "ComfyUI caido. Reiniciando..."
        Stop-ByPort 8188
        Start-Sleep -Seconds 2
        Start-Process -FilePath "$DataRoot\ComfyUI\venv\Scripts\python.exe" -ArgumentList "main.py" `
            -WorkingDirectory "$DataRoot\ComfyUI" -WindowStyle Hidden
        Write-Log "ComfyUI reiniciado (tardara ~20-30s en responder)."
    }
}

function Ensure-Orchestrator {
    if (Test-Down "Orquestador" "http://127.0.0.1:8899/health") {
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
    if (Test-Down "OpenCode" "http://127.0.0.1:8901/" $FailuresBeforeRestart (Get-OpenCodeHeaders)) {
        Write-Log "Agente de codigo (OpenCode) caido. Reiniciando..."
        Stop-ByPort 8901
        Start-Sleep -Seconds 1
        $env:OPENCODE_SERVER_PASSWORD = Get-OpenCodePassword
        Start-Process -FilePath "$env:APPDATA\npm\opencode.cmd" -ArgumentList "serve", "--port", "8901", "--hostname", "127.0.0.1" `
            -WorkingDirectory $AiRoot -WindowStyle Hidden
        Write-Log "Agente de codigo reiniciado."
    }
}

function Ensure-DailyBackup {
    $marker = $BackupMarker
    $today = Get-Date -Format "yyyy-MM-dd"
    $last = if (Test-Path $marker) { Get-Content $marker -Raw } else { "" }
    if ($last.Trim() -ne $today) {
        # La copia reinicia el orquestador (cierra las sesiones y corta lo que
        # este en marcha): de madrugada y sin actividad. Si lleva 2 dias sin
        # poder, en el primer momento sin actividad (auditoria 2026-09-29).
        $hour = (Get-Date).Hour
        $overdue = $true
        try { $overdue = ((Get-Date) - [datetime]::ParseExact($last.Trim(), "yyyy-MM-dd", $null)).TotalDays -ge 2 } catch {}
        if (-not $overdue -and ($hour -lt 3 -or $hour -ge 7)) { return }
        try {
            $h = Invoke-RestMethod -Uri "http://127.0.0.1:8899/health?busy=true" -TimeoutSec 30
            if ($h.busy) { return }
        } catch {}  # orquestador caido: se puede copiar igualmente
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
    # un fallo en una comprobacion (p.ej. no poder leer la contraseña de
    # OpenCode) se anota y se sigue: el vigilante no debe morir nunca
    foreach ($check in "Ensure-Ollama", "Ensure-ComfyUI", "Ensure-Orchestrator", "Ensure-OpenCode", "Ensure-DailyBackup") {
        try { & $check } catch { Write-Log "Fallo en ${check}: $_" }
    }
    Start-Sleep -Seconds $CheckIntervalSeconds
}
