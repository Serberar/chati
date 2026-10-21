# Chati · IA personal en tu propio ordenador

Asistente de inteligencia artificial que **funciona entero en local**, sin depender de servicios en la nube: chat, generación de imágenes y vídeo, voz y un agente que edita código. Los modelos corren en tu PC con Ollama y ComfyUI, y se usa desde el navegador o desde el móvil.

## Funcionalidades

- **Chat** que detecta si pides código, una imagen o un vídeo y usa el agente adecuado; consulta la fecha real y su memoria cuando hace falta.
- **Imagen y vídeo**, también con una cara de referencia (con permiso de la persona), y edición: escalar x4 o repintar una zona.
- **Voz:** hablas y responde con voz (Whisper + Piper).
- **Agente de código** que trabaja sobre tus proyectos, con cambios revisables y la posibilidad de deshacerlos.
- **Cuentas con conversaciones cifradas**, modo invitado y panel de administración.
- **Varios ordenadores** y uso desde el móvil como app instalable, con avisos push (ver [Serberar.github.io](https://github.com/Serberar/Serberar.github.io)).
- Actualización de modelos, copias de seguridad y *watchdog* que reinicia lo que se cuelga.

## Tecnologías

- **Backend:** Python, FastAPI, Ollama (modelos Qwen), ComfyUI, faster-whisper, Piper, ChromaDB (memoria y documentos) y Playwright.
- **Frontend:** HTML, CSS y JavaScript servidos por el propio orquestador (PWA).
- **Instalación en Windows:** scripts de PowerShell e instalador de Inno Setup (`setup/`).

## Estructura

```
orchestrator/   # Servidor FastAPI, agentes (texto, imagen, vídeo, voz) y la web
setup/          # Instalador, arranque, watchdog, backups y restauración
MANUAL_DE_USO.md
```

## Instalar

Descarga **[ChatiIA-Instalador.exe](https://github.com/Serberar/chati/releases/latest/download/ChatiIA-Instalador.exe)** (la última versión, en [Releases](https://github.com/Serberar/chati/releases)) y ábrelo. Pide permisos de administrador, crea la cuenta de administrador y deja elegir qué modelos descargar y si Chati arranca al iniciar Windows. Instala lo que falte (Python, Ollama, Git, Node, ComfyUI…), así que la primera vez tarda.

## Uso

Tras instalarlo, el acceso directo **«Chati IA»** del escritorio levanta Ollama, ComfyUI y el orquestador y abre la ventana de Chati (también en **http://localhost:8899**). La guía completa para el usuario está en [MANUAL_DE_USO.md](MANUAL_DE_USO.md).

En **Opciones**:

- **Buscar actualizaciones de Chati**: cuando hay una versión nueva en Releases sale un aviso arriba; con **Actualizar** se descarga, se comprueba su huella SHA-256 y se instala (Windows pide permiso), y Chati se vuelve a abrir sola. Los datos y los modelos no se tocan.
- **Arrancar con Windows**: si está en «no», al encender el ordenador no se carga nada en memoria hasta que abres Chati.

## Publicar una versión

1. Sube `VERSION` en `orchestrator/version.py`.
2. Compila: `powershell -ExecutionPolicy Bypass -File setup\build_release.ps1` (necesita `orchestrator\venv`, creado con `python -m venv orchestrator\venv` y `pip install -r orchestrator\requirements.lock.txt pyinstaller`, e [Inno Setup 6](https://jrsoftware.org/isinfo.php)).
3. Crea una release con la etiqueta `vX.Y.Z` y adjunta `setup\Output\ChatiIA-Instalador.exe` (con ese nombre).

Las Chati instaladas la verán como mucho en 6 horas, o al pulsar **Buscar actualizaciones**.

## Autor

Sergio Bernabé
