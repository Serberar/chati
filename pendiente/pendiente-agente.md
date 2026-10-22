# Agente de Chati: estado y roadmap

Objetivo: que el agente de Chati (modos Agente y Código) haga casi de todo lo que le pide Sergio en su ordenador, con fiabilidad de producto comercial, en local y sin pagar. Este documento recoge lo medido hasta el 2026-10-22 y el plan para llegar a eso. Se actualiza tras cada ronda de pruebas.

## 1. Resultados medidos

### Modo Código: proyecto Python con tests (banco `orchestrator/agent_bench.py`, 13/10)

9 tareas sobre un proyecto pequeño: arreglar un bug, función nueva con test, renombrar en varios archivos, leer un Excel, recomendar o comprobar librerías, explicar código y recordar una preferencia. Modelo qwen3-coder 30B (`-mixto`).

| Ronda | Acierto | Tiempo medio |
| --- | --- | --- |
| Completa, 2 vueltas | 14/18 (78 %) | 5,8 min |
| Los 2 fallos repetidos, tras corregirlos | 4/4 | 1,1 min |
| Renombrar y función nueva, repetidas | 4/4 | 3-12 min |

Las tareas de programación (bug, función, renombrar, Excel) salieron bien en todas las vueltas (10/10, siempre con los tests pasando). Los fallos eran de comportamiento: decir que existe una librería inventada y no guardar en memoria lo que se le pide.

### Modo Agente: arreglar programas rotos (22/10)

Batería en `Escritorio/pruebas`, con 4 casos típicos y un comprobador objetivo que abre la ventana, mira el total, etc.:

- **notas**: un menú de consola (`input()`) compilado como `.exe` sin consola; no hace nada al abrirlo. Es el caso real del HostManager.
- **facturas**: el CSV trae importes con coma decimal y punto de miles; el programa falla con `ValueError`.
- **contador**: ventana tkinter con `command=sumar()` en vez de `command=sumar`; el botón no hace nada.
- **informe**: lee sus datos de una ruta fija de otro ordenador.

| Ronda | Arreglados | Qué falló |
| --- | --- | --- |
| Antes de los cambios (HostManager real) | 0 | Respondía con listas de "posibles soluciones", no miraba el código, lanzaba el .exe con argumentos inventados |
| 1ª: prompt "arréglalo tú" | 1/4 | Se bloqueaba 2 min probando ventanas con `python`; **sobrescribió el CSV del usuario con datos inventados**; cortaba `pyinstaller` con `Select-Object -First` |
| 2ª: + herramienta `chati_probar_programa` | 2/4 | `glob` con ruta absoluta no encontraba el CSV y lo volvió a sobrescribir |
| 3ª: + `glob` desactivado en modo Agente | 3/4 | notas a medias: hizo la ventana en `notas.py` (funciona), pero compiló el .exe nuevo en `pruebas/dist/` y no lo puso en el sitio del viejo |

En la 3ª ronda cada caso tardó entre 1,2 y 4,3 min (notas 4,3 · facturas 2,7 · contador 1,3 · informe 1,2). Facturas salió bien a la primera: entendió la coma decimal y el punto de miles y no tocó los datos.

### Velocidad (22/10, misma petición de ~2.800 tokens)

| Variante qwen3-coder 30B | Lee | Escribe |
| --- | --- | --- |
| `-mixto` (12 capas en GPU) | 246 tok/s | 14,0 tok/s |
| `-cpu` | 50 tok/s | 12,2 tok/s |

`-mixto` es la mejor en las dos cosas. Si otra cosa usa la CPU a la vez (tests, compilaciones), la escritura baja a ~4 tok/s. Un archivo de 2.400 tokens tarda ~3 min en escribirse en condiciones normales.

## 2. Qué hace bien

- Encuentra la causa de fallos acotados (un archivo, error reproducible) leyendo el código y ejecutándolo.
- Con tests en el proyecto, comprueba y repite hasta que pasan.
- Usa bien las herramientas cuando son simples y dicen BIEN o MAL (`chati_probar_programa`).
- Explica en dos frases qué estaba mal y qué ha cambiado.
- Pide permiso antes de tocar archivos, y Chati deja deshacer en modo Código.

