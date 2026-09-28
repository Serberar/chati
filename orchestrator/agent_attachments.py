"""Archivos adjuntos a una tarea del agente (roadmap n.º 4).

Se copian a una carpeta visible del usuario (Documentos/Chati/adjuntos/<fecha>)
y al agente se le dice donde estan: trabaja sobre esas copias con sus
herramientas, los originales no se tocan. Las imagenes se describen antes con
el modelo de vision, porque el del agente (qwen3:8b) no ve imagenes - asi
puede, p.ej., renombrar fotos segun lo que sale en ellas."""

import base64
from datetime import datetime
from pathlib import Path
from typing import Callable

from rag import _sanitize_filename

# Carpeta de trabajo del agente (ver opencode_client.WORKDIR): los adjuntos
# van dentro para que lo que el agente haga con ellos se pueda deshacer.
AGENT_DIR = Path.home() / "Documents" / "Chati"
ATTACH_ROOT = AGENT_DIR / "adjuntos"
MAX_FILES = 10
MAX_BYTES = 20 * 1024 * 1024
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}


def save_batch(files: list[tuple[str, bytes]]) -> tuple[Path, list[dict]]:
    """Guarda los archivos en una carpeta nueva. Lanza ValueError si no valen."""
    if not files:
        raise ValueError("No hay archivos.")
    if len(files) > MAX_FILES:
        raise ValueError(f"Como maximo {MAX_FILES} archivos por tarea.")
    for name, content in files:
        if len(content) > MAX_BYTES:
            raise ValueError(f'"{name}" es demasiado grande (maximo 20 MB).')
    folder = ATTACH_ROOT / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    n = 2
    while folder.exists():  # dos tareas en el mismo segundo
        folder = folder.with_name(f"{folder.name}_{n}")
        n += 1
    folder.mkdir(parents=True)
    saved = []
    for name, content in files:
        name = _sanitize_filename(name or "archivo")
        path = folder / name
        stem, suffix, i = path.stem, path.suffix, 2
        while path.exists():
            path = folder / f"{stem}_{i}{suffix}"
            i += 1
        path.write_bytes(content)
        saved.append({"name": path.name, "path": path.as_posix(), "description": None})
    return folder, saved


def describe_images(saved: list[dict], describe: Callable[[str], str]) -> None:
    """Rellena "description" de las imagenes. Si la descripcion falla, se
    deja vacia: el agente sigue teniendo la ruta del archivo."""
    for item in saved:
        if Path(item["path"]).suffix.lower() not in IMAGE_SUFFIXES:
            continue
        try:
            b64 = base64.b64encode(Path(item["path"]).read_bytes()).decode("ascii")
            item["description"] = describe(b64).strip() or None
        except Exception:
            item["description"] = None


def task_note(saved: list[dict]) -> str:
    """Lo que se añade a la tarea para que el agente sepa que tiene."""
    if not saved:
        return ""
    folder = Path(saved[0]["path"]).parent.as_posix()
    lines = [f'Archivos adjuntos por el usuario (copias en "{folder}"; trabaja sobre ellas):']
    for item in saved:
        line = f'- "{item["path"]}"'
        if item.get("description"):
            line += f" - se ve: {item['description']}"
        lines.append(line)
    return "\n".join(lines)
