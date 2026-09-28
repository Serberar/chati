import json
import time
import uuid
from typing import Callable

import requests


class GenerationCancelled(Exception):
    pass


# Lo registra main.py: libera la GPU/RAM que ocupan los modelos de texto de
# Ollama antes de cada generacion. Aqui porque TODA generacion de imagen y
# video pasa por submit_and_wait - un solo punto en vez de uno por endpoint.
before_submit: Callable[[], None] | None = None

# memoria de GPU que retiene ComfyUI sin ningun modelo cargado (medido: ~60MB)
FREED_VRAM_BYTES = 512 * 1024 * 1024


def _is_still_queued(base_url: str, prompt_id: str) -> bool:
    queue = requests.get(f"{base_url}/queue", timeout=10).json()
    items = queue.get("queue_running", []) + queue.get("queue_pending", [])
    return any(item[1] == prompt_id for item in items)


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
    if before_submit is not None:
        before_submit()

    client_id = str(uuid.uuid4())
    resp = requests.post(
        f"{base_url}/prompt",
        json={"prompt": workflow, "client_id": client_id},
        timeout=30,
    )
    resp.raise_for_status()
    prompt_id = resp.json()["prompt_id"]

    deadline = time.time() + timeout
    grace_deadline = time.time() + 5  # margen inicial: no comprobar la cola nada mas enviar (posible carrera)

    while time.time() < deadline:
        hist = requests.get(f"{base_url}/history/{prompt_id}", timeout=10).json()
        if prompt_id in hist:
            status = hist[prompt_id].get("status", {})
            if status.get("status_str") == "error":
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
            return {"content": file_resp.content, "prompt_id": prompt_id}

        if time.time() > grace_deadline and not _is_still_queued(base_url, prompt_id):
            raise GenerationCancelled(f"Generacion cancelada (prompt_id={prompt_id})")

        time.sleep(1)

    raise TimeoutError(f"Generacion no termino en {timeout}s (prompt_id={prompt_id})")


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

    return ([{"id": item[1], "state": "running"} for item in queue.get("queue_running", []) if is_user_job(item)]
            + [{"id": item[1], "state": "queued"} for item in queue.get("queue_pending", []) if is_user_job(item)])


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
