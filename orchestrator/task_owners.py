"""De quien es cada tarea del agente (OpenCode no sabe nada de los usuarios
de Chati). Auditoria 2026-09-30: cualquier usuario con permiso para usar el
ordenador veia, seguia, aprobaba o deshacia las tareas de los demas.

Solo se guardan ids (session_id -> user_id), nada del contenido. Las tareas
anteriores a este registro no tienen dueño: se consideran del administrador
(entonces solo existia el)."""

import json
import threading

import paths
import atomic

_lock = threading.Lock()


def _file():
    return paths.DATA_DIR / "agent_task_owners.json"


def _load() -> dict[str, str]:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def record(session_id: str | None, user_id: str | None) -> None:
    if not session_id or not user_id:
        return
    with _lock:
        owners = _load()
        owners[session_id] = user_id
        _file().parent.mkdir(parents=True, exist_ok=True)
        atomic.write_text(_file(), json.dumps(owners))


def owner(session_id: str) -> str | None:
    return _load().get(session_id)


def may_use(session_id: str, user_id: str | None, is_admin: bool) -> bool:
    if not user_id:
        return False
    found = owner(session_id)
    return found == user_id or (found is None and is_admin)


def forget_user(user_id: str) -> None:
    with _lock:
        owners = {sid: uid for sid, uid in _load().items() if uid != user_id}
        if _file().exists():
            atomic.write_text(_file(), json.dumps(owners))
