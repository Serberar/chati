<#
Arranca los tres componentes de la IA personal: Ollama, ComfyUI y el
orquestador. Nada de esto se instala como servicio de Windows, asi que hay
que ejecutar este script cada vez que reinicies el equipo y quieras usarla
(o usa watchdog.ps1 / install_watchdog.ps1 para que se vigile solo).

Uso:  powershell -ExecutionPolicy Bypass -File start_all.ps1
Luego abre http://localhost:8899 en el navegador.
#>

# Se calcula a partir de donde vive este script, no fijo a C:\AI - para que
# la carpeta entera se pueda mover o vivir en un disco externo (ver
# ROADMAP.md, punto 3b).
$AiRoot = Split-Path -Parent $PSScriptRoot

function Test-Url($url) {
    try {
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

Write-Host "=== Arrancando Ollama ===" -ForegroundColor Cyan
if (-not (Test-Url "http://localhost:11434/api/tags")) {
    # Bug conocido de Ollama con GPUs Blackwell (RTX 50, ollama/ollama#18276, #18232):
    # flash attention se activa sola y crashea al cargar modelos grandes en GPU.
    $env:OLLAMA_FLASH_ATTENTION = "0"
    Start-Process -FilePath (Get-Command ollama).Source -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 2
} else {
    Write-Host "Ollama ya esta corriendo." -ForegroundColor Green
}

Write-Host "=== Arrancando ComfyUI ===" -ForegroundColor Cyan
if (-not (Test-Url "http://127.0.0.1:8188/system_stats")) {
    Start-Process -FilePath "$AiRoot\ComfyUI\venv\Scripts\python.exe" -ArgumentList "main.py" -WorkingDirectory "$AiRoot\ComfyUI" -WindowStyle Hidden
    Write-Host "ComfyUI arrancando (tarda ~20-30s en estar listo)..." -ForegroundColor Yellow
} else {
    Write-Host "ComfyUI ya esta corriendo." -ForegroundColor Green
}

Write-Host "=== Arrancando el orquestador ===" -ForegroundColor Cyan
if (-not (Test-Url "http://127.0.0.1:8899/health")) {
    Write-Host "Esperando a que ComfyUI este listo antes de arrancar el orquestador..." -ForegroundColor Yellow
    $deadline = (Get-Date).AddSeconds(60)
    while (-not (Test-Url "http://127.0.0.1:8188/system_stats") -and (Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
    }
    Start-Process -FilePath "$AiRoot\orchestrator\venv\Scripts\python.exe" `
        -ArgumentList "-m", "uvicorn", "main:app", "--host", "127.0.0.1", "--port", "8899" `
        -WorkingDirectory "$AiRoot\orchestrator" -WindowStyle Hidden
    Write-Host "Orquestador arrancando..." -ForegroundColor Yellow
} else {
    Write-Host "El orquestador ya esta corriendo." -ForegroundColor Green
}

Write-Host "=== Arrancando el agente de codigo (OpenCode) ===" -ForegroundColor Cyan
if (-not (Test-Url "http://127.0.0.1:8901/")) {
    Start-Process -FilePath "$env:APPDATA\npm\opencode.cmd" -ArgumentList "web", "--port", "8901", "--hostname", "127.0.0.1" `
        -WorkingDirectory $AiRoot -WindowStyle Hidden
    Write-Host "Agente de codigo arrancando..." -ForegroundColor Yellow
} else {
    Write-Host "El agente de codigo ya esta corriendo." -ForegroundColor Green
}

Start-Sleep -Seconds 5
Write-Host "`n=== Listo. Abre http://localhost:8899 en el navegador. ===" -ForegroundColor Cyan
