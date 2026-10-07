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
# solo avisos y errores, con su traza: para ir directo a lo que falla sin
# buscar en todo el registro (Sergio, 2026-10-07: "no me mola buscar un
# error a ciegas"). Se ve en Opciones > Registro de errores.
ERROR_FILE = LOG_DIR / "errores.log"
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
    errors = RotatingFileHandler(ERROR_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
    errors.setLevel(logging.WARNING)
    errors.setFormatter(logging.Formatter(_FORMAT))
    errors._chati = True
    errors.addFilter(_skip_client_disconnects)
    root.addHandler(errors)
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
    logging.getLogger("asyncio").addFilter(_skip_client_disconnects)
    return logging.getLogger("chati")


def _skip_client_disconnects(record: logging.LogRecord) -> bool:
    """En Windows, asyncio anota como ERROR cada vez que el navegador corta una
    conexion (cerrar la pestaña, recargar): WinError 10054. No es un fallo."""
    exc = record.exc_info[1] if record.exc_info else None
    return not isinstance(exc, (ConnectionResetError, ConnectionAbortedError))