## 3. Qué hace mal

- **Sigue mal las instrucciones largas**: con muchas reglas se salta justo las importantes. Hay que convertir las reglas críticas en herramientas o en bloqueos, no en texto.
- **Inventa para salir del paso**: si algo no le cuadra, crea datos de ejemplo y lo da por arreglado. Es el fallo más grave porque destruye datos.
- **Se cree herramientas rotas**: si una herramienta le devuelve vacío, lo toma como verdad (el `glob` con rutas absolutas).
- **No sabe esperar a programas que se quedan abiertos**: ventanas o servidores lanzados con `bash` lo bloquean hasta el límite de 2 min.
- **PowerShell flojo**: usa `&&`, `cd x && y` y `Select-Object -First`, que corta comandos.
- **El modelo rápido (`qwen3:8b`) no sabe depurar**: da consejos genéricos. Ahora las peticiones de arreglar algo van siempre al potente.
- **Lento en tareas largas**: cada paso del potente tarda de 10 s a varios minutos. Una tarea de 15 pasos son 10-20 min.
- **Sin visión de lo que ve el usuario**: no puede mirar una ventana ni pulsar un botón para comprobar que hace lo que debe.
- **Repos grandes**: no hay mapa del proyecto. Lee archivos enteros, el contexto crece y se pierde.

## 4. Roadmap

Ordenado por impacto. Cada fase termina con una ronda del banco de pruebas y sus números en este documento. Ningún cambio se da por bueno si baja el acierto del banco.

### La visión en una frase

Un agente que **no adivina**: sabe cómo es tu ordenador, acuerda contigo qué significa "terminado", lo demuestra con pruebas que ejecuta él mismo, recuerda cada problema que ya resolvió y mejora solo cada noche. Lo que le falta al modelo en inteligencia lo compensa con cálculo, memoria y verificación.

### Arquitectura a la que vamos

Hoy el agente es OpenCode + un modelo + reglas en el prompt. Funciona, pero las cosas importantes (cuándo parar, cómo comprobar, qué no tocar) dependen de que el modelo obedezca, y no obedece bien. El destino es un **motor de agente propio de Chati**, con OpenCode como respaldo mientras tanto:

```text
 Petición ──► Contrato de la tarea ──► Plan ──► Pasos ──► Comprobaciones ──► Informe con pruebas
               (qué es "hecho")         │        │  ▲            │
                                        │        ▼  │            ▼
                                 Mapa del PC   Herramientas   Copias y marcha atrás
                                 y del proyecto (con gramática)  (volver a cualquier paso)
                                        ▲                         │
                                        └── Memoria de experiencia ◄── Lecciones y habilidades
```

- **Máquina de estados, no conversación libre**: entender → contrato → plan → ejecutar → comprobar → informar. Cada estado tiene sus herramientas y su condición de salida, y el modelo no puede saltarse "comprobar".
- **Cada paso con el modelo justo**: el planificador y el revisor, el grande; leer, listar y ejecutar, el rápido en GPU; mirar una captura, el de visión.
- **Todo queda registrado**: cada paso (petición, herramienta, salida, archivos tocados) se guarda y se puede rebobinar.

### Fase 12: las ideas que separan un buen agente de uno genial

