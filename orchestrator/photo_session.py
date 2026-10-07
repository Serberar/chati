"""La foto que se esta editando en cada conversacion, con todas sus versiones.

Por que (Sergio, 2026-10-05/06): editar fotos es lo que mas usa, hablando
normal: "ponme en la playa", luego "ahora con un sombrero", "no, mas grande",
"vuelve a como estaba". Antes solo se recordaba la ultima foto en la sesion de
login: se perdia al reiniciar Chati, no incluia las imagenes generadas desde
texto y una correccion acababa generando otra imagen o en el agente.

Cada conversacion guarda:
- versions: las imagenes en orden (nombres de media_store, cifradas aparte).
  versions[0] es la original (la subida o la generada): de ahi sale la cara.
- pos: la que se ve ahora (deshacer la mueve hacia atras sin borrar nada).
- requests: lo que se pidio para llegar a cada version (para que el
  planificador entienda "mas grande" o "quita lo de antes").

Usuarios registrados: en memory.db, cifrado con su clave (sobrevive a un
reinicio). Invitados: solo en su sesion (en memoria), como el resto de su modo.
"""

import json
import time

import memory

MAX_VERSIONS = 30  # cada una es una imagen guardada; mas atras no se vuelve


def _guest_store(auth_session: dict) -> dict:
    return auth_session.setdefault("photo_sessions", {})


def load(auth_session: dict, chat: str | None) -> dict | None:
    if not chat:
        return None
    if not auth_session.get("user_id"):
        return _guest_store(auth_session).get(chat)
    raw = memory.load_session_photo(chat, auth_session["user_id"], auth_session["dek"],
                                    auth_session["key_generation"])
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def _save(auth_session: dict, chat: str, state: dict) -> None:
    state["time"] = time.time()
    if not auth_session.get("user_id"):
        _guest_store(auth_session)[chat] = state
        return
    memory.save_session_photo(chat, auth_session["user_id"], json.dumps(state, ensure_ascii=False),
                              auth_session["dek"], auth_session["key_generation"])


def start(auth_session: dict, chat: str | None, name: str, origin: str) -> None:
    """Una foto nueva en la conversacion (subida o generada): empieza de cero.
    origin: "subida" o "generada" (con una generada no hay cara "real" que
    proteger, pero se trata igual: su cara es la de la primera version)."""
    if chat:
        _save(auth_session, chat, {"versions": [name], "pos": 0, "requests": [""], "origin": origin})


def push(auth_session: dict, chat: str | None, name: str, request: str) -> None:
    """Una version nueva encima de la que se ve ahora. Si se habia deshecho
    algo, lo deshecho se descarta (como en cualquier editor)."""
    state = load(auth_session, chat)
    if not chat or state is None:
        return
    pos = state["pos"]
    versions = state["versions"][:pos + 1] + [name]
    requests = state["requests"][:pos + 1] + [request]
    if len(versions) > MAX_VERSIONS:  # la original (cara) se queda siempre
        versions = versions[:1] + versions[-(MAX_VERSIONS - 1):]
        requests = requests[:1] + requests[-(MAX_VERSIONS - 1):]
    state.update(versions=versions, requests=requests, pos=len(versions) - 1)
    _save(auth_session, chat, state)


def move(auth_session: dict, chat: str | None, to: str) -> str | None:
    """to: "anterior" o "original". Devuelve el nombre de la version que queda
    a la vista, o None si no hay a donde ir."""
    state = load(auth_session, chat)
    if state is None:
        return None
    target = 0 if to == "original" else state["pos"] - 1
    if target < 0 or target == state["pos"]:
        return None
    state["pos"] = target
    _save(auth_session, chat, state)
    return state["versions"][target]


def current(state: dict) -> str:
    return state["versions"][state["pos"]]


def original(state: dict) -> str:
    return state["versions"][0]


def previous(state: dict) -> str | None:
    return state["versions"][state["pos"] - 1] if state["pos"] > 0 else None


def requests_so_far(state: dict) -> list[str]:
    """Lo pedido hasta la version que se ve, en orden (sin la original)."""
    return [r for r in state["requests"][1:state["pos"] + 1] if r]


def last_request(state: dict) -> str:
    return state["requests"][state["pos"]] if state["pos"] > 0 else ""
