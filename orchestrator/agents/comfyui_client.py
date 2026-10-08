import json
import logging
import threading
import time
import uuid
from contextvars import ContextVar
from pathlib import Path
from typing import Callable

import requests

import paths


class GenerationCancelled(Exception):
    pass


# Lo registra main.py: libera la GPU/RAM que ocupan los modelos de texto de
# Ollama antes de cada generacion. Aqui porque TODA generacion de imagen y
# video pasa por submit_and_wait - un solo punto en vez de uno por endpoint.
before_submit: Callable[[], None] | None = None

# De quien es cada generacion (ComfyUI no sabe nada de los usuarios de Chati).
# El boton de parar de un usuario cortaba TODO lo que hubiera en ComfyUI,
# tambien lo de los demas, invitados incluidos (auditoria 2026-10-05: Sergio
# paro su edicion y se cancelo la de otra prueba a la vez). main.py pone el
# dueño de cada peticion en current_owner.
current_owner: ContextVar[str | None] = ContextVar("comfy_owner", default=None)
_owners: dict[str, str] = {}
# y de que conversacion: el boton "Ver" de las tareas en marcha lleva a ella
# (Sergio, pendiente.md 2026-10-08). main._own_session_id lo pone.
current_conversation: ContextVar[str | None] = ContextVar("comfy_conversation", default=None)
_conversations: dict[str, str] = {}
_owners_lock = threading.Lock()


# Generaciones en marcha (de cualquiera, tambien los calentamientos). Mientras
# haya alguna, Ollama no debe meter modelos en la GPU: con 8 GB, un chat a la vez
# que Kontext lo dejaba sin memoria y una edicion de 2,5 min se atascaba 15
# (2026-10-06). Ver OllamaClient.gpu_busy en main.py.
_active = 0
_active_lock = threading.Lock()


def generation_running() -> bool:
    with _active_lock:
        return _active > 0


_queue_seen = {"at": 0.0, "busy": False}


def gpu_busy(base_url: str) -> bool:
    """Hay una imagen generandose, de este proceso o de CUALQUIER otro (se le
    pregunta a ComfyUI, con la respuesta guardada 2 s). Solo con el contador
    de este proceso, otro programa que hablaba con Ollama (los tests, el
    agente) le metia el modelo de chat en la GPU a una edicion en marcha y la
    dejaba 15 minutos atascada hasta cancelarse (2026-10-07)."""
    if generation_running():
        return True
    now = time.time()
    if now - _queue_seen["at"] > 2:
        try:
            queue = requests.get(f"{base_url}/queue", timeout=0.5).json()
            busy = bool(queue.get("queue_running") or queue.get("queue_pending"))
        except (requests.RequestException, ValueError, AttributeError):
            busy = False  # ComfyUI apagado o sin responder: nada que proteger
        _queue_seen.update(at=now, busy=busy)
    return _queue_seen["busy"]


def owner_of(prompt_id: str) -> str | None:
    with _owners_lock:
        return _owners.get(prompt_id)


def stop_owned(base_url: str, owner: str) -> int:
    """Para solo las generaciones de `owner`. Devuelve cuantas."""
    mine = [j for j in user_jobs(base_url) if owner_of(j["id"]) == owner]
    for job in mine:
        stop_job(base_url, job["id"])
    return len(mine)

def _comfy_dir() -> Path:
    return paths.DATA_ROOT / "ComfyUI"


# carpeta de ComfyUI (su input/ y output/); los tests la cambian
COMFY_DIR: Callable[[], Path] | Path = _comfy_dir

# memoria de GPU que retiene ComfyUI sin ningun modelo cargado (medido: ~60MB)
FREED_VRAM_BYTES = 512 * 1024 * 1024


def _is_still_queued(base_url: str, prompt_id: str) -> bool:
    try:
        queue = requests.get(f"{base_url}/queue", timeout=10).json()
    except (requests.RequestException, ValueError):
        return True  # no contesta (ocupado): no se da por cancelada
    if not isinstance(queue, dict):
        return True
    items = queue.get("queue_running", []) + queue.get("queue_pending", [])
    return any(item[1] == prompt_id for item in items)


def _in_history(base_url: str, prompt_id: str) -> bool:
    try:
        return prompt_id in requests.get(f"{base_url}/history/{prompt_id}", timeout=10).json()
    except (requests.RequestException, ValueError):
        return True  # no contesta: mejor seguir esperando que darla por cancelada