- [ ] **Contrato de la tarea**: antes de tocar nada, el agente escribe en 2-4 líneas qué comprobará para dar la tarea por terminada ("se abre una ventana; al pulsar Sumar el número sube; el .exe nuevo está donde el viejo"). Si la tarea es ambigua, te lo enseña; si no, lo usa como lista de verificación. Termina solo cuando todas las comprobaciones pasan, y el informe final es ese contrato con cada punto demostrado.
- [ ] **Mapa vivo del ordenador**: un inventario que Chati mantiene al día con las versiones de Python y dónde están, los programas instalados, el PATH, las carpetas de proyectos y cómo se arranca y se prueba cada uno, las rutas reales del escritorio y OneDrive… El agente nunca más pregunta `where python` ni se inventa una ruta.
- [ ] **Biblioteca de habilidades que se escribe sola** (como Voyager): cuando resuelve algo nuevo (por ejemplo, "empaquetar una app tkinter con icono y permisos de administrador"), lo guarda como procedimiento probado, con código y comprobación, y la próxima vez lo reutiliza en vez de improvisar. Las habilidades se validan en el banco igual que el agente.
- [ ] **Herramientas con gramática forzada**: llamar a Ollama/llama.cpp con una gramática JSON por herramienta, de forma que el modelo solo pueda generar llamadas válidas. Se acaban los parámetros null, el JSON roto y las herramientas inventadas, y el modelo pequeño gana mucha fiabilidad.
- [ ] **Motor de inferencia a medida**: valorar llama.cpp server además de Ollama por tres cosas que aquí valen oro:
  - Decodificación especulativa, con un modelo pequeño que adelanta tokens y el grande que solo los confirma (2-3 veces más rápido escribiendo).
  - Caché del contexto guardada en disco por proyecto, para retomar una tarea al día siguiente sin volver a leer nada.
  - Gramáticas.

  Se decide midiendo.
- [ ] **Rebobinar y ramificar**: en la vista de la tarea, una línea de tiempo con cada paso. Pulsas un punto, "prueba otra cosa desde aquí", y sigue desde ese estado exacto (archivos incluidos) por otro camino.
- [ ] **Preguntar poco y bien**: el agente calcula cuánto le ayudaría saber algo frente a lo que cuesta preguntarlo, y solo pregunta lo que cambia la solución. Cuando pregunta, te da opciones concretas.
- [ ] **Previsión antes de empezar**: "unos 6 min, 4 pasos, confianza alta" o "esto es difícil para mí: ¿lo intento con varias soluciones en paralelo (20 min) o lo preparo para consultarlo con Claude?". Sale de su historial en tareas parecidas.
- [ ] **Modo sombra para cada versión nueva**: antes de cambiar el agente de verdad, la versión nueva repite en segundo plano tus últimas tareas reales en copias aisladas y se compara con lo que se hizo. Solo se activa si es mejor.
- [ ] **Explica y enseña**: un modo "enséñame" en el que cuenta el porqué de cada arreglo a tu nivel, para que también aprendas tú.

### Fase 0: medir siempre (base de todo lo demás)

- [ ] Pasar la batería de `Escritorio/pruebas` al repo (`orchestrator/agent_bench/`): casos, comprobadores objetivos y un único comando para lanzarla.
- [ ] Ampliar a **60+ casos en 8 categorías** (ver Fase 5), con tres niveles cada una: fácil, medio y difícil.
- [ ] Banco unificado para los modos Agente y Código, con informe HTML: acierto por categoría, tiempo mediano, pasos, tokens y "datos destruidos" (tiene que ser siempre 0).
- [ ] Ronda automática de madrugada (el vigilante ya hace la copia diaria a esa hora) y aviso al móvil si baja el acierto.
- [ ] Casos sacados de tareas reales: cuando una tarea de Sergio falle, convertirla en caso del banco.
- [ ] **Compararse con los comerciales con sus propias reglas**: ejecutar en local bancos públicos con resultados publicados de Claude, GPT o Gemini (Aider Polyglot, un subconjunto de SWE-bench Verified, Terminal-Bench). Así "casi comercial" deja de ser una impresión y pasa a ser un número.

### Fase 1: no romper nada nunca

- [ ] **Guardia de datos en Chati, no en el prompt**: antes de conceder un permiso de `write` o `edit` sobre un archivo de datos que ya existe (csv, xlsx, json, txt, db…), Chati lo detecta y lo rechaza con un mensaje al modelo ("es un dato del usuario: arregla el código"), salvo que la tarea pida cambiar ese archivo.
- [ ] **Copia y "Deshacer" también en modo Agente**: hoy solo existen en modo Código (`checkpoints.py`). Copia de lo que vaya a tocar antes del primer cambio.
- [ ] **Verificación obligatoria al terminar**: igual que el vigilante del modo Código ejecuta los tests, en modo Agente Chati comprueba lo que se tocó (abre el programa con `probar_programa`, ejecuta el script). Si falla, se lo devuelve al agente con el error, como mucho 2 veces, y si no, te lo dice.
- [ ] Detector de bucles: el mismo comando 3 veces seguidas o el mismo error 3 veces cortan el intento y obligan a cambiar de enfoque.
- [ ] Desactivar o arreglar cada herramienta que devuelva resultados falsos (hecho con `glob`; revisar `list`, `grep`, `read` con rutas de Windows).

