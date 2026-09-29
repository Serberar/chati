"""Registro de lo que pasa en el orquestador (auditoria 2026-09-29: el
vigilante lo arranca oculto y todo lo que escribia se perdia; un fallo con un
usuario no dejaba rastro).

- Archivo: <carpeta de datos>/data/logs/chati.log, 5 MB x 5 copias (rota solo).
- Nunca se registran mensajes, respuestas ni datos del usuario: solo que
  paso (ruta, error, traza). Los datos del usuario van cifrados; el log no.
"""

import logging
import sys
from logging.handlers import RotatingFileHandler

import paths

LOG_DIR = paths.DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "chati.log"
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup() -> logging.Logger:
    root = logging.getLogger()
    if any(getattr(h, "_chati", False) for h in root.handlers):
        return logging.getLogger("chati")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(_FORMAT))
    handler._chati = True
    root.addHandler(handler)
    if sys.stderr is not None:  # en el .exe sin consola no hay stderr
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(_FORMAT))
        console._chati = True
        root.addHandler(console)
    root.setLevel(logging.INFO)
    # uvicorn: errores si; el registro de cada peticion (access) no aporta y
    # llena el archivo con las consultas de estado cada pocos segundos
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    for noisy in ("httpx", "httpx2", "urllib3", "chromadb", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("chati")
