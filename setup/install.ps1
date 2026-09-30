<#
Instala el "motor" de Chati IA en un equipo nuevo. Dos raices separadas
(ver ROADMAP.md, "instalador"):

  -AiRoot    donde vive el CODIGO (orchestrator/, setup/) - con el
             instalador .exe esto es C:\Program Files\Chati IA\, de solo
             lectura en uso normal.
  -DataRoot  donde vive todo lo que la app ESCRIBE (modelos, datos de
             usuarios, imagenes/videos generados, y tambien ComfyUI y
             sd-scripts enteros - necesitan escribir mucho, venv+paquetes+
             modelos, no tiene sentido meterlos donde el codigo es de solo
             lectura). Por defecto %LOCALAPPDATA%\ChatiIA - nunca pide
             permisos de administrador para escribir ahi, a diferencia de
             Archivos de Programa.

Si quieres el modo antiguo "todo en una sola carpeta" (llevarla a mano a
otro equipo, ver punto 3b), pasa el mismo valor en -AiRoot y -DataRoot.

QUE COPIAR al mover de equipo (los pesos, lo caro - lo unico que este
script no puede recrear por su cuenta):
    <DataRoot>\models\  (la voz de Piper es la unica excepcion - se
                         descarga sola aqui abajo)
    <AiRoot>\setup\

QUE NO HACE FALTA COPIAR (este script lo clona/genera de nuevo si falta):
    <DataRoot>\ComfyUI\   (se clona de Comfy-Org/ComfyUI + 3 nodos
                           personalizados: GGUF, IPAdapter, Manager)
    <DataRoot>\sd-scripts\ (entrenamiento de LoRA de persona, punto 8b -
                            se clona de kohya-ss/sd-scripts)
    <DataRoot>\ComfyUI\venv, <DataRoot>\sd-scripts\venv, <AiRoot>\orchestrator\venv

Uso:  powershell -ExecutionPolicy Bypass -File install.ps1 [-AiRoot D:\IA] [-DataRoot D:\IA]
#>

param(
    [string]$AiRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DataRoot = $(if ($env:LOCALAPPDATA) { "$env:LOCALAPPDATA\ChatiIA" } else { (Split-Path -Parent $PSScriptRoot) })
)

$ErrorActionPreference = "Stop"
# En Windows PowerShell 5.1 la barra de progreso de Invoke-WebRequest ralentiza
# muchisimo las descargas (visto en Windows Sandbox con el instalador de Ollama)
$ProgressPreference = "SilentlyContinue"

Write-Host "=== Instalando Chati IA ===" -ForegroundColor Cyan
Write-Host "Codigo: $AiRoot" -ForegroundColor Cyan
Write-Host "Datos y modelos: $DataRoot" -ForegroundColor Cyan

# La app en si (main.py -> paths.py) lee esta variable para saber donde
# escribir - fijarla aqui garantiza que coincide exactamente con lo que
# este script acaba de preparar, sin depender de que el calculo por
# defecto de paths.py llegue al mismo sitio por su cuenta.
setx CHATI_DATA_ROOT "$DataRoot" | Out-Null
$env:CHATI_DATA_ROOT = $DataRoot

# Fuera del perfil del usuario (p.ej. C:\Chati en modo "todo junto"), una
# carpeta hereda de C:\ "Modificar" para cualquier cuenta de Windows: otra
# cuenta del equipo podria leer las claves de data\ o cambiar lo que se
# ejecuta (auditoria 2026-09-29). Se deja solo para este usuario, SYSTEM y
# los administradores. En %LOCALAPPDATA% ya es privada.
New-Item -ItemType Directory -Force -Path $DataRoot | Out-Null
if (-not ([IO.Path]::GetFullPath($DataRoot).StartsWith([IO.Path]::GetFullPath($env:USERPROFILE), "OrdinalIgnoreCase"))) {
    # Solo en la carpeta (sin /T) y luego todo lo de dentro vuelve a heredar:
    # con /T, (OI)(CI) no vale en archivos y se quedaban SIN ningun permiso
    # (paso de verdad el 2026-09-30: users.db inaccesible hasta repararlo)
    $me = "${env:USERDOMAIN}\${env:USERNAME}"
    icacls $DataRoot /inheritance:r /grant:r "${me}:(OI)(CI)F" "*S-1-5-18:(OI)(CI)F" "*S-1-5-32-544:(OI)(CI)F" /Q | Out-Null
    if (Get-ChildItem -LiteralPath $DataRoot -Force) { icacls "$DataRoot\*" /reset /T /C /Q | Out-Null }
    Write-Host "Carpeta de datos restringida a $env:USERNAME (fuera del perfil de usuario)." -ForegroundColor Green
}