### Fase 2: herramientas que no se puedan usar mal

- [x] `chati_probar_programa`: abre .py o .exe, dice BIEN/MAL con el error y lo cierra.
- [ ] `chati_ejecutar`: comando con tiempo límite, salida recortada por el final y sin colgarse si pide teclado. Sustituye a `bash` para lo habitual y corrige solo `&&` y las rutas con espacios.
- [ ] **(Urgente, falló en la 3ª ronda)** `chati_compilar_exe`: PyInstaller con las opciones correctas según el programa (ventana o consola, `--uac-admin` si hace falta), compila en una carpeta temporal, sustituye el .exe viejo y prueba el nuevo con `probar_programa`. El agente compilaba en su carpeta de trabajo y no movía el resultado.
- [ ] `chati_ver_ventana`: captura de la ventana del programa y descripción con el modelo de visión ("se ve un contador en 0 y un botón Sumar 1").
- [ ] `chati_usar_ventana`: pulsar botones y escribir en programas de escritorio (pywinauto), para comprobar que el contador sube al pulsar. Es el equivalente de `chati_usar_web` para programas de Windows.
- [ ] `chati_entorno_python`: crea un venv por proyecto e instala lo que haga falta ahí, nunca en el Python del sistema.
- [ ] `chati_editar_bloque`: reemplazar una función o un bloque por nombre en vez de por texto exacto, que es donde fallan las ediciones del modelo local.
- [ ] Plantillas probadas para lo frecuente: app tkinter, script con menú, web Flask, API FastAPI, script programado de Windows, lectura de Excel/CSV. El agente parte de algo que ya funciona en vez de escribir 300 líneas de cero.

### Fase 3: un cerebro mejor con el mismo hardware

- [ ] **Planificador y ejecutor**: el modelo grande hace un plan corto (pasos y cómo comprobar cada uno) y los pasos mecánicos (leer, listar, ejecutar) los hace el rápido en GPU. Cada paso cuesta segundos en vez de minutos.
- [ ] **Revisor**: antes de decir "hecho", una segunda pasada que compara lo pedido con la evidencia de las comprobaciones y rechaza los "arreglos" que no se han probado.
- [ ] **Probar modelos nuevos con el banco**: qwen3-coder 30B (actual), gpt-oss 20B (medido 8/9 el 13/10), Devstral, GLM-4.5-Air y lo que salga. Se elige por acierto/tiempo del banco, no por fama.
- [ ] **Velocidad**:
  - [ ] Que nada de Chati use la CPU mientras el agente escribe: pausar la indexación de memoria y diferir la copia diaria.
  - [ ] Prompt del sistema y definiciones de herramientas más cortas (hoy ~5.000 tokens fijos por petición).
  - [ ] Caché de contexto entre pasos (ya funciona: confirmarlo en cada cambio).
  - [ ] Decodificación especulativa si Ollama la soporta.
- [ ] **Escribir menos**: editar solo las líneas necesarias en vez de reescribir archivos; un archivo de 2.400 tokens son 3 min.
- [ ] **Si el hardware cambia**: con 16-24 GB de VRAM, el modelo de 30B entero en GPU escribiría a 60-100 tok/s. Medir antes de decidir.
- [ ] **Escape a un modelo comercial** (ya existe en modo Código como "CONSULTA PARA CHATGPT O CLAUDE"): llevarlo también al modo Agente cuando falle dos veces.

### Fase 4: entender proyectos grandes