def submit_and_wait(base_url: str, workflow: dict, save_node: str, output_keys: list[str] | str,
                     timeout: int) -> dict:
    """Envia un workflow a ComfyUI y espera el resultado, con deteccion real
    de cancelacion. Dos casos distintos, comprobados a mano (ver notas):
    1. Se interrumpe ANTES de que el trabajo empiece a ejecutarse (todavia en
       cola): ComfyUI no deja ningun rastro en /history, simplemente
       desaparece de la cola. Se detecta comprobando la cola.
    2. Se interrumpe DESPUES de empezar a ejecutarse: SI aparece en /history,
       con status_str='error' pero con 'execution_interrupted' en los
       mensajes - hay que distinguirlo de un fallo real, si no se reporta
       como error generico en vez de como cancelacion.

    Limite real que no se puede evitar desde aqui: /interrupt de ComfyUI solo
    hace efecto entre pasos del sampler, no durante la carga de pesos del
    modelo. Si el trabajo esta cargando el modelo (puede tardar decenas de
    segundos en frio), cancelar tarda lo que tarde esa carga, no es instantaneo."""
    if isinstance(output_keys, str):
        output_keys = [output_keys]
    global _active
    if before_submit is not None:
        before_submit()
    with _active_lock:
        _active += 1
    try:
        return _submit_and_wait(base_url, workflow, save_node, output_keys, timeout)
    finally:
        with _active_lock:
            _active -= 1


def _submit_and_wait(base_url: str, workflow: dict, save_node: str, output_keys: list[str],
                     timeout: int) -> dict:

    client_id = str(uuid.uuid4())
    resp = requests.post(
        f"{base_url}/prompt",
        json={"prompt": workflow, "client_id": client_id},
        timeout=30,
    )
    resp.raise_for_status()
    prompt_id = resp.json()["prompt_id"]
    owner, conversation = current_owner.get(), current_conversation.get()
    with _owners_lock:
        if owner:
            _owners[prompt_id] = owner
        if conversation:
            _conversations[prompt_id] = conversation

    deadline = time.time() + timeout
    grace_deadline = time.time() + 5  # margen inicial: no comprobar la cola nada mas enviar (posible carrera)

    while time.time() < deadline:
        try:
            hist = requests.get(f"{base_url}/history/{prompt_id}", timeout=10).json()
        except (requests.RequestException, ValueError):
            # ComfyUI ocupado (cargando el modelo) tarda en contestar: no es un
            # fallo de la generacion. Antes una consulta lenta tiraba una
            # edicion de 4 minutos ("Read timed out", 2026-10-06)
            time.sleep(2)
            continue
        if prompt_id in hist:
            status = hist[prompt_id].get("status", {})
            if status.get("status_str") == "error":
                forget(base_url, prompt_id, workflow)
                messages = status.get("messages", [])
                was_interrupted = any(m[0] == "execution_interrupted" for m in messages)
                if was_interrupted:
                    raise GenerationCancelled(f"Generacion cancelada (prompt_id={prompt_id})")
                raise RuntimeError(f"ComfyUI fallo: {status}")
            outputs = hist[prompt_id]["outputs"]
            node_output = outputs.get(save_node, {})
            items = []
            for key in output_keys:
                items = node_output.get(key, [])
                if items:
                    break
            if not items:
                raise RuntimeError(f"ComfyUI no devolvio resultado en '{save_node}.{output_keys}': {outputs}")
            info = items[0]
            file_resp = requests.get(
                f"{base_url}/view",
                params={"filename": info["filename"], "subfolder": info.get("subfolder", ""),
                        "type": info.get("type", "output")},
                timeout=60,
            )
            file_resp.raise_for_status()
            forget(base_url, prompt_id, workflow, info)
            return {"content": file_resp.content, "prompt_id": prompt_id}

        if time.time() > grace_deadline and not _is_still_queued(base_url, prompt_id):
            # pudo terminar justo entre mirar el historial y mirar la cola: ya
            # no esta en la cola pero si en el historial. Sin volver a mirar,
            # una ampliacion de 17 s que salio bien se daba por cancelada y la
            # edicion entera fallaba (2026-10-07)
            if _in_history(base_url, prompt_id):
                continue
            forget(base_url, prompt_id, workflow)
            raise GenerationCancelled(f"Generacion cancelada (prompt_id={prompt_id})")

        time.sleep(1)

    # y se para en ComfyUI: si no, seguia trabajando "zombi" y la siguiente
    # edicion iba a paso de tortuga peleando por la GPU (2026-10-06)
    try:
        stop_job(base_url, prompt_id)
    except requests.RequestException:
        pass
    # tambien aqui: si no, la foto subida se quedaba en claro en input/ (auditoria 2026-10-05)
    forget(base_url, prompt_id, workflow)
    raise TimeoutError(f"Generacion no termino en {timeout}s (prompt_id={prompt_id})")


def upload_unique(base_url: str, image_bytes: bytes, filename: str) -> str:
    """Sube una imagen a input/ de ComfyUI con un nombre que no comparte con
    ninguna otra peticion; devuelve el nombre para el nodo LoadImage. forget()
    la borra al terminar."""
    suffix = Path(filename).suffix.lower() or ".png"
    unique = f"chati_{uuid.uuid4().hex}{suffix if suffix in ('.png', '.jpg', '.jpeg', '.webp') else '.png'}"
    resp = requests.post(f"{base_url}/upload/image", files={"image": (unique, image_bytes)}, timeout=30)
    resp.raise_for_status()
    return resp.json()["name"]


def _try_unlink(path: Path) -> bool:
    try:
        path.unlink(missing_ok=True)
        return True
    except OSError:
        return False


