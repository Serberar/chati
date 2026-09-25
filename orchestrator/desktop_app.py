"""Abre Chati en una ventana nativa (pywebview, motor Edge WebView2 ya
instalado en Windows 10/11) en vez de una pestaña del navegador normal -
para que se sienta como un programa de verdad: sin barra de direcciones,
sin pestañas, icono y titulo propios. Lo lanza el acceso directo del
escritorio (ver setup/launch_and_open.ps1), DESPUES de que start_all.ps1
ya haya arrancado Ollama/ComfyUI/el orquestador - este script solo espera a
que el orquestador responda y abre la ventana, no arranca nada por su
cuenta."""

import sys
import time

import requests
import webview

from paths import CODE_ROOT

URL = "http://127.0.0.1:8899"
ICON_PATH = str(CODE_ROOT / "icono.ico")


def _wait_for_backend(timeout: int = 120) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{URL}/health", timeout=3).status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(2)
    return False


def main() -> None:
    if not _wait_for_backend():
        print("El orquestador no respondio a tiempo en " + URL, file=sys.stderr)
        sys.exit(1)

    webview.create_window("Chati IA", URL, width=1280, height=800, min_size=(720, 480))
    webview.start(icon=ICON_PATH)


if __name__ == "__main__":
    main()