- [ ] Mapa del repo (estructura, clases, funciones y quién llama a quién) con tree-sitter, como hace aider: el agente sabe dónde mirar sin leerlo todo.
- [ ] Búsqueda semántica del código con los embeddings que Chati ya tiene (`nomic-embed-text`).
- [ ] Ficha del proyecto (`AGENTS.md`, ya existe en modo Código): cómo se arranca, cómo se prueba, decisiones. Generarla y mantenerla también desde el modo Agente.
- [ ] **Lecciones aprendidas**: cuando un arreglo funciona tras un error, guardar el patrón ("PyInstaller --windowed + input() = se cierra") en una memoria que se consulta al empezar cada tarea.
- [ ] Compactar el historial de forma inteligente en tareas largas: resumir lo hecho y conservar los errores y las decisiones.

### Fase 5: saber hacer de todo (categorías del banco)

| Categoría | Ejemplos de casos | Meta |
| --- | --- | --- |
| Scripts y archivos | ordenar, renombrar, limpiar duplicados, convertir formatos | 95 % |
| Programas de escritorio | tkinter/PyQt, .exe, iconos, permisos de administrador | 90 % |
| Depurar | errores de Python, dependencias, rutas, codificación, Unicode | 90 % |
| Datos | Excel, CSV, pandas, gráficos, informes en PDF | 85 % |
| Web | Flask/FastAPI, HTML/JS, formularios, probar con navegador | 80 % |
| Automatizar Windows | tareas programadas, registro de eventos, servicios, PowerShell | 80 % |
| Proyectos existentes | añadir funciones, refactorizar, tests, git y GitHub | 75 % |
| Sistemas | Docker, bases de datos, redes, archivo hosts | 70 % |

### Fase 6: experiencia de producto

- [x] Ver en todo momento qué está haciendo (leyendo, escribiendo, herramienta, paso de ComfyUI).
- [x] Un solo campo de chat: los mensajes siguen la tarea si van de lo mismo.
- [ ] Plan visible y editable antes de empezar en tareas grandes (sí / cambia esto / no).
- [ ] Informe final con evidencias: qué ha probado y con qué resultado, con capturas en programas de ventana.
- [ ] Permisos por lotes: "todo lo de esta carpeta, sí" para no aprobar 15 veces lo mismo.
- [ ] Modo autónomo con límites claros: carpeta de trabajo, sin borrar y sin red salvo para instalar.
- [ ] Reanudar tareas tras reiniciar el PC o Chati.

### Fase 7: seguridad

- [ ] Cada tarea trabaja en una copia de la carpeta y solo al final se aplican los cambios (con diff) a la de verdad.
- [ ] Lista cerrada de acciones que siempre preguntan: borrar, sistema, red, registro, archivo hosts, credenciales.
- [ ] Nunca leer ni mostrar contraseñas o tokens. Si aparecen en un archivo, se ocultan antes de mandarlos al modelo.

### Fase 8: pensar más para acertar más (calcular en vez de adivinar)

- [ ] **Reproducir antes de arreglar**: lo primero de todo fallo es un test o un script que lo demuestra (falla). El arreglo solo vale si ese test pasa y los demás siguen pasando, al estilo TDD automático. Ya no depende de que el modelo "crea" que está bien.
- [ ] **Varias soluciones en paralelo**: para un fallo difícil, generar 3-5 arreglos distintos en copias aisladas de la carpeta, pasarles a todos las comprobaciones y quedarse con el que pasa (o con el más pequeño que pasa). Es lo que hacen los mejores agentes en SWE-bench, y aquí el "juez" es objetivo: los tests y `probar_programa`.
- [ ] **Búsqueda con marcha atrás**: si un camino lleva 3 pasos sin avanzar, volver al último punto bueno (con las copias de la Fase 1) y probar otra rama, en vez de seguir empeorándolo.
- [ ] **Autocrítica con pruebas**: después de cada cambio, una pasada que busca casos que rompan el arreglo (entradas vacías, rutas con espacios, números con coma, Unicode) y los añade como tests.
- [ ] **Presupuesto de pensamiento según la tarea**: lo fácil, rápido y en una pasada; lo difícil, plan, varias soluciones y revisión. Lo decide un clasificador medido con el banco.

### Fase 9: un agente que aprende solo