function Update-Path {
    $env:Path = [System.Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [System.Environment]::GetEnvironmentVariable("Path","User")
}

# Programas de fuera: con winget si lo hay; si no (Windows Sandbox, algunos
# Windows 10/LTSC), el instalador oficial de una version fija, y SOLO si su
# firma digital es valida y de quien debe ser (auditoria 2026-09-30).
function Install-Tool($name, $wingetId, $url, $signer, $installerArgs, [switch]$Msi, [int[]]$OkCodes = @(0)) {
    Write-Host "Instalando $name..." -ForegroundColor Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id $wingetId -e --silent --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -eq 0) { Update-Path; return }
        Write-Host "winget no pudo instalar ${name}; se usa el instalador oficial." -ForegroundColor Yellow
    }
    $file = Join-Path $env:TEMP (Split-Path $url -Leaf)
    Write-Host "  Descargando $url ..." -ForegroundColor DarkGray
    Invoke-WebRequest -Uri $url -OutFile $file -UseBasicParsing
    $sig = Get-AuthenticodeSignature $file
    if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notlike "CN=$signer*") {
        Remove-Item $file -Force
        throw "El instalador de $name no tiene la firma esperada ($signer). No se ejecuta."
    }
    Write-Host "  Firma correcta ($signer). Instalando..." -ForegroundColor DarkGray
    # WaitForExit y no "Start-Process -Wait": -Wait espera tambien a todo lo que
    # lance el instalador, y el de Ollama deja su aplicacion abierta para siempre
    # (se quedo 2 h "Instalando Ollama" en Windows Sandbox, 2026-09-30)
    if ($Msi) {
        $p = Start-Process msiexec.exe -ArgumentList "/i `"$file`" /qn /norestart" -PassThru
    } else {
        $p = Start-Process $file -ArgumentList $installerArgs -PassThru
    }
    $p.WaitForExit()
    Remove-Item $file -Force -ErrorAction SilentlyContinue
    if ($OkCodes -notcontains $p.ExitCode) { throw "El instalador de $name fallo (codigo $($p.ExitCode))." }
    Update-Path
}

# 0. Librerias de Visual C++: un Windows recien instalado no las trae, y sin
# ellas no cargan PyTorch (ComfyUI) ni la voz (faster_whisper) - visto en
# Windows Sandbox el 2026-09-30: ComfyUI y el orquestador se caian al arrancar.
$vcRuntime = Get-ItemProperty "HKLM:\SOFTWARE\Microsoft\VisualStudio\14.0\VC\Runtimes\x64" -ErrorAction SilentlyContinue
if (-not ($vcRuntime -and $vcRuntime.Installed -eq 1)) {
    # 3010 = instalado, pide reiniciar; 1638 = ya habia una version mas nueva
    Install-Tool "Visual C++ (librerias de Microsoft)" "Microsoft.VCRedist.2015+.x64" `
        "https://aka.ms/vs/17/release/vc_redist.x64.exe" "Microsoft Corporation" "/install /quiet /norestart" `
        -OkCodes 0, 3010, 1638
} else {
    Write-Host "Librerias de Visual C++ ya instaladas." -ForegroundColor Green
}

# 1. Ollama (nucleo de texto y codigo)
if (-not (Get-Command ollama -ErrorAction SilentlyContinue)) {
    Install-Tool "Ollama" "Ollama.Ollama" "https://github.com/ollama/ollama/releases/download/v0.34.4/OllamaSetup.exe" `
        "Ollama Inc" "/VERYSILENT /NORESTART /SUPPRESSMSGBOXES"
} else {
    Write-Host "Ollama ya instalado." -ForegroundColor Green
}

