<#
Arranca todo (Ollama, ComfyUI, orquestador) y abre el navegador cuando este
listo. Esto es lo que ejecuta el acceso directo del escritorio.
#>

& powershell -ExecutionPolicy Bypass -File "$PSScriptRoot\start_all.ps1"

Write-Host "Esperando a que el orquestador responda..." -ForegroundColor Cyan
$deadline = (Get-Date).AddSeconds(120)
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:8899/health" -UseBasicParsing -TimeoutSec 3
        if ($r.StatusCode -eq 200) { break }
    } catch {}
    Start-Sleep -Seconds 2
}

Start-Process "http://127.0.0.1:8899"
