$o = (Resolve-Path "$PSScriptRoot\..\..\orchestrator").Path
$s = $PSScriptRoot
$dir = "C:\Users\Sergio Bernabe\Desktop\pruebas"
$env:PYTHONPATH = $o
$env:PYTHONIOENCODING = "utf-8"
$tasks = @(
    "en el escritorio, en la carpeta pruebas/notas, hay un Notas.exe que al abrirlo no hace nada. arreglalo, quiero que se vea la interfaz",
    "el programa calcular_total.py de la carpeta pruebas/facturas del escritorio me da error al ejecutarlo. arreglalo",
    "en pruebas/contador del escritorio tengo un contador con ventana, el boton de sumar no hace nada. arreglalo",
    "el informe.py de la carpeta pruebas/informe del escritorio no funciona, arreglalo"
)
foreach ($t in $tasks) {
    "################ $t"
    $t0 = Get-Date
    & "$o\venv\Scripts\python.exe" "$s\probar_agente.py" $dir $t 2>&1
    "################ tiempo: {0:N0} s" -f ((Get-Date) - $t0).TotalSeconds
}
"################ COMPROBACION"
python "$s\comprobar_pruebas.py" 2>&1