# 1a. Python 3.12 (entornos de ComfyUI, sd-scripts y el orquestador: el
# lockfile y PyTorch estan probados con 3.12). En un Windows recien instalado
# "python" suele ser el acceso directo que abre la Microsoft Store: se busca
# la ruta REAL del python.exe y se usa esa en todo el script.
function Find-Python312 {
    $candidates = @()
    try { $candidates += (& py -3.12 -c "import sys; print(sys.executable)" 2>$null) } catch { }
    $candidates += "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe", "$env:ProgramFiles\Python312\python.exe"
    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c) -and ($c -notlike "*\WindowsApps\*")) {
            if (((& $c --version 2>&1) -join " ") -match "Python 3\.12\.") { return $c }
        }
    }
    return $null
}
$PythonExe = Find-Python312
if (-not $PythonExe) {
    Install-Tool "Python 3.12" "Python.Python.3.12" "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe" `
        "Python Software Foundation" "/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 Include_test=0"
    $PythonExe = Find-Python312
    if (-not $PythonExe) { throw "No se encuentra Python 3.12 despues de instalarlo." }
}
Write-Host "Python: $PythonExe" -ForegroundColor Green

# 1b. Git: para clonar ComfyUI y sd-scripts, y para que el agente pueda
# registrar y deshacer sus cambios (su carpeta de trabajo es un repositorio
# git, ver opencode_client.WORKDIR). Sin git no hay "Ver cambios / Deshacer".
if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Install-Tool "Git" "Git.Git" "https://github.com/git-for-windows/git/releases/download/v2.55.0.windows.4/Git-2.55.0.4-64-bit.exe" `
        "Johannes Schindelin" "/VERYSILENT /NORESTART /NOCANCEL /SP- /SUPPRESSMSGBOXES"
} else {
    Write-Host "Git ya instalado." -ForegroundColor Green
}

setx OLLAMA_MODELS "$DataRoot\models\text" | Out-Null
$env:OLLAMA_MODELS = "$DataRoot\models\text"
Write-Host "OLLAMA_MODELS -> $DataRoot\models\text" -ForegroundColor Green

setx HF_HOME "$DataRoot\models\voice\hf_cache" | Out-Null
$env:HF_HOME = "$DataRoot\models\voice\hf_cache"
Write-Host "HF_HOME -> $DataRoot\models\voice\hf_cache" -ForegroundColor Green

