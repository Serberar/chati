<#
Cierra Chati antes de instalar encima (lo llama chati_installer.iss, tambien
al actualizar desde la app). Primero el vigilante, que si no la vuelve a
arrancar; despues todo lo que corre desde la carpeta del programa (la
ventana ChatiIA.exe bloquea sus archivos, y el orquestador y las
herramientas del agente usan su venv) y OpenCode, que carga esas
herramientas. Ollama y ComfyUI no se tocan: viven fuera de esa carpeta.

-All: "Cerrar Chati" desde el icono junto al reloj (desktop_app.py). Ademas
cierra ComfyUI y Ollama (tambien su propio icono), para liberar la memoria y
la GPU que tienen los modelos cargados.

Uso:  powershell -ExecutionPolicy Bypass -File stop_chati.ps1 -AiRoot "C:\Program Files\Chati IA" [-All]
#>

param([Parameter(Mandatory = $true)] [string]$AiRoot, [switch]$All)

$root = [IO.Path]::GetFullPath($AiRoot).TrimEnd("\") + "\"
# start_all.ps1 -Stay lleva el vigilante dentro
$watchdog = [regex]::Escape("$root" + "setup\") + "(watchdog|start_all)\.ps1"

function Get-ChatiProcesses {
    Get-CimInstance Win32_Process | Where-Object {
        ($_.CommandLine -match $watchdog) -or
        ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($root, "OrdinalIgnoreCase"))
    }
}

foreach ($p in Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match $watchdog }) {
    # con -All, todo lo que lanzo (y lo que lanzaron esos, p.ej. los modelos
    # de Ollama o los procesos de OpenCode)
    if ($All) { taskkill.exe /PID $p.ProcessId /T /F *> $null }
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
if ($All) {
    foreach ($c in Get-NetTCPConnection -LocalPort 8188 -State Listen -ErrorAction SilentlyContinue) {
        # ComfyUI tambien va en pareja (su venv); el padre solo si es python,
        # por si se arranco a mano desde una terminal
        $parent = Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)" -ErrorAction SilentlyContinue
        $parent = if ($parent) { Get-Process -Id $parent.ParentProcessId -ErrorAction SilentlyContinue }
        if ($parent -and $parent.Name -eq "python") { Stop-Process -Id $parent.Id -Force -ErrorAction SilentlyContinue }
        Stop-Process -Id $c.OwningProcess -Force -ErrorAction SilentlyContinue
    }
    # "ollama app" es su icono junto al reloj; los modelos cargados son
    # procesos ollama.exe hijos del servidor
    Get-Process -Name "ollama app", "ollama" -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}

$deadline = (Get-Date).AddSeconds(20)
while ((Get-ChatiProcesses) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
