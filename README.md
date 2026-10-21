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

## Uso

Tras instalarlo, el acceso directo **«Arrancar IA Personal»** levanta Ollama, ComfyUI y el orquestador y abre **http://localhost:8899**. La guía completa para el usuario está en [MANUAL_DE_USO.md](MANUAL_DE_USO.md).

## Autor

Sergio Bernabé