- [ ] **Diario de trayectorias**: guardar cada tarea entera (petición, pasos, herramientas, errores, resultado comprobado). Es la materia prima de todo lo demás.
- [ ] **Lecciones automáticas**: cuando algo falla y luego sale bien, extraer la lección ("`--windowed` + `input()` = se cierra al abrir") y recuperarla en tareas parecidas. Es memoria de experiencia, no de datos.
- [ ] **El banco crece solo**: cada fallo real se convierte, con su comprobador, en un caso nuevo del banco, y Sergio solo lo confirma.
- [ ] **Se mejora a sí mismo con red de seguridad**: el agente propone cambios a su propio prompt y a sus herramientas, el banco los valida de madrugada y solo entran los que suben el acierto sin bajar ninguna categoría. Se informa por la mañana de qué ha cambiado y cuánto mejora.
- [ ] **Afinar un modelo propio**: con las trayectorias buenas (las suyas y las de sesiones con Claude), entrenar con QLoRA un modelo de 7-8B que quepa en la GPU, especializado en "los problemas de Sergio en Windows". Pasaría de 14 a 60-80 tokens/s y podría hacer de ejecutor rápido de la Fase 3. Se mide contra el actual en el banco antes de usarlo.

### Fase 10: manos y ojos en todo el ordenador

- [ ] **Usar cualquier programa de Windows**: captura de pantalla, modelo de visión local y ratón y teclado (al estilo "computer use"), con permiso por programa. Puede probar de verdad lo que hace: abrir la app, pulsar el botón, leer el resultado.
- [ ] **Navegador completo**: ya existe `chati_usar_web`. Llevarlo a flujos largos (iniciar sesión con permiso, rellenar, descargar) y a probar las webs que construye como lo haría un humano.
- [ ] **Documentación sin conexión**: índice local de la documentación oficial de Python, PowerShell, tkinter, pandas, Flask, FastAPI, PyInstaller… para no inventar funciones y citar de dónde saca cada cosa.
- [ ] **Equipo de agentes**: planificador, programador, probador, revisor e investigador trabajando a la vez en subtareas, con un coordinador que junta los resultados. Las tareas grandes pasan de horas en serie a minutos en paralelo.
- [ ] **Usar los dos ordenadores**: con Tailscale (ya conectado), repartir el trabajo entre el PC de casa y el del trabajo (por ejemplo, uno genera soluciones mientras el otro las prueba) y usar la GPU que esté libre.

### Fase 11: proactivo, como un compañero

- [ ] **Vigila tus proyectos**: errores nuevos en registros, tests que dejan de pasar, dependencias con fallos de seguridad o versiones nuevas. Te propone el arreglo ya hecho y probado, listo para aceptar o deshacer.
- [ ] **Tareas de fondo con informe**: "esta noche revisa el repo X y déjame las mejoras" y por la mañana un informe con cambios en ramas separadas, cada uno con sus pruebas.
- [ ] **Por voz y desde el móvil**: pedirle cosas hablando o desde el iPhone. Te avisa cuando termina o cuando necesita una decisión (los avisos ya existen).
- [ ] **Proyectos completos de cero**: de "quiero una app que haga X" a un proyecto con estructura, tests, README, instalador y primera versión publicada en GitHub, con revisión en cada fase.

### Hitos que se pueden enseñar

Cada hito es una demo que se puede repetir delante de cualquiera, no una sensación.

| Hito | Demo | Necesita |
| --- | --- | --- |
| H1 | Cualquier "mi programa no funciona" de la batería, resuelto y demostrado sin intervenir | Fases 1-2 y compilar_exe |
| H2 | De "quiero una app que haga X" a un .exe probado y con instalador en < 30 min | Fases 2-3 y plantillas |
| H3 | Arreglar un issue real de un repo de GitHub de terceros y pasar su CI | Fases 4 y 8 |
| H4 | Una noche sola mejorando su propio banco, con informe por la mañana y cero regresiones | Fase 9 y modo sombra |
| H5 | Probar una app de escritorio como una persona (abrir, pulsar, leer) y arreglar lo que no hace | Fase 10 |
| H6 | Un número en Aider Polyglot o SWE-bench Verified que se pueda comparar con los comerciales | Fase 0 |

