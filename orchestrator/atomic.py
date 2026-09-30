"""Escritura atomica: primero a un temporal en la misma carpeta y luego se
cambia por el bueno de un golpe (os.replace). Si el PC se apaga a mitad, queda
el archivo anterior entero, no uno a medias (auditoria 2026-09-30: un
meta.json roto hacia fallar cada mensaje de esa conversacion)."""

import os
import uuid
from pathlib import Path


def write_bytes(path: Path, data: bytes) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_text(path: Path, text: str) -> None:
    write_bytes(path, text.encode("utf-8"))
