<#
Compila el instalador de Chati con la version de orchestrator\version.py:
setup\Output\ChatiIA-Instalador.exe. Hace antes el lanzador (build_launcher.ps1,
necesita orchestrator\venv - ver README).

Para publicar una version: sube VERSION en orchestrator\version.py, ejecuta
esto y crea una release "vX.Y.Z" en GitHub con ese .exe adjunto (README).
Las Chati instaladas la ven en Opciones y se actualizan con un boton.

Uso:  powershell -ExecutionPolicy Bypass -File build_release.ps1
#>

$ErrorActionPreference = "Stop"
$AiRoot = Split-Path -Parent $PSScriptRoot

$version = (Select-String -Path "$AiRoot\orchestrator\version.py" -Pattern '^VERSION = "(.+)"').Matches[0].Groups[1].Value
Write-Host "Compilando Chati $version" -ForegroundColor Cyan

& powershell -ExecutionPolicy Bypass -File "$PSScriptRoot\build_launcher.ps1"
if ($LASTEXITCODE -ne 0) { throw "No se pudo crear el lanzador (build_launcher.ps1)." }

$iscc = (Get-Command iscc -ErrorAction SilentlyContinue).Source
if (-not $iscc) {
    $iscc = Get-ChildItem "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe" -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty FullName
}
if (-not $iscc) { throw "Falta Inno Setup 6 (winget install JRSoftware.InnoSetup)" }
& $iscc "/DMyAppVersion=$version" "$PSScriptRoot\chati_installer.iss"
if ($LASTEXITCODE -ne 0) { throw "Inno Setup fallo (codigo $LASTEXITCODE)" }

Write-Host "Listo: setup\Output\ChatiIA-Instalador.exe (v$version)" -ForegroundColor Green
