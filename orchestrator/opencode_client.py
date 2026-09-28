"""Cliente para la API HTTP de OpenCode (el agente de codigo local, ver
C:\\AI\\AGENTS.md - proyecto de terceros, licencia MIT, https://opencode.ai).
Chati no reimplementa el agente: solo le pone una interfaz sencilla encima
(modo "Agente" de index.html) - crear la tarea, seguir su progreso, y
contestar las preguntas/permisos que pida sin salir de la app.

Medido en vivo, incluso una tarea trivial tarda minutos (el modelo corre en
CPU puro y OpenCode manda un prompt grande con todas sus herramientas), asi
que nada de aqui espera a que la tarea termine: se lanza de forma asincrona
y la interfaz consulta el estado cada pocos segundos (get_task_view)."""

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import requests

TIMEOUT = 15

# Sin esto el modelo no sabe en que sistema esta y se para a preguntar cosas
# que podria averiguar solo (medido: pregunto "¿cual es la ruta del
# escritorio? /home/user/Desktop, /Users/..., C:\\Users\\..." y se quedo
# esperando horas una respuesta que nadie veia).
_HOME = Path(os.environ.get("USERPROFILE") or Path.home())
SYSTEM_CONTEXT = (
    "Contexto del equipo: Windows 11, shell PowerShell. "
    f"Carpeta personal del usuario: {_HOME.as_posix()}. Escritorio: {(_HOME / 'Desktop').as_posix()}. "
    f"Descargas: {(_HOME / 'Downloads').as_posix()}. Escribe las rutas con barras normales (/), "
    "que Windows acepta igual. "
    "No preguntes datos que puedas averiguar tu mismo (rutas, sistema operativo, "
    "contenido de archivos) - pregunta solo si la tarea es ambigua de verdad. "
    "Responde siempre en español. Al terminar, resume en una o dos frases que has hecho."
)

# Lo registra main.py: deja memoria libre para el modelo del agente antes de
# lanzar una tarea (punto unico: modo Agente, chat y herramienta de delegar
# pasan todos por start_task). Recibe el nombre del agente de OpenCode.
before_task: Callable[[str | None], None] | None = None

# Carpeta de trabajo del agente (roadmap n.º 6): la fija main.py. OpenCode
# solo registra -y puede deshacer- los cambios hechos dentro de ella, y solo
# si es un repositorio git (medido el 2026-09-28: sin git, 0 cambios
# registrados y deshacer no hacia nada). None = la carpeta en la que arranco
# OpenCode, sin poder deshacer.
WORKDIR: Path | None = None


def _dir() -> dict:
    return {"params": {"directory": str(WORKDIR)}} if WORKDIR else {}


def workdir_undoable() -> bool:
    return bool(WORKDIR) and (WORKDIR / ".git").exists()


