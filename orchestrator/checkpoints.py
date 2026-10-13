"""Copias del proyecto antes de cada encargo del modo Codigo, para el boton
"Deshacer" (Sergio, 2026-10-13: con el modo "sin preguntar" hace falta poder
volver atras si no gusta lo que hizo el agente).

Cada carpeta tiene su propio git "en la sombra" en DATA_DIR/agente_copias:
no toca el .git del proyecto (si lo tiene) y git solo guarda una vez cada
contenido, asi que cien copias de un proyecto ocupan poco mas que una."""
import hashlib
import json
import subprocess
import threading
import time
from pathlib import Path

from paths import DATA_DIR

COPIES_DIR = DATA_DIR / "agente_copias"
SESSIONS_FILE = COPIES_DIR / "sesiones.json"
MAX_PER_SESSION = 30
IGNORE = ["venv/", ".venv/", "env/", "node_modules/", ".git/", "__pycache__/", ".pytest_cache/", "*.pyc",
          "dist/", "build/", ".mypy_cache/", ".ruff_cache/"]
_lock = threading.Lock()


def _repo(folder: Path) -> Path:
    key = hashlib.sha1(str(folder.resolve()).lower().encode()).hexdigest()[:16]
    return COPIES_DIR / f"{key}.git"


def _git(folder: Path, *args: str) -> subprocess.CompletedProcess:
    repo = _repo(folder)
    return subprocess.run(["git", f"--git-dir={repo}", f"--work-tree={folder}", "-c", "user.name=Chati",
                           "-c", "user.email=chati@local", "-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
                           *args], capture_output=True, text=True, timeout=300, encoding="utf-8", errors="replace")


def _load() -> dict:
    try:
        return json.loads(SESSIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _store(data: dict) -> None:
    COPIES_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def _commit(folder: Path, label: str) -> str | None:
    repo = _repo(folder)
    if not repo.exists():
        COPIES_DIR.mkdir(parents=True, exist_ok=True)
        if subprocess.run(["git", "init", "--bare", "-q", str(repo)], capture_output=True).returncode != 0:
            return None
        (repo / "info").mkdir(exist_ok=True)
        (repo / "info" / "exclude").write_text("\n".join(IGNORE) + "\n", encoding="utf-8")
    if _git(folder, "add", "-A", ".").returncode != 0:
        return None
    _git(folder, "commit", "-q", "--allow-empty", "--no-verify", "-m", label[:200])
    out = _git(folder, "rev-parse", "HEAD")
    return out.stdout.strip() if out.returncode == 0 else None


def take(folder: Path, label: str) -> str | None:
    """Guarda como esta el proyecto ahora, antes de un encargo. None si no se
    pudo (sin git, carpeta rara): el encargo sigue igual, solo sin deshacer."""
    try:
        with _lock:
            return _commit(folder, label)
    except (OSError, subprocess.SubprocessError):
        return None


def record(session_id: str, folder: Path, commit: str, label: str) -> None:
    """Apunta la copia en la tarea (al empezar aun no se sabe su id)."""
    with _lock:
        data = _load()
        entries = data.setdefault(session_id, [])
        entries.append({"commit": commit, "carpeta": str(folder), "encargo": label[:200], "hora": time.time()})
        data[session_id] = entries[-MAX_PER_SESSION:]
        _store(data)


def snapshot(session_id: str, folder: Path, label: str) -> str | None:
    commit = take(folder, label)
    if commit:
        record(session_id, folder, commit, label)
    return commit


def available(session_id: str) -> int:
    """Cuantas veces se puede deshacer en esta tarea."""
    return len(_load().get(session_id, []))


def undo(session_id: str) -> dict:
    """Deja el proyecto como estaba antes del ultimo encargo de la tarea.
    Antes guarda como esta ahora (por si se arrepiente) y dice que archivos
    han cambiado."""
    with _lock:
        data = _load()
        entries = data.get(session_id) or []
        if not entries:
            return {"ok": False, "detail": "No hay nada que deshacer en esta tarea."}
        last = entries[-1]
        folder = Path(last["carpeta"])
        if not folder.is_dir():
            return {"ok": False, "detail": f"Ya no existe la carpeta {folder}."}
        current = _commit(folder, f"antes de deshacer: {last['encargo']}")
        if not current:
            return {"ok": False, "detail": "No se pudo guardar el estado actual; no se ha tocado nada."}
        changed = _git(folder, "diff", "--name-status", "--no-renames", last["commit"], current).stdout.split("\n")
        # read-tree pone el indice y los archivos como en la copia y quita los
        # que se crearon despues (solo los que vigila: nunca venv ni .git)
        out = _git(folder, "read-tree", "-u", "--reset", last["commit"])
        if out.returncode != 0:
            return {"ok": False, "detail": f"No se pudo deshacer: {out.stderr.strip()[-300:]}"}
        data[session_id] = entries[:-1]
        _store(data)
    files = []
    for line in filter(None, changed):
        status, _, name = line.partition("\t")
        files.append({"A": f"borrado {name}", "D": f"recuperado {name}"}.get(status, f"restaurado {name}"))
    return {"ok": True, "archivos": files, "quedan": len(entries) - 1, "encargo": last["encargo"]}


def diff_since_last(session_id: str, max_chars: int = 12000, exclude: tuple[str, ...] = ()) -> str:
    """Lo que ha cambiado en el proyecto desde la copia del ultimo encargo
    (para que el agente revise su propio trabajo), recortado. "" si no hay."""
    entries = _load().get(session_id) or []
    if not entries:
        return ""
    last = entries[-1]
    folder = Path(last["carpeta"])
    try:
        with _lock:
            if _git(folder, "add", "-A", ".").returncode != 0:
                return ""
            out = _git(folder, "diff", "--cached", "--no-color", "--no-renames", last["commit"], "--", ".",
                       *(f":(exclude){name}" for name in exclude))
    except (OSError, subprocess.SubprocessError):
        return ""
    text = out.stdout if out.returncode == 0 else ""
    return text if len(text) <= max_chars else text[:max_chars] + "\n[... recortado ...]"
