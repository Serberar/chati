<#
Abre Chati IA sin consola. Lo mismo que hace el acceso directo del
escritorio (pythonw.exe + desktop_app.py, que arranca los servicios y
muestra una pantalla de carga) - se mantiene por si algo lo sigue llamando.
#>

param(
    [string]$AiRoot = (Split-Path -Parent $PSScriptRoot)
)

Start-Process -FilePath "$AiRoot\orchestrator\venv\Scripts\pythonw.exe" `
    -ArgumentList "`"$AiRoot\orchestrator\desktop_app.py`"" -WorkingDirectory "$AiRoot\orchestrator"
