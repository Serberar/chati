# Instrucciones para el agente de código local

Este es el proyecto de IA personal de Sergio: un orquestador FastAPI (`orchestrator/`)
con agentes de texto/código/imagen/video/voz, memoria SQLite, RAG con ChromaDB,
y generación vía Ollama y ComfyUI. Todo corre en local, sin dependencias de pago.

## Reglas de seguridad (no negociables)

- **Nunca ejecutes acciones irreversibles sin que Sergio las confirme explícitamente**:
  `git push`, `git reset --hard`, borrar archivos o carpetas, sobrescribir sin backup,
  parar procesos en producción, enviar nada a un servicio externo (email, formulario,
  solicitud de empleo, API de terceros). Si una tarea implica algo de esto, párate y
  pregunta antes de actuar.
- Antes de borrar o sobrescribir algo con `git checkout/restore/reset/clean` o `rm`,
  comprueba primero `git status` y guarda lo que encuentres (stash o commit) si no
  estás seguro de que se pueda perder.
- No uses `--no-verify` en commits ni saltes hooks salvo que Sergio lo pida explícitamente.

## Cómo trabajar en este repo

- Lee el archivo antes de editarlo. No asumas cómo funciona algo — verifícalo leyendo
  el código o ejecutando el test correspondiente.
- Después de cualquier cambio de código, ejecuta los tests relevantes
  (`cd orchestrator && python -m pytest tests/ -x -q`, o el archivo de test concreto)
  antes de dar el cambio por terminado. Si no hay test para lo que has tocado, dilo.
- Los tests marcados `live` necesitan Ollama corriendo; los marcados `slow` necesitan
  ComfyUI y pueden tardar varios minutos. No los descartes solo porque tardan.
- No añadas comentarios que expliquen QUÉ hace el código (los nombres ya lo dicen).
  Solo comenta el PORQUÉ cuando no sea obvio (una restricción oculta, un workaround
  de un bug concreto, algo que sorprendería a quien lo lea después).
- No añadas abstracciones, manejo de errores o validaciones para casos que no pueden
  pasar. No diseñes para hipotéticos futuros. Cambios pequeños y acotados salvo que
  se pida explícitamente un refactor mayor.
- Este proyecto corre en Windows con PowerShell como shell principal. Ten cuidado con
  rutas (`C:\AI\...`), y recuerda que los procesos vía venv de Windows lanzan un
  proceso padre + hijo — eso es normal, no un bug.
- Responde siempre en español a Sergio, salvo que el propio código deba estar en inglés
  (identificadores, docstrings de librería, etc).

## Modelos MoE grandes (30B) y las variantes "-cpu"

Ollama calcula mal cuanta VRAM necesita para modelos MoE grandes (ej.
qwen3-coder:30b) en esta GPU de 8GB y falla con "CUDA error: out of memory"
al intentar offloadear capas que no caben. La solucion, verificada: crear una
variante con `num_gpu 0` fijado en el Modelfile (`ollama create <modelo>-cpu
-f Modelfile`, reutiliza los pesos ya descargados, no vuelve a bajar nada).
Gracias a que estos modelos son MoE (solo ~3.3B de parametros activos por
token aunque pesen 30B en disco), correrlos 100% en CPU sigue siendo rapido
(~20 tok/s medido). Usa siempre la variante `-cpu` de estos modelos, nunca la
original, o volvera a fallar por falta de memoria de GPU.

Ya se investigo si un offload PARCIAL a GPU (unas pocas capas, no todas) podia
ir mas rapido que CPU puro, aprovechando algo de los 8GB de VRAM. Resultado
medido: **mas lento** (6.8 tok/s con 28 capas en GPU vs 21 tok/s en CPU puro),
por el coste de mover datos entre RAM y VRAM en cada capa que cambia de
dispositivo. No merece la pena volver a intentarlo salvo que cambie el modelo
o el hardware. `OLLAMA_FLASH_ATTENTION=0` (ya fijado en start_all.ps1 y
watchdog.ps1) sigue siendo necesario de todas formas - sin el, cualquier
intento de tocar la GPU con estos modelos crashea (bug conocido de Ollama con
GPUs Blackwell/RTX 50, ollama/ollama#18276 y #18232).

## Tareas de varios pasos

Si una tarea tiene más de 2-3 pasos, usa la herramienta de lista de tareas (todowrite)
para planificarla antes de empezar y marca cada paso al completarlo. No la uses para
tareas triviales de un solo paso.

## Antes de decir "terminado"

Terminado significa: código leído, cambio hecho, test relevante ejecutado y en verde.
Si no has podido probarlo de verdad (por ejemplo porque requiere GPU y no la tienes
disponible en ese momento), dilo explícitamente en vez de dar por hecho que funciona.
