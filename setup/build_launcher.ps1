<#
Fabrica ChatiIA.exe (el lanzador: pantalla de carga + ventana de Chati, ver
orchestrator/desktop_app.py) con PyInstaller. Asi en el Administrador de
tareas aparece "Chati IA" con su icono en vez de "Python" - el nombre y el
icono de un proceso salen de su .exe, no se pueden cambiar desde dentro.

Hay que volver a ejecutarlo si cambias desktop_app.py o paths.py, y antes de
compilar el instalador (chati_installer.iss copia la carpeta ChatiIA\).
Resultado: <AiRoot>\ChatiIA\ChatiIA.exe

Uso:  powershell -ExecutionPolicy Bypass -File build_launcher.ps1
#>

param(
    [string]$AiRoot = (Split-Path -Parent $PSScriptRoot)
)
$ErrorActionPreference = "Stop"

$python = "$AiRoot\orchestrator\venv\Scripts\python.exe"
& $python -m PyInstaller --version *> $null
if ($LASTEXITCODE -ne 0) {
    & $python -m pip install pyinstaller
}

$work = Join-Path $env:TEMP "chati_launcher_build"
& $python -m PyInstaller --noconfirm --clean --windowed --onedir `
    --name ChatiIA `
    --icon "$AiRoot\icono.ico" `
    --version-file "$PSScriptRoot\launcher_version.txt" `
    --paths "$AiRoot\orchestrator" `
    --distpath $AiRoot `
    --workpath $work --specpath $work `
    "$AiRoot\orchestrator\desktop_app.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller fallo (codigo $LASTEXITCODE)" }

Write-Host "Lanzador creado: $AiRoot\ChatiIA\ChatiIA.exe" -ForegroundColor Green
