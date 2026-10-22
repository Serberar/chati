<#
Argumentos para arrancar ComfyUI (los usan start_all.ps1 y watchdog.ps1).
La copia de desarrollo (el repo) lee tambien los modelos de la Chati
instalada, si la hay, para no tenerlos dos veces en disco (ver paths.py).
#>

function Get-ComfyArgs($AiRoot, $DataRoot) {
    $installedRoot = [Environment]::GetEnvironmentVariable("CHATI_DATA_ROOT", "User")
    if (-not $installedRoot) { $installedRoot = "$env:LOCALAPPDATA\ChatiIA" }
    $installedYaml = "$installedRoot\ComfyUI\extra_model_paths.yaml"
    if ((Test-Path "$AiRoot\.git") -and ($installedRoot -ne $DataRoot) -and (Test-Path $installedYaml)) {
        return @("main.py", "--extra-model-paths-config", "`"$installedYaml`"")
    }
    return @("main.py")
}