def _unlink_later(paths: list[Path], attempts: int = 20, delay: float = 1.0) -> None:
    for _ in range(attempts):
        time.sleep(delay)
        paths = [p for p in paths if not _try_unlink(p)]
        if not paths:
            return
    for p in paths:
        logging.getLogger("chati").warning("No se pudo borrar la copia de ComfyUI %s", p.name)


def forget(base_url: str, prompt_id: str, workflow: dict, output_info: dict | None = None) -> None:
    """ComfyUI guarda su propia copia de todo: la foto subida (input/), el
    resultado (output/) y el texto pedido (/history). Chati ya se ha quedado
    el resultado cifrado, asi que aqui se borra el rastro en claro (auditoria
    2026-09-29: habia 87 fotos de cara y 181 imagenes sin cifrar en ComfyUI).
    Nunca falla: si algo no se puede borrar, no rompe la generacion."""
    with _owners_lock:
        _owners.pop(prompt_id, None)
        _conversations.pop(prompt_id, None)
    root = COMFY_DIR() if callable(COMFY_DIR) else COMFY_DIR
    targets = []
    if output_info and output_info.get("filename"):
        kind = output_info.get("type", "output")
        if kind in ("output", "temp"):
            targets.append(root / kind / output_info.get("subfolder", "") / output_info["filename"])
    for node in (workflow or {}).values():
        if not isinstance(node, dict) or not str(node.get("class_type", "")).startswith("LoadImage"):
            continue
        name = (node.get("inputs") or {}).get("image")
        if isinstance(name, str) and name and "/" not in name and "\\" not in name and ".." not in name:
            targets.append(root / "input" / name)
    pending = [p for p in targets if not _try_unlink(p)]
    if pending:
        # ComfyUI tarda un momento en soltar el archivo que acaba de servir
        # (WinError 32, medido 2026-09-29): se reintenta sin hacer esperar
        threading.Thread(target=_unlink_later, args=(pending,), daemon=True).start()
    try:
        requests.post(f"{base_url}/history", json={"delete": [prompt_id]}, timeout=5)
    except requests.RequestException:
        pass


def free_memory(base_url: str) -> None:
    """Pide a ComfyUI que suelte los modelos que retiene en RAM/VRAM entre
    generaciones (los vuelve a cargar en la siguiente). Si no responde, se
    ignora - liberar memoria nunca debe bloquear nada.

    /free solo lo apunta: ComfyUI descarga en diferido. Medido el 2026-09-28:
    cargar el modelo del chat justo despues, con FLUX aun en la GPU, lo
    dejaba fuera de Ollama. Por eso se espera (max. 10s) a ver la VRAM libre."""
    try:
        requests.post(f"{base_url}/free", json={"unload_models": True, "free_memory": True}, timeout=10)
        deadline = time.time() + 10
        while time.time() < deadline:
            device = requests.get(f"{base_url}/system_stats", timeout=5).json()["devices"][0]
            # torch_vram_total es solo lo de ComfyUI (vram_free cuenta tambien Ollama y demas)
            if device["torch_vram_total"] <= FREED_VRAM_BYTES:
                return
            time.sleep(0.5)
    except (requests.RequestException, ValueError, KeyError, IndexError):
        pass


def interrupt(base_url: str) -> None:
    requests.post(f"{base_url}/interrupt", timeout=10)


WARMUP_PROMPT = "warm up"  # lo usa main.py para precargar un modo; no es trabajo del usuario


def user_jobs(base_url: str) -> list[dict]:
    """Generaciones del usuario en ComfyUI: [{"id", "state": running|queued}],
    sin contar las de precarga de modelos. [] si ComfyUI no responde."""
    try:
        queue = requests.get(f"{base_url}/queue", timeout=5).json()
    except (requests.RequestException, ValueError):
        return []

    def is_user_job(item) -> bool:
        return f'"{WARMUP_PROMPT}"' not in json.dumps(item[2] if len(item) > 2 else {})

    def job(item, state: str) -> dict:
        with _owners_lock:
            return {"id": item[1], "state": state, "session_id": _conversations.get(item[1])}

    return ([job(item, "running") for item in queue.get("queue_running", []) if is_user_job(item)]
            + [job(item, "queued") for item in queue.get("queue_pending", []) if is_user_job(item)])


def user_queue(base_url: str) -> tuple[int, int]:
    """(generaciones del usuario ejecutandose, en cola)."""
    jobs = user_jobs(base_url)
    return (sum(j["state"] == "running" for j in jobs), sum(j["state"] == "queued" for j in jobs))


def stop_job(base_url: str, prompt_id: str) -> None:
    """Quita una generacion de la cola, o la corta si ya se esta ejecutando."""
    if any(j["id"] == prompt_id and j["state"] == "running" for j in user_jobs(base_url)):
        requests.post(f"{base_url}/interrupt", timeout=10)
    else:
        requests.post(f"{base_url}/queue", json={"delete": [prompt_id]}, timeout=10)


def stop_everything(base_url: str) -> None:
    """Vacia la cola y corta lo que se este generando."""
    requests.post(f"{base_url}/queue", json={"clear": True}, timeout=10)
    requests.post(f"{base_url}/interrupt", timeout=10)