### Orden de ataque (lo siguiente, en este orden)

1. `chati_compilar_exe` y otra ronda de la batería (objetivo 4/4).
2. Publicar la versión con todo lo de hoy.
3. Batería al repo y ampliarla a 20 casos (Fase 0).
4. Guardia de datos en Chati y copias con "Deshacer" en modo Agente (Fase 1).
5. Verificación obligatoria al terminar (Fase 1) usando el contrato de la tarea (Fase 12).
6. Mapa vivo del ordenador (Fase 12).
7. Planificador + ejecutor rápido (Fase 3): el mayor salto de velocidad.
8. Lecciones y biblioteca de habilidades (Fases 9 y 12).
9. Varias soluciones en paralelo con juez objetivo (Fase 8).
10. Gramáticas y motor de inferencia medido (Fase 12).

## 5. Metas de "casi comercial"

- Acierto del banco: ≥ 90 % en fácil, ≥ 75 % en medio y ≥ 50 % en difícil.
- Datos del usuario destruidos o inventados: **0**, siempre.
- Tiempo mediano: < 3 min en fácil y < 10 min en medio.
- Nunca "hecho" sin una comprobación que lo demuestre.
- En bancos públicos (Aider Polyglot, SWE-bench Verified): primero a la altura de los mejores modelos abiertos y después a menos de 15 puntos de los comerciales, compensando con más cálculo (Fase 8) lo que le falta de modelo.
- **Más allá de lo comercial en lo suyo**: en los problemas de tu ordenador (tus programas, tus rutas, tu Windows), mejor que un agente comercial genérico. Conoce tu equipo, tiene memoria de lo que ya ha arreglado y puede probarlo todo de verdad en la máquina.

## 6. Pendiente inmediato

### Donde lo dejamos (22/10, 18:00)

- **Hecho, en el repo y sin publicar todavía**: `chati_probar_programa`, `chati_compilar_exe` (probado a mano: crea el .exe de notas en 14 s, sustituye el viejo y dice BIEN), prompt nuevo del modo Agente, `glob` desactivado, "arreglar" → modelo potente y tests (694 en verde).
- **4ª ronda de la batería**: lanzada con `chati_compilar_exe` y **parada a mano** al empezar (Sergio se iba). No hay resultado.
- **Mañana, en este orden**:
  1. Repetir la 4ª ronda: `powershell -ExecutionPolicy Bypass -File pendiente\bateria\bateria.ps1` (antes, `python pendiente\bateria\crear_pruebas.py` para dejar los casos rotos, y que OpenCode esté arrancado). Objetivo: 4/4.
  2. Si sale 4/4: subir la versión, compilar con `setup\build_release.ps1` y publicar la release.
  3. Seguir con el "Orden de ataque" desde el punto 3.

### Ojo

- La configuración de OpenCode de tu perfil (`~/.config/opencode/opencode.json`) apunta ahora al código del repo (`agent_mcp.py` del repo y su venv) y lleva activadas `chati_probar_programa` y `chati_compilar_exe`. La copia anterior está en `opencode.json.antes-pruebas`. Al publicar e instalar la versión nueva, `install.ps1` la regenera apuntando a la Chati instalada.
- Los scripts de la batería viven en `pendiente/bateria/` (crear casos, comprobar, lanzar el agente y la batería entera). Los casos se crean en `Escritorio/pruebas`.

### Lista

- [ ] Publicar en Chati lo de hoy: `chati_probar_programa`, `chati_compilar_exe`, prompt nuevo del modo Agente, `glob` desactivado, "arreglar" → modelo potente.
- [ ] Repetir la 4ª ronda con `chati_compilar_exe`: objetivo 4/4.
- [ ] Tras actualizar, la configuración de OpenCode del perfil vuelve a apuntar a la Chati instalada. Mientras tanto apunta al repo; la copia anterior está en `~/.config/opencode/opencode.json.antes-pruebas`.
- [ ] Pasar la batería y su comprobador al repo (Fase 0).
- [ ] Borrar `Escritorio/pruebas` cuando la batería viva en el repo.
