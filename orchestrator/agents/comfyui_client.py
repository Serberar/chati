import time
import uuid

import requests


class GenerationCancelled(Exception):
    pass


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


def interrupt(base_url: str) -> None:
    requests.post(f"{base_url}/interrupt", timeout=10)