def ensure_workdir() -> None:
    """Crea la carpeta de trabajo y la convierte en repositorio git (sin
    commits: OpenCode guarda sus propias instantaneas aparte). Sin git
    instalado se queda como carpeta normal y no se ofrece deshacer."""
    if not WORKDIR:
        return
    WORKDIR.mkdir(parents=True, exist_ok=True)
    if not (WORKDIR / ".git").exists() and shutil.which("git"):
        subprocess.run(["git", "init", "-q", str(WORKDIR)], check=False,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# herramientas internas de OpenCode que no aportan nada al usuario como "paso"
_HIDDEN_TOOLS = {"question", "todowrite", "todoread"}


def create_session(base_url: str) -> str:
    ensure_workdir()
    resp = requests.post(f"{base_url}/session", json={}, **_dir(), timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()["id"]


# ruta de Windows: unidad + barra invertida, hasta un caracter que no puede ir en una ruta
_WINDOWS_PATH = re.compile(r'[A-Za-z]:\\[^"\n<>|?*]*')


def forward_slash_paths(text: str) -> str:
    """C:\\Users\\x -> C:/Users/x. Medido el 2026-09-28 con qwen3:8b: con la ruta
    con barras invertidas en la tarea, el modelo escribia la llamada a la
    herramienta como JSON invalido (sin escapar las barras) y Ollama la
    descartaba en silencio - 0 de 4 respuestas utiles; con barras normales,
    4 de 4. Windows acepta las dos."""
    return _WINDOWS_PATH.sub(lambda m: m.group(0).replace("\\", "/"), text)


def send_prompt_async(base_url: str, session_id: str, text: str, agent: str | None = None) -> None:
    system = SYSTEM_CONTEXT
    if WORKDIR:
        system += (f" Tu carpeta de trabajo es {WORKDIR.as_posix()}: si el usuario no dice donde "
                   "guardar algo, guardalo ahi (lo que cambies en ella se puede deshacer).")
    body = {"parts": [{"type": "text", "text": forward_slash_paths(text)}], "system": system}
    if agent:
        body["agent"] = agent
    resp = requests.post(f"{base_url}/session/{session_id}/prompt_async", json=body, **_dir(), timeout=TIMEOUT)
    resp.raise_for_status()


def start_task(base_url: str, task: str, agent: str | None = None) -> str:
    if before_task is not None:
        before_task(agent)
    session_id = create_session(base_url)
    send_prompt_async(base_url, session_id, task, agent)
    return session_id


def continue_task(base_url: str, session_id: str, text: str) -> None:
    """Otro mensaje en una tarea ya existente ("no, ponlo en la carpeta
    Trabajo"), con el mismo agente con el que empezo (rapido o potente) - el
    agente ve todo lo anterior de la tarea, no empieza de cero."""
    agent = _get(base_url, f"/session/{session_id}").get("agent")
    if before_task is not None:
        before_task(agent)
    send_prompt_async(base_url, session_id, text, agent)


def delegate(base_url: str, task: str, agent: str | None = None) -> tuple[str, str | None]:
    """Lanza la tarea desde el chat normal (router o herramienta
    delegar_a_agente_de_codigo). Devuelve (texto a mostrar, id de la tarea) -
    el id lo usa la interfaz para pintar la tarjeta del agente en el propio
    chat. No lanza si OpenCode no esta corriendo: lo reporta como texto."""
    try:
        session_id = start_task(base_url, task, agent)
    except requests.RequestException as exc:
        return f"No se pudo contactar con el agente de codigo (¿esta arrancado?): {exc}", None
    return (
        "Se lo he pasado al agente. Veras aqui debajo lo que va haciendo, y te pedira "
        "permiso antes de cambiar nada. Puede tardar unos minutos.",
        session_id,
    )


def _get(base_url: str, path: str):
    resp = requests.get(f"{base_url}{path}", **_dir(), timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _tool_detail(tool: str, inp: dict) -> str:
    if tool == "bash":
        return inp.get("command", "")
    for key in ("filePath", "path", "pattern", "url"):
        if inp.get(key):
            return str(inp[key])
    return ""


# Salida de los comandos (terminal en directo, roadmap n.º 5): OpenCode la va
# guardando en state.metadata.output mientras el comando corre (medido: una
# actualizacion por linea) y en state.output al acabar. Se manda solo el final,
# que es lo que se ve en la tarjeta.
TERMINAL_MAX_CHARS = 4000
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _terminal_output(state: dict) -> str:
    out = (state.get("metadata") or {}).get("output") or state.get("output") or ""
    out = _ANSI.sub("", out).replace("\r\n", "\n")
    if len(out) > TERMINAL_MAX_CHARS:
        out = "…\n" + out[-TERMINAL_MAX_CHARS:]
    return out


def _simplify_parts(messages: list) -> tuple[list, str | None]:
    """Pasos a mostrar. Los mensajes del usuario salen como pasos "user"
    (continuaciones de la tarea), salvo el primero: es la tarea en si, que la
    interfaz ya enseña encima de la tarjeta."""
    steps = []
    error = None
    first_user_seen = False
    for msg in messages:
        info = msg.get("info", {})
        if info.get("role") == "user":
            if first_user_seen:
                text = " ".join(p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text").strip()
                if text:
                    steps.append({"kind": "user", "text": text})
            first_user_seen = True
            continue
        if info.get("role") != "assistant":
            continue
        if info.get("error"):
            err = info["error"]
            error = (err.get("data") or {}).get("message") or err.get("name") or "Error desconocido"
        useful = any(p.get("type") in ("text", "tool") for p in msg.get("parts", []))
        if info.get("finish") == "length" and not useful:
            # el modelo pequeño a veces entra en bucle hasta el limite de salida
            # sin producir nada (visto el 2026-09-28): sin esto la tarjeta
            # decia "Terminado" con la tarjeta vacia
            error = "El modelo se ha atascado sin llegar a hacer nada. Prueba otra vez o usa el modelo potente."
        for part in msg.get("parts", []):
            if part.get("type") == "text" and part.get("text", "").strip():
                steps.append({"kind": "text", "text": part["text"]})
            elif part.get("type") == "tool" and part.get("tool") not in _HIDDEN_TOOLS:
                state = part.get("state", {})
                step = {
                    "kind": "tool",
                    "tool": part.get("tool"),
                    "status": state.get("status"),
                    "title": state.get("title") or "",
                    "detail": _tool_detail(part.get("tool", ""), state.get("input") or {}),
                    "error": state.get("error"),
                }
                if part.get("tool") == "bash":
                    step["output"] = _terminal_output(state)
                    step["exit"] = (state.get("metadata") or {}).get("exit")
                steps.append(step)
    return steps, error


def get_task_view(base_url: str, session_id: str) -> dict:
    """Todo lo que la interfaz necesita para pintar una tarea en un solo
    viaje: estado, pasos (texto y herramientas usadas), y las preguntas y
    permisos pendientes de ESTA tarea."""
    session = _get(base_url, f"/session/{session_id}")
    statuses = _get(base_url, "/session/status")
    messages = _get(base_url, f"/session/{session_id}/message")
    questions = [q for q in _get(base_url, "/question") if q.get("sessionID") == session_id]
    permissions = [p for p in _get(base_url, "/permission") if p.get("sessionID") == session_id]

    # /session/status solo lista las sesiones activas - ausente = parada
    status = statuses.get(session_id, {"type": "idle"})
    steps, error = _simplify_parts(messages)
    first_user = next((m for m in messages if m.get("info", {}).get("role") == "user"), None)
    task = " ".join(p.get("text", "") for p in (first_user or {}).get("parts", [])
                    if p.get("type") == "text").strip()
    return {
        "session_id": session_id,
        "task": task,
        "title": session.get("title", ""),
        "model": (session.get("model") or {}).get("id"),
        "status": status.get("type", "idle"),
        "retry_message": status.get("message"),
        "steps": steps,
        "error": error,
        "questions": [{"id": q["id"], "questions": q.get("questions", [])} for q in questions],
        "permissions": [{
            "id": p["id"],
            "permission": p.get("permission", ""),
            "patterns": p.get("patterns", []),
        } for p in permissions],
        "changes": _changes_summary(messages),
        "reverted": bool(session.get("revert")),
        "undoable": workdir_undoable(),
    }


def _changes_summary(messages: list) -> dict:
    """Archivos distintos cambiados en la carpeta de trabajo y lineas +/-."""
    files, additions, deletions = set(), 0, 0
    for msg in messages:
        info = msg.get("info", {})
        if info.get("role") == "user":
            for d in (info.get("summary") or {}).get("diffs") or []:
                files.add(d.get("file"))
                additions += d.get("additions", 0)
                deletions += d.get("deletions", 0)
    return {"files": len(files), "additions": additions, "deletions": deletions}


def list_tasks(base_url: str, limit: int = 20) -> list:
    sessions = _get(base_url, "/session")
    statuses = _get(base_url, "/session/status")
    sessions = [s for s in sessions if not s.get("parentID")]  # subagentes internos, no tareas del usuario
    sessions.sort(key=lambda s: s.get("time", {}).get("updated", 0), reverse=True)
    return [{
        "session_id": s["id"],
        "title": s.get("title", ""),
        "updated": s.get("time", {}).get("updated"),
        "status": statuses.get(s["id"], {"type": "idle"}).get("type", "idle"),
    } for s in sessions[:limit]]


def task_summary(base_url: str, session_id: str) -> str:
    """Nombre legible de una tarea: su titulo, o lo que pidio el usuario si
    OpenCode no le puso titulo ("New session - 2026-09-25T07:55...")."""
    title = _get(base_url, f"/session/{session_id}").get("title", "")
    if title and not title.startswith("New session"):
        return title
    for msg in _get(base_url, f"/session/{session_id}/message"):
        if msg.get("info", {}).get("role") == "user":
            for part in msg.get("parts", []):
                if part.get("type") == "text" and part.get("text", "").strip():
                    text = part["text"].strip()
                    return text if len(text) <= 70 else text[:67] + "..."
    return title


def waiting_sessions(base_url: str) -> dict[str, str]:
    """{session_id: "question" | "permission"} de las tareas paradas esperando
    al usuario. {} si OpenCode no responde."""
    waiting: dict[str, str] = {}
    try:
        for path, reason in (("/permission", "permission"), ("/question", "question")):
            for item in requests.get(f"{base_url}{path}", **_dir(), timeout=3).json():
                waiting[item["sessionID"]] = reason
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return {}
    return waiting


def stop_task(base_url: str, session_id: str) -> None:
    """Cierra una tarea del todo: rechaza lo que tenga pendiente (pregunta o
    permiso) y la detiene. Solo detenerla la dejaria con la pregunta colgando."""
    for question in _get(base_url, "/question"):
        if question.get("sessionID") == session_id:
            reject_question(base_url, question["id"])
    for permission in _get(base_url, "/permission"):
        if permission.get("sessionID") == session_id:
            reply_permission(base_url, permission["id"], "reject")
    abort(base_url, session_id)


def busy_session_ids(base_url: str) -> list[str]:
    """Tareas del agente trabajando ahora mismo (las que necesitan su modelo
    cargado). Las que esperan una respuesta o un permiso del usuario NO
    cuentan aunque OpenCode las marque "busy": una pregunta sin contestar
    retendria 26GB de RAM indefinidamente (paso de verdad: una tarea llevaba
    dias esperando). Si OpenCode no responde, ninguna."""
    try:
        statuses = requests.get(f"{base_url}/session/status", **_dir(), timeout=3).json()
        waiting = {item["sessionID"]
                   for path in ("/question", "/permission")
                   for item in requests.get(f"{base_url}{path}", **_dir(), timeout=3).json()}
        return [sid for sid, st in statuses.items() if st.get("type") != "idle" and sid not in waiting]
    except (requests.RequestException, ValueError, AttributeError, KeyError, TypeError):
        return []


def reply_question(base_url: str, request_id: str, answers: list[list[str]]) -> None:
    resp = requests.post(f"{base_url}/question/{request_id}/reply",
                         json={"answers": answers}, **_dir(), timeout=TIMEOUT)
    resp.raise_for_status()


def reject_question(base_url: str, request_id: str) -> None:
    resp = requests.post(f"{base_url}/question/{request_id}/reject", json={}, **_dir(), timeout=TIMEOUT)
    resp.raise_for_status()


def reply_permission(base_url: str, request_id: str, reply: str) -> None:
    resp = requests.post(f"{base_url}/permission/{request_id}/reply",
                         json={"reply": reply}, **_dir(), timeout=TIMEOUT)
    resp.raise_for_status()


PATCH_MAX_CHARS = 8000


def get_changes(base_url: str, session_id: str) -> dict:
    """Lo que ha cambiado la tarea en la carpeta de trabajo, por mensaje (la
    tarea y cada "continuar"): OpenCode lo guarda en cada mensaje del usuario
    (info.summary.diffs) - el diff de la sesion entera viene vacio."""
    turns = []
    for msg in _get(base_url, f"/session/{session_id}/message"):
        info = msg.get("info", {})
        if info.get("role") != "user":
            continue
        diffs = (info.get("summary") or {}).get("diffs") or []
        text = " ".join(p.get("text", "") for p in msg.get("parts", []) if p.get("type") == "text").strip()
        turns.append({
            "message_id": info.get("id"),
            "text": text if len(text) <= 90 else text[:87] + "...",
            "files": [{
                "file": d.get("file", ""),
                "status": d.get("status", "modified"),
                "additions": d.get("additions", 0),
                "deletions": d.get("deletions", 0),
                "patch": (d.get("patch") or "")[:PATCH_MAX_CHARS],
            } for d in diffs],
        })
    session = _get(base_url, f"/session/{session_id}")
    return {"turns": turns, "reverted": bool(session.get("revert")),
            "workdir": WORKDIR.as_posix() if WORKDIR else None}


def revert_task(base_url: str, session_id: str) -> None:
    """Deshace todo lo que la tarea cambio en la carpeta de trabajo (desde su
    primer mensaje). Se puede volver atras con unrevert_task mientras no se
    mande otro mensaje a la tarea."""
    messages = _get(base_url, f"/session/{session_id}/message")
    first_user = next((m for m in messages if m.get("info", {}).get("role") == "user"), None)
    if first_user is None:
        return
    resp = requests.post(f"{base_url}/session/{session_id}/revert",
                         json={"messageID": first_user["info"]["id"]}, **_dir(), timeout=60)
    resp.raise_for_status()


def unrevert_task(base_url: str, session_id: str) -> None:
    resp = requests.post(f"{base_url}/session/{session_id}/unrevert", json={}, **_dir(), timeout=60)
    resp.raise_for_status()


def abort(base_url: str, session_id: str) -> None:
    resp = requests.post(f"{base_url}/session/{session_id}/abort", json={}, **_dir(), timeout=TIMEOUT)
    resp.raise_for_status()
