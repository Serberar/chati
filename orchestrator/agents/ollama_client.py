import json
from typing import Iterator

import requests

# El modelo grande unificado (qwen3-coder:30b-cpu, ~20GB) es el unico modelo
# de texto/codigo que se mantiene cargado ahora - con 32GB de RAM de sobra,
# mantenerlo en memoria mas tiempo que el default de Ollama (5 min) evita
# recargas en frio (~20s) entre mensajes espaciados durante el uso normal
# del dia a dia. Ver ROADMAP.md, punto 13.
DEFAULT_KEEP_ALIVE = "30m"


def _raise_with_body(resp: requests.Response) -> None:
    """resp.raise_for_status() por si sola solo da 'NNN Client Error: ...
    for url: ...' - sin el cuerpo real que Ollama manda explicando el
    motivo (encontrado en vivo, 2026-09-25: un 400 en vision totalmente
    opaco que no dejaba diagnosticar nada). Ollama devuelve
    {"error": "..."} en el cuerpo en los fallos - lo incluimos en el
    mensaje de la excepcion para que el error de verdad llegue hasta la
    interfaz en vez de perderse."""
    if resp.ok:
        return
    try:
        detail = resp.json().get("error")
    except (ValueError, AttributeError):
        detail = None
    if not detail:
        detail = resp.text.strip()
    try:
        resp.raise_for_status()
    except requests.HTTPError as exc:
        if detail:
            raise requests.HTTPError(f"{exc} - Ollama dice: {detail}", response=resp) from None
        raise


class OllamaClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")

    def chat(self, model: str, messages: list[dict], temperature: float = 0.7,
              keep_alive: str = DEFAULT_KEEP_ALIVE, think: bool | None = None) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature},
            "keep_alive": keep_alive,
        }
        if think is not None:
            payload["think"] = think
        resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=180)
        _raise_with_body(resp)
        return resp.json()["message"]["content"]

    def chat_stream(self, model: str, messages: list[dict], temperature: float = 0.7,
                     keep_alive: str = DEFAULT_KEEP_ALIVE, think: bool | None = None) -> Iterator[str]:
        """Igual que chat() pero devuelve los trozos de texto segun van llegando."""
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "options": {"temperature": temperature},
            "keep_alive": keep_alive,
        }
        if think is not None:
            payload["think"] = think
        resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=180, stream=True)
        _raise_with_body(resp)
        for line in resp.iter_lines():
            if not line:
                continue
            data = json.loads(line)
            content = data.get("message", {}).get("content", "")
            if content:
                yield content
            if data.get("done"):
                break

    def chat_with_tools(self, model: str, messages: list[dict], tools: list[dict],
                         temperature: float = 0.7, keep_alive: str = DEFAULT_KEEP_ALIVE,
                         think: bool | None = None) -> dict:
        """Como chat() pero con function calling nativo de Ollama. Devuelve el
        mensaje completo (dict con 'content' y, si el modelo decide usar una
        herramienta, 'tool_calls'), no solo el texto."""
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools,
            "stream": False,
            "options": {"temperature": temperature},
            "keep_alive": keep_alive,
        }
        if think is not None:
            payload["think"] = think
        resp = requests.post(f"{self.base_url}/api/chat", json=payload, timeout=180)
        _raise_with_body(resp)
        return resp.json()["message"]

    def unload(self, model: str) -> None:
        """Descarga un modelo de memoria de inmediato (keep_alive=0), sin
        generar nada - usado para la politica de "un solo modelo pesado en
        RAM a la vez" (ver ROADMAP.md, punto 14: varios modelos CPU grandes
        cargados a la vez agotaban los 32GB de RAM y causaban lentitud
        severa por intercambio a disco). Si Ollama no responde a tiempo, se
        ignora el fallo - no descargar el modelo viejo no debe bloquear la
        peticion real del usuario, en el peor caso solo se queda mas RAM
        ocupada de la cuenta."""
        try:
            requests.post(
                f"{self.base_url}/api/generate",
                json={"model": model, "keep_alive": 0},
                timeout=30,
            )
        except requests.RequestException:
            pass

    def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        resp = requests.post(
            f"{self.base_url}/api/embed",
            json={"model": model, "input": texts},
            timeout=60,
        )
        _raise_with_body(resp)
        return resp.json()["embeddings"]
