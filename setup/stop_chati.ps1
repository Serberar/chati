<#
Cierra Chati antes de instalar encima (lo llama chati_installer.iss, tambien
al actualizar desde la app). Primero el vigilante, que si no la vuelve a
arrancar; despues todo lo que corre desde la carpeta del programa (la
ventana ChatiIA.exe bloquea sus archivos, y el orquestador y las
herramientas del agente usan su venv) y OpenCode, que carga esas
herramientas. Ollama y ComfyUI no se tocan: viven fuera de esa carpeta.

Uso:  powershell -ExecutionPolicy Bypass -File stop_chati.ps1 -AiRoot "C:\Program Files\Chati IA"
#>

param([Parameter(Mandatory = $true)] [string]$AiRoot)

$root = [IO.Path]::GetFullPath($AiRoot).TrimEnd("\") + "\"
$watchdog = [regex]::Escape("$root" + "setup\watchdog.ps1")

function Get-ChatiProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        ($_.CommandLine -match $watchdog) -or
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, "OrdinalIgnoreCase"))
    }
}

foreach ($p in Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $watchdog }) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
foreach ($p in Get-ChatiProcesses) {
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
# el venv de Windows arranca en pareja: el hijo es el python.exe base, que no
# vive en la carpeta del programa (ver Stop-ByPort en watchdog.ps1)
foreach ($port in 8899, 8901) {
    foreach ($c in Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) {
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
}

$deadline = (Get-Date).AddSeconds(20)
while ((Get-ChatiProcesses) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
