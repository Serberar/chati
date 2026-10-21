"""Arrancar Chati al iniciar Windows (Opciones): es el acceso directo del
vigilante en la carpeta Inicio, el mismo que crea setup/install_watchdog.ps1.
Sin el, al encender el ordenador no se carga nada (ni Ollama, ni ComfyUI, ni
los modelos en memoria) hasta que se abre Chati, que arranca todo
(setup/start_all.ps1)."""

import os
import subprocess
from pathlib import Path

from paths import CODE_ROOT

INSTALL_SCRIPT = CODE_ROOT / "setup" / "install_watchdog.ps1"


def _shortcut() -> Path:
    return (Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
            / "IA-Personal-Watchdog.lnk")


def enabled() -> bool:
    return _shortcut().exists()


def set_enabled(on: bool) -> None:
    if not on:
        _shortcut().unlink(missing_ok=True)
        return
    subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(INSTALL_SCRIPT)],
                   check=True, capture_output=True, timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
