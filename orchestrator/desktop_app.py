"""Abre Chati en una ventana nativa (pywebview, motor Edge WebView2 ya
instalado en Windows 10/11) en vez de una pestaña del navegador normal -
para que se sienta como un programa de verdad: sin barra de direcciones,
sin pestañas, icono y titulo propios.

Es lo que ejecuta el acceso directo del escritorio, con pythonw.exe (sin
consola): la ventana aparece al momento con una pantalla de carga, y
mientras tanto arranca Ollama/ComfyUI/el orquestador (setup/start_all.ps1)
en segundo plano, tambien sin ventana. Antes el acceso directo lanzaba
PowerShell y se veia una terminal negra durante todo el arranque."""

import logging
import subprocess
import threading
import time
import webbrowser

import requests
import webview

from paths import CODE_ROOT, DATA_DIR

URL = "http://127.0.0.1:8899"
ICON_PATH = str(CODE_ROOT / "icono.ico")
START_SCRIPT = CODE_ROOT / "setup" / "start_all.ps1"
LOG_PATH = DATA_DIR / "arranque.log"
BACKEND_TIMEOUT = 180  # primer arranque tras reiniciar: ComfyUI solo ya tarda ~30s

_PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
  body {{ margin: 0; height: 100vh; display: flex; align-items: center; justify-content: center;
    background: #14161a; color: #e8e9ec; font-family: "Segoe UI", system-ui, sans-serif; }}
  .box {{ text-align: center; max-width: 520px; padding: 24px; }}
  h1 {{ font-size: 22px; font-weight: 600; margin: 0 0 10px; }}
  p {{ color: #9099a8; font-size: 14px; line-height: 1.5; margin: 0; }}
  .spin {{ width: 34px; height: 34px; margin: 0 auto 22px; border-radius: 50%;
    border: 3px solid #333844; border-top-color: #5b8cff; animation: s 0.9s linear infinite; }}
  @keyframes s {{ to {{ transform: rotate(360deg); }} }}
</style></head><body><div class="box">{body}</div></body></html>"""

LOADING_HTML = _PAGE.format(body=(
    '<div class="spin"></div><h1>Arrancando Chati IA</h1>'
    "<p>Preparando los modelos locales. La primera vez tras encender el equipo "
    "puede tardar hasta un minuto.</p>"))


def _error_html(msg: str) -> str:
    return _PAGE.format(body=(
        f"<h1>No se pudo arrancar Chati IA</h1><p>{msg}</p>"
        f"<p style='margin-top:14px'>Detalles en: {LOG_PATH}</p>"))


def _backend_up() -> bool:
    try:
        return requests.get(f"{URL}/health", timeout=3).status_code == 200
    except requests.RequestException:
        return False


def _start_services() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "w", encoding="utf-8", errors="replace") as log:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-WindowStyle", "Hidden", "-File", str(START_SCRIPT)],
            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


class _WebViewFailure(logging.Handler):
    """pywebview solo lo anota en su registro cuando el motor de Edge (WebView2)
    no arranca, y la ventana se queda en negro para siempre (visto en Windows
    Sandbox el 2026-09-30: "Couldn't find a compatible Webview2 Runtime"). Si
    pasa, Chati se abre en el navegador normal."""

    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.failed = threading.Event()

    def emit(self, record):
        if "WebView2 initialization failed" in record.getMessage():
            self.failed.set()


_webview_failure = _WebViewFailure()
logging.getLogger("pywebview").addHandler(_webview_failure)


def _fallback_to_browser(window) -> None:
    if _webview_failure.failed.wait(timeout=BACKEND_TIMEOUT + 30):
        webbrowser.open(URL)
        window.destroy()


def _boot(window) -> None:
    threading.Thread(target=_fallback_to_browser, args=(window,), daemon=True).start()
    if not _backend_up():
        try:
            _start_services()
        except OSError as exc:
            window.load_html(_error_html(f"Fallo al lanzar los servicios: {exc}"))
            return
        deadline = time.time() + BACKEND_TIMEOUT
        while not _backend_up():
            if time.time() > deadline:
                window.load_html(_error_html("El orquestador no respondio a tiempo."))
                return
            time.sleep(2)
    window.load_url(URL)


def _set_taskbar_identity() -> None:
    # Sin esto Windows agrupa la ventana con pythonw.exe y la barra de tareas
    # muestra el icono de Python en vez del de Chati (el de la ventana si es
    # el nuestro, pero la barra agrupa por AppUserModelID, no por ventana).
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SergioBernabe.ChatiIA")
    except (AttributeError, OSError):
        pass


class DesktopApi:
    """Lo que la pagina puede pedirle a la ventana (window.pywebview.api)."""

    def open_in_browser(self) -> None:
        # un enlace normal se abriria en otra ventana de la app, no en el navegador
        webbrowser.open(URL)


def main() -> None:
    _set_taskbar_identity()
    # sin esto el boton "Descargar" de las imagenes no hacia nada en la app (en
    # el navegador si); y Chati nunca abre archivos locales por file://
    webview.settings["ALLOW_DOWNLOADS"] = True
    webview.settings["ALLOW_FILE_URLS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    # text_select: pywebview desactiva la seleccion de texto por defecto - sin
    # esto no se podia seleccionar ni copiar nada de la conversacion
    window = webview.create_window("Chati IA", html=LOADING_HTML, width=1280, height=800,
                                   min_size=(720, 480), background_color="#14161a", text_select=True,
                                   js_api=DesktopApi())
    webview.start(lambda: threading.Thread(target=_boot, args=(window,), daemon=True).start(),
                  icon=ICON_PATH)


if __name__ == "__main__":
    main()
