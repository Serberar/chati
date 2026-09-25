<#
Instala el "motor" de la IA personal en un equipo nuevo, asumiendo que la
carpeta C:\AI ya ha sido copiada aqui (o esta en otra ruta, pasala con -AiRoot).

QUE COPIAR al mover de equipo (los pesos, lo caro):
    C:\AI\models\  (la voz de Piper es la unica excepcion - se descarga sola
                    aqui abajo, no hace falta copiarla)
    C:\AI\ComfyUI\  (el codigo fuente esta bien copiarlo, PERO borra antes
                     C:\AI\ComfyUI\venv y C:\AI\ComfyUI\models si van vacios/placeholders)
    C:\AI\setup\

QUE NO COPIAR (se genera de nuevo con este script, depende de cada maquina/GPU):
    C:\AI\ComfyUI\venv
    C:\AI\orchestrator\venv
    C:\AI\sd-scripts\  (entrenamiento de LoRA de persona, ver ROADMAP.md punto
                        8b - se clona de nuevo con "git clone
                        https://github.com/kohya-ss/sd-scripts")

Las dependencias de Python del orquestador se instalan desde
requirements.lock.txt (versiones exactas fijadas con pip freeze), no desde
requirements.txt, para que la instalacion sea reproducible entre equipos.

Pensado para llevar la carpeta entera a otro equipo (disco externo, otro
ordenador con GPU NVIDIA similar) - ver ROADMAP.md, punto 3b. Por defecto usa
la ubicacion donde vive esta carpeta ahora mismo (no C:\AI fijo), asi que
basta con copiarla donde sea y ejecutar este script ahi.

Uso:  powershell -ExecutionPolicy Bypass -File install.ps1 [-AiRoot D:\IA]
#>

param(
    [string]$AiRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = "Stop"

Write-Host "=== Instalando IA personal en $AiRoot ===" -ForegroundColor Cyan

# 1. Ollama (nucleo de texto y codigo)
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Write-Host "Instalando Ollama..." -ForegroundColor Yellow
    winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
} else {
    Write-Host "Ollama ya instalado." -ForegroundColor Green
}

setx OLLAMA_MODELS "$AiRoot\models\text" | Out-Null
$env:OLLAMA_MODELS = "$AiRoot\models\text"
Write-Host "OLLAMA_MODELS -> $AiRoot\models\text" -ForegroundColor Green

setx HF_HOME "$AiRoot\models\voice\hf_cache" | Out-Null
$env:HF_HOME = "$AiRoot\models\voice\hf_cache"
Write-Host "HF_HOME -> $AiRoot\models\voice\hf_cache" -ForegroundColor Green

# Voz de Piper (TTS en espanol) - no se copia entre equipos, se descarga sola
# aqui del repositorio oficial de voces de Piper (investigado y confirmado
# real en el punto 7 del ROADMAP: es_ES/davefx es la unica voz "medium" en
# castellano de España de ese repo). faster_whisper (STT) no necesita este
# paso, se descarga solo la primera vez que se usa.
$piperDir = "$AiRoot\models\voice\piper"
$piperOnnx = "$piperDir\es_ES-davefx-medium.onnx"
if (-not (Test-Path $piperOnnx)) {
    Write-Host "Descargando voz de Piper (es_ES-davefx-medium, ~63MB)..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $piperDir -Force | Out-Null
    $piperBase = "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/davefx/medium"
    Invoke-WebRequest -Uri "$piperBase/es_ES-davefx-medium.onnx" -OutFile $piperOnnx
    Invoke-WebRequest -Uri "$piperBase/es_ES-davefx-medium.onnx.json" -OutFile "$piperOnnx.json"
    Write-Host "Voz de Piper descargada." -ForegroundColor Green
} else {
    Write-Host "Voz de Piper ya presente, no se descarga de nuevo." -ForegroundColor Green
}

# Bug conocido de Ollama con GPUs NVIDIA Blackwell (RTX 50 series - encontrado
# y arreglado en esta misma maquina, ver AGENTS.md): flash attention se activa
# sola y crashea al cargar modelos MoE grandes en GPU. Fijarlo de forma
# permanente aqui para que un equipo nuevo con GPU similar (ej. RTX 5070) no
# tenga que redescubrir el mismo bug.
setx OLLAMA_FLASH_ATTENTION "0" | Out-Null
$env:OLLAMA_FLASH_ATTENTION = "0"
Write-Host "OLLAMA_FLASH_ATTENTION -> 0 (fix GPUs Blackwell, ollama/ollama#18276)" -ForegroundColor Green

Get-Process | Where-Object { $_.ProcessName -match "^ollama" } | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Start-Process -FilePath (Get-Command ollama).Source -ArgumentList "serve" -WindowStyle Hidden
Start-Sleep -Seconds 3
ollama list

# 2. ComfyUI (imagen y video) - recrea el entorno Python, NO se copia entre equipos
$comfy = "$AiRoot\ComfyUI"
if (Test-Path $comfy) {
    if (-not (Test-Path "$comfy\venv")) {
        Write-Host "Creando entorno virtual de ComfyUI..." -ForegroundColor Yellow
        python -m venv "$comfy\venv"
        & "$comfy\venv\Scripts\python.exe" -m pip install --upgrade pip

        # AJUSTA esta linea segun la GPU del equipo nuevo (esto asume NVIDIA/CUDA).
        & "$comfy\venv\Scripts\python.exe" -m pip install torch torchvision --index-url https://download.pytorch.org/whl/cu121

        & "$comfy\venv\Scripts\python.exe" -m pip install -r "$comfy\requirements.txt"

        foreach ($node in Get-ChildItem "$comfy\custom_nodes" -Directory -ErrorAction SilentlyContinue) {
            $req = Join-Path $node.FullName "requirements.txt"
            if (Test-Path $req) {
                & "$comfy\venv\Scripts\python.exe" -m pip install -r $req
            }
        }
        Write-Host "Entorno de ComfyUI creado." -ForegroundColor Green
    } else {
        Write-Host "El venv de ComfyUI ya existe, no se toca." -ForegroundColor Green
    }

    if (-not (Test-Path "$comfy\extra_model_paths.yaml")) {
        Write-Host "AVISO: falta extra_model_paths.yaml en $comfy - sin el, ComfyUI no encontrara los modelos en $AiRoot\models\img y \vid" -ForegroundColor Red
    }
} else {
    Write-Host "No se encontro $comfy - copia la carpeta ComfyUI (sin venv) antes de ejecutar esto." -ForegroundColor Red
}


# 3. Orquestador (texto/codigo/imagen/video/voz/RAG/memoria/auth) - su propio venv
$orch = "$AiRoot\orchestrator"
if (Test-Path $orch) {
    if (-not (Test-Path "$orch\venv")) {
        Write-Host "Creando entorno virtual del orquestador..." -ForegroundColor Yellow
        python -m venv "$orch\venv"
        & "$orch\venv\Scripts\python.exe" -m pip install --upgrade pip

        $lockFile = "$orch\requirements.lock.txt"
        if (Test-Path $lockFile) {
            & "$orch\venv\Scripts\python.exe" -m pip install -r $lockFile
        } else {
            Write-Host "AVISO: no se encontro requirements.lock.txt, instalando desde requirements.txt (versiones sin fijar)" -ForegroundColor Yellow
            & "$orch\venv\Scripts\python.exe" -m pip install -r "$orch\requirements.txt"
        }

        & "$orch\venv\Scripts\python.exe" -m playwright install chromium

        Write-Host "Entorno del orquestador creado." -ForegroundColor Green
    } else {
        Write-Host "El venv del orquestador ya existe, no se toca." -ForegroundColor Green
    }
} else {
    Write-Host "No se encontro $orch - copia la carpeta orchestrator antes de ejecutar esto." -ForegroundColor Red
}

# 4. Node.js + OpenCode (agente de codigo local, ver AGENTS.md)
if (-not (Get-Command node -ErrorAction SilentlyContinue)) {
    Write-Host "Instalando Node.js..." -ForegroundColor Yellow
    winget install --id OpenJS.NodeJS.LTS -e --accept-source-agreements --accept-package-agreements
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
} else {
    Write-Host "Node.js ya instalado." -ForegroundColor Green
}

if (-not (Get-Command opencode -ErrorAction SilentlyContinue)) {
    Write-Host "Instalando OpenCode..." -ForegroundColor Yellow
    npm install -g opencode-ai
} else {
    Write-Host "OpenCode ya instalado." -ForegroundColor Green
}

# Configuracion de OpenCode (vive en el perfil de usuario, no viaja con la
# carpeta al copiarla) - se regenera aqui apuntando a los modelos locales.
$opencodeConfigDir = "$env:USERPROFILE\.config\opencode"
if (-not (Test-Path $opencodeConfigDir)) {
    New-Item -ItemType Directory -Path $opencodeConfigDir -Force | Out-Null
}
$opencodeConfig = @'
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama (local)",
      "options": { "baseURL": "http://127.0.0.1:11434/v1" },
      "models": {
        "qwen3-coder:30b-cpu": { "name": "Qwen3 Coder 30B-A3B (local, CPU)" }
      }
    }
  },
  "model": "ollama/qwen3-coder:30b-cpu",
  "instructions": ["AGENTS.md"],
  "permission": {
    "edit": "ask",
    "webfetch": "ask",
    "todowrite": "allow",
    "bash": {
      "git push*": "ask", "git reset --hard*": "ask", "git clean*": "ask",
      "rm *": "ask", "rm -rf*": "ask", "del *": "ask", "Remove-Item*": "ask",
      "python -m pytest*": "allow", "pytest*": "allow",
      "git status*": "allow", "git diff*": "allow", "git log*": "allow",
      "*": "ask"
    }
  },
  "agent": { "build": { "temperature": 0.1 }, "plan": { "temperature": 0.1 } }
}
'@
Set-Content -Path "$opencodeConfigDir\opencode.json" -Value $opencodeConfig -Encoding utf8
Write-Host "Config de OpenCode escrita en $opencodeConfigDir\opencode.json" -ForegroundColor Green

# 5. Acceso directo del escritorio + vigilante automatico
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut("$desktop\Arrancar IA Personal.lnk")
$shortcut.TargetPath = "powershell.exe"
$shortcut.Arguments = "-ExecutionPolicy Bypass -File `"$AiRoot\setup\launch_and_open.ps1`""
$shortcut.Save()
Write-Host "Acceso directo creado en el escritorio." -ForegroundColor Green

& powershell -ExecutionPolicy Bypass -File "$AiRoot\setup\install_watchdog.ps1"

Write-Host "`n=== Listo. Modelos de texto/codigo disponibles via 'ollama list'. ===" -ForegroundColor Cyan
Write-Host "Para arrancar todo: doble clic en 'Arrancar IA Personal' del escritorio," -ForegroundColor Cyan
Write-Host "o powershell -ExecutionPolicy Bypass -File $AiRoot\setup\start_all.ps1" -ForegroundColor Cyan
Write-Host "La clave API se genera sola en $AiRoot\data\api_key.txt la primera vez que arranca el orquestador." -ForegroundColor Cyan