# Voz de Piper (TTS en espanol) - no se copia entre equipos, se descarga sola
# aqui del repositorio oficial de voces de Piper (investigado y confirmado
# real en el punto 7 del ROADMAP: es_ES/davefx es la unica voz "medium" en
# castellano de España de ese repo). faster_whisper (STT) no necesita este
# paso, se descarga solo la primera vez que se usa.
$piperDir = "$DataRoot\models\voice\piper"
$piperOnnx = "$piperDir\es_ES-davefx-medium.onnx"
if (-not (Test-Path $piperOnnx)) {
    Write-Host "Descargando voz de Piper (es_ES-davefx-medium, ~63MB)..." -ForegroundColor Yellow
    New-Item -ItemType Directory -Path $piperDir -Force | Out-Null
    $piperBase = "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/davefx/medium"
    Invoke-WebRequest -Uri "$piperBase/es_ES-davefx-medium.onnx" -OutFile $piperOnnx
    Invoke-WebRequest -Uri "$piperBase/es_ES-davefx-medium.onnx.json" -OutFile "$piperOnnx.json"
    # huellas comprobadas el 2026-09-29: si el archivo cambia en el servidor, no se usa
    $expected = @{
        $piperOnnx = "6658b03b1a6c316ee4c265a9896abc1393353c2d9e1bca7d66c2c442e222a917"
        "$piperOnnx.json" = "0e0dda87c732f6f38771ff274a6380d9252f327dca77aa2963d5fbdf9ec54842"
    }
    foreach ($f in $expected.Keys) {
        if ((Get-FileHash $f -Algorithm SHA256).Hash.ToLower() -ne $expected[$f]) {
            Remove-Item $f -Force
            throw "La voz de Piper descargada no coincide con la esperada ($f). Se ha borrado por seguridad."
        }
    }
    Write-Host "Voz de Piper descargada y comprobada." -ForegroundColor Green
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

# Versiones probadas juntas en el equipo de desarrollo (auditoria 2026-09-29:
# antes se instalaba "lo ultimo" de cada cosa, y una instalacion nueva podia
# romperse -o traer codigo cambiado- de un dia para otro). Para actualizar:
# probar en desarrollo y cambiar aqui el commit/version.
$ComfyCommit = "30bdda1ef13a3a34fce2cd2fec633f15d832122a"
$SdScriptsCommit = "690ea7f96c23182352ec63def76d431c6120bd2f"
$TorchPackages = @("torch==2.12.0.dev20260408+cu128", "torchvision==0.27.0.dev20260407+cu128",
                   "torchaudio==2.11.0.dev20260407+cu128")
$OpenCodeVersion = "1.18.32"

function Install-Torch($python) {
    # Las versiones nightly desaparecen del servidor al cabo de un tiempo: si
    # la fijada ya no esta, se instala la nightly del momento y se avisa.
    & $python -m pip install --pre @TorchPackages --index-url https://download.pytorch.org/whl/nightly/cu128
    if ($LASTEXITCODE -ne 0) {
        Write-Host "AVISO: la version probada de PyTorch ya no esta disponible; se instala la nightly actual (sin probar)." -ForegroundColor Yellow
        & $python -m pip install --pre torch torchvision torchaudio --index-url https://download.pytorch.org/whl/nightly/cu128
    }
}

function Checkout-Pinned($dir, $commit) {
    git -C $dir fetch --quiet origin $commit 2>$null
    git -C $dir checkout --quiet $commit
    if ($LASTEXITCODE -ne 0) { Write-Host "AVISO: no se pudo fijar $dir en $commit" -ForegroundColor Yellow }
}

# 2. ComfyUI (imagen y video) - se clona solo si falta, recrea el entorno
# Python siempre (NO se copia entre equipos, depende de la GPU). Vive en
# DataRoot: necesita escribir su venv y los custom_nodes libremente.
$comfy = "$DataRoot\ComfyUI"
if (-not (Test-Path $comfy)) {
    Write-Host "Clonando ComfyUI..." -ForegroundColor Yellow
    git clone https://github.com/Comfy-Org/ComfyUI.git $comfy
    Checkout-Pinned $comfy $ComfyCommit
}

$comfyNodes = @{
    # necesario para FLUX (GGUF)
    "ComfyUI-GGUF"           = @("https://github.com/city96/ComfyUI-GGUF.git", "6ea2651e7df66d7585f6ffee804b20e92fb38b8a")
    # gestion de nodos desde la propia interfaz
    "ComfyUI-Manager"        = @("https://github.com/Comfy-Org/ComfyUI-Manager.git", "b75fc664ecab9c4602380d9660833d02f6a63333")
    # necesario para preservar caras (FaceID)
    "ComfyUI_IPAdapter_plus" = @("https://github.com/cubiq/ComfyUI_IPAdapter_plus.git", "a0f451a5113cf9becb0847b92884cb10cbdec0ef")
}
foreach ($name in $comfyNodes.Keys) {
    $nodeDir = "$comfy\custom_nodes\$name"
    if (-not (Test-Path $nodeDir)) {
        Write-Host "Clonando nodo personalizado $name..." -ForegroundColor Yellow
        git clone $comfyNodes[$name][0] $nodeDir
        Checkout-Pinned $nodeDir $comfyNodes[$name][1]
    }
}

if (-not (Test-Path "$comfy\venv")) {
    Write-Host "Creando entorno virtual de ComfyUI..." -ForegroundColor Yellow
    & $PythonExe -m venv "$comfy\venv"
    & "$comfy\venv\Scripts\python.exe" -m pip install --upgrade pip

    # Mismo PyTorch nightly que el resto del sistema (cu128, no cu121) - las
    # GPUs Blackwell (RTX 50 series) necesitan esta version concreta, ver
    # AGENTS.md y ROADMAP.md. AJUSTA el index-url si el equipo nuevo tiene
    # una GPU mas antigua que ya soporte una version estable de PyTorch.
    Install-Torch "$comfy\venv\Scripts\python.exe"

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

$extraModelPaths = @'
# Rutas centralizadas de modelos - Chati IA
# Los pesos viven fuera de ComfyUI, organizados por agente (img/vid), para
# poder actualizarlos/copiarlos sin depender de la instalacion de ComfyUI.

img_models:
    base_path: {0}\models\img\
    checkpoints: checkpoints
    diffusion_models: diffusion_models
    text_encoders: text_encoders
    vae: vae
    ipadapter: ipadapter
    loras: loras
    clip_vision: clip_vision
    upscale_models: upscale_models
    controlnet: controlnet

vid_models:
    base_path: {0}\models\vid\
    checkpoints: checkpoints
'@ -f $DataRoot
$extraModelPathsFile = "$comfy\extra_model_paths.yaml"
if (-not (Test-Path $extraModelPathsFile)) {
    Set-Content -Path $extraModelPathsFile -Value $extraModelPaths -Encoding utf8
    Write-Host "extra_model_paths.yaml generado." -ForegroundColor Green
}

# 2b. sd-scripts (entrenamiento de LoRA de persona, ver ROADMAP.md punto 8b)
# - se clona solo si falta, mismo patron de venv+torch que ComfyUI, tambien
# en DataRoot (venv + paquetes propios, necesita escribir libremente)
$sdScripts = "$DataRoot\sd-scripts"
if (-not (Test-Path $sdScripts)) {
    Write-Host "Clonando sd-scripts..." -ForegroundColor Yellow
    git clone https://github.com/kohya-ss/sd-scripts.git $sdScripts
    Checkout-Pinned $sdScripts $SdScriptsCommit
}
if (-not (Test-Path "$sdScripts\venv")) {
    Write-Host "Creando entorno virtual de sd-scripts..." -ForegroundColor Yellow
    & $PythonExe -m venv "$sdScripts\venv"
    & "$sdScripts\venv\Scripts\python.exe" -m pip install --upgrade pip
    Install-Torch "$sdScripts\venv\Scripts\python.exe"
    & "$sdScripts\venv\Scripts\python.exe" -m pip install -r "$sdScripts\requirements.txt"
    Write-Host "Entorno de sd-scripts creado." -ForegroundColor Green
} else {
    Write-Host "El venv de sd-scripts ya existe, no se toca." -ForegroundColor Green
}

# 3. Orquestador (texto/codigo/imagen/video/voz/RAG/memoria/auth) - vive en
# AiRoot (codigo), su venv tambien - un venv es "codigo instalado", no dato
# de usuario, y no cambia salvo que se reinstale la app.
$orch = "$AiRoot\orchestrator"
if (Test-Path $orch) {
    if (-not (Test-Path "$orch\venv")) {
        Write-Host "Creando entorno virtual del orquestador..." -ForegroundColor Yellow
        & $PythonExe -m venv "$orch\venv"
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
    Install-Tool "Node.js" "OpenJS.NodeJS.LTS" "https://nodejs.org/dist/v24.19.0/node-v24.19.0-x64.msi" `
        "OpenJS Foundation" "" -Msi
} else {
    Write-Host "Node.js ya instalado." -ForegroundColor Green
}

if (-not (Get-Command opencode -ErrorAction SilentlyContinue)) {
    Write-Host "Instalando OpenCode..." -ForegroundColor Yellow
    npm install -g "opencode-ai@$OpenCodeVersion"
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
      "options": { "baseURL": "http://127.0.0.1:8899/llm/v1", "apiKey": "{env:OPENCODE_SERVER_PASSWORD}" },
      "models": {
        "qwen2.5:7b": { "name": "Qwen2.5 7B (local, GPU)", "limit": { "context": 16384, "output": 4096 } },
        "qwen3:8b": { "name": "Qwen3 8B (local, GPU)", "limit": { "context": 16384, "output": 4096 } },
        "qwen3-coder:30b-cpu": { "name": "Qwen3 Coder 30B-A3B (local, CPU)", "limit": { "context": 32768, "output": 8192 } }
      }
    }
  },
  "model": "ollama/qwen3-coder:30b-cpu",
  "small_model": "ollama/qwen3:8b",
  "shell": "powershell",
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
  "agent": {
    "build": { "temperature": 0.1 },
    "plan": { "temperature": 0.1 },
    "chati": {
      "description": "Agente de Chati IA: tareas sobre los archivos del usuario (rapido, GPU)",
      "mode": "primary", "model": "ollama/qwen3:8b", "temperature": 0.1,
      "prompt": "{file:./chati_agent_prompt.md}",
      "tools": { "task": false, "todowrite": false, "webfetch": false, "skill": false, "write": false }
    },
    "chati-potente": {
      "description": "Agente de Chati IA con el modelo grande (lento, CPU) para tareas complejas",
      "mode": "primary", "model": "ollama/qwen3-coder:30b-cpu", "temperature": 0.1,
      "prompt": "{file:./chati_agent_prompt.md}",
      "tools": { "task": false, "todowrite": false, "webfetch": false, "skill": false }
    },
    "chati-code": {
      "description": "Chati modo Codigo: programa en el proyecto del usuario (construir)",
      "mode": "primary", "model": "ollama/qwen3-coder:30b-cpu", "temperature": 0.1,
      "prompt": "{file:./chati_code_prompt.md}",
      "tools": { "task": false, "webfetch": false, "skill": false }
    },
    "chati-code-plan": {
      "description": "Chati modo Codigo: analiza el proyecto y propone, sin cambiar nada (planificar)",
      "mode": "primary", "model": "ollama/qwen3-coder:30b-cpu", "temperature": 0.1,
      "prompt": "{file:./chati_code_plan_prompt.md}",
      "tools": { "task": false, "webfetch": false, "skill": false, "edit": false, "write": false, "patch": false }
    }
  }
}
'@
# baseURL: la pasarela de Chati (main.py, /llm/v1) y no Ollama directamente -
# quita los parametros null que el modelo pequeño pone en las herramientas.
# Agentes "chati" y "chati-potente" (los usa Chati, ver orchestrator/config.yaml):
# instrucciones cortas propias en vez de las de OpenCode - medido: una tarea
# "crea un archivo" paso de 8-10 min a 15s (chati) / 2-3 min (potente).
Copy-Item "$PSScriptRoot\chati_agent_prompt.md" "$opencodeConfigDir\chati_agent_prompt.md" -Force
# modo Codigo (chati-code / chati-code-plan)
Copy-Item "$PSScriptRoot\chati_code_prompt.md" "$opencodeConfigDir\chati_code_prompt.md" -Force
Copy-Item "$PSScriptRoot\chati_code_plan_prompt.md" "$opencodeConfigDir\chati_code_plan_prompt.md" -Force
Set-Content -Path "$opencodeConfigDir\opencode.json" -Value $opencodeConfig -Encoding utf8
Write-Host "Config de OpenCode escrita en $opencodeConfigDir\opencode.json" -ForegroundColor Green

# 5. Acceso directo del escritorio + vigilante automatico
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut("$desktop\Chati IA.lnk")
# ChatiIA.exe (setup/build_launcher.ps1): sin consola, y en el Administrador
# de tareas sale "Chati IA" con su icono. Si no se ha compilado (instalacion
# a mano desde el codigo), pythonw.exe hace lo mismo pero figura como Python.
if (Test-Path "$AiRoot\ChatiIA\ChatiIA.exe") {
    $shortcut.TargetPath = "$AiRoot\ChatiIA\ChatiIA.exe"
    $shortcut.WorkingDirectory = "$AiRoot\ChatiIA"
} else {
    $shortcut.TargetPath = "$AiRoot\orchestrator\venv\Scripts\pythonw.exe"
    $shortcut.Arguments = "`"$AiRoot\orchestrator\desktop_app.py`""
    $shortcut.WorkingDirectory = "$AiRoot\orchestrator"
}
$shortcut.IconLocation = "$AiRoot\icono.ico"
$shortcut.Save()
Write-Host "Acceso directo 'Chati IA' creado en el escritorio." -ForegroundColor Green

& powershell -ExecutionPolicy Bypass -File "$AiRoot\setup\install_watchdog.ps1"

Write-Host "`n=== Listo. Modelos de texto/codigo disponibles via 'ollama list'. ===" -ForegroundColor Cyan
Write-Host "Para arrancar todo: doble clic en 'Chati IA' del escritorio," -ForegroundColor Cyan
Write-Host "o powershell -ExecutionPolicy Bypass -File $AiRoot\setup\start_all.ps1" -ForegroundColor Cyan
Write-Host "Los datos de usuarios se generan solos en $DataRoot\data la primera vez que arranca el orquestador." -ForegroundColor Cyan
