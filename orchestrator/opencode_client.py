"""Cliente minimo para la API HTTP de OpenCode (el agente de codigo local,
ver C:\\AI\\AGENTS.md). Se usa para delegar tareas que necesitan tocar
archivos de verdad desde el chat normal, sin esperar la respuesta - medido
en vivo, incluso un "di hola" tarda 3-5 minutos a traves de OpenCode (manda
su set completo de herramientas + las instrucciones de AGENTS.md, mucho mas
grande que el prompt del chat normal, y el modelo corre en CPU puro). Por
eso esto es deliberadamente "dispara y olvida": se crea la sesion, se manda
la tarea de forma asincrona, y se devuelve el enlace para seguirla en la
propia interfaz de OpenCode - nunca se espera aqui a que termine."""

import requests

TIMEOUT = 15  # solo para crear la sesion y mandar la tarea, no para que termine


def create_session(base_url: str) -> str:
    resp = requests.post(f"{base_url}/session", json={}, timeout=TIMEOUT)
    resp.raise_for_status()
    return resp.json()["id"]


def send_prompt_async(base_url: str, session_id: str, text: str) -> None:
    resp = requests.post(
        f"{base_url}/session/{session_id}/prompt_async",
        json={"parts": [{"type": "text", "text": text}]},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()


def delegate(base_url: str, web_url: str, task: str) -> str:
    """Crea una sesion nueva en OpenCode, le manda la tarea sin esperar
    respuesta, y devuelve el texto a mostrar al usuario con el enlace para
    seguirla. No lanza si OpenCode no esta corriendo - lo reporta como texto,
    igual que el resto de herramientas."""
    try:
        session_id = create_session(base_url)
        send_prompt_async(base_url, session_id, task)
    except requests.RequestException as exc:
        return f"No se pudo contactar con el agente de codigo (¿esta arrancado?): {exc}"
    return (
        f"Tarea enviada al agente de codigo (sesion {session_id}). Puede tardar "
        f"varios minutos en completarse - sigue el progreso y aprueba los cambios "
        f"que pida en {web_url}."
    )
