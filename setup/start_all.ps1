<#
Arranca los tres componentes de la IA personal: Ollama, ComfyUI y el
orquestador. Nada de esto se instala como servicio de Windows, asi que hay
que ejecutar este script cada vez que reinicies el equipo y quieras usarla
(o usa watchdog.ps1 / install_watchdog.ps1 para que se vigile solo).

Uso:  powershell -ExecutionPolicy Bypass -File start_all.ps1
Luego abre http://localhost:8899 en el navegador.
#>

# AiRoot (codigo, orchestrator/) se calcula a partir de donde vive este
# script; DataRoot (ComfyUI, modelos - ver install.ps1) por defecto en
# AppData, salvo que se haya instalado en modo "todo junto" (ver ROADMAP.md
# punto 3b / 8b).
param(
    [string]$AiRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = $(if ($env:CHATI_DATA_ROOT) { $env:CHATI_DATA_ROOT }
                           elseif ([Environment]::GetEnvironmentVariable("CHATI_DATA_ROOT", "User")) {
                               [Environment]::GetEnvironmentVariable("CHATI_DATA_ROOT", "User") }
                           elseif ($env:LOCALAPPDATA) { "$env:LOCALAPPDATA\ChatiIA" }
                           else { (Split-Path -Parent $PSScriptRoot) })
)

function Test-Url($url) {
    try {
        $resp = Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 5
        return $resp.StatusCode -eq 200
    } catch {
        return $false
    }
}

Write-Host "=== Arrancando Ollama ===" -ForegroundColor Cyan
if (-not (Test-Url "http://127.0.0.1:11434/api/tags")) {
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
    Start-Process -FilePath "$DataRoot\ComfyUI\venv\Scripts\python.exe" -ArgumentList "main.py" -WorkingDirectory "$DataRoot\ComfyUI" -WindowStyle Hidden
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
# con contraseña (la misma que usa el orquestador, ver watchdog.ps1)
$ocFile = "$DataRoot\data\opencode_password.txt"
if (-not ((Test-Path $ocFile) -and (Get-Content $ocFile -Raw).Trim())) {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    New-Item -ItemType Directory -Force -Path (Split-Path $ocFile) | Out-Null
    Set-Content -Path $ocFile -Encoding ascii -NoNewline `
        -Value ([Convert]::ToBase64String($bytes).Replace("+", "-").Replace("/", "_").TrimEnd("="))
}
$ocPassword = (Get-Content $ocFile -Raw).Trim()
$ocAuth = @{ Authorization = "Basic " + [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("opencode:$ocPassword")) }
$ocUp = $false
try { $ocUp = (Invoke-WebRequest -Uri "http://127.0.0.1:8901/" -Headers $ocAuth -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200 } catch {}
if (-not $ocUp) {
    $env:OPENCODE_SERVER_PASSWORD = $ocPassword
    Start-Process -FilePath "$env:APPDATA\npm\opencode.cmd" -ArgumentList "serve", "--port", "8901", "--hostname", "127.0.0.1" `
        -WorkingDirectory $AiRoot -WindowStyle Hidden
    Write-Host "Agente de codigo arrancando..." -ForegroundColor Yellow
} else {
    Write-Host "El agente de codigo ya esta corriendo." -ForegroundColor Green
}

Start-Sleep -Seconds 5
Write-Host "`n=== Listo. Abre http://localhost:8899 en el navegador. ===" -ForegroundColor Cyan
