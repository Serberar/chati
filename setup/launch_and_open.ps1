<#
Arranca todo (Ollama, ComfyUI, orquestador) y abre Chati en su propia
ventana (no una pestaña del navegador normal - ver orchestrator/desktop_app.py)
cuando este listo. Esto es lo que ejecuta el acceso directo del escritorio.
#>

param(
    [string]$AiRoot = (Split-Path -Parent $PSScriptRoot)
)

& powershell -ExecutionPolicy Bypass -File "$PSScriptRoot\start_all.ps1"

& "$AiRoot\orchestrator\venv\Scripts\python.exe" "$AiRoot\orchestrator\desktop_app.py"
