"""Pasarela entre OpenCode y Ollama (API compatible con OpenAI) que limpia
las llamadas a herramientas antes de que lleguen a OpenCode.

Por que: qwen2.5:7b (el modelo del agente rapido) rellena los parametros
opcionales de las herramientas con null ("workdir": null, "timeout": null).
OpenCode los rechaza ("SchemaError: Expected string | undefined, got null")
y el modelo se queda reintentando lo mismo - medido el 2026-09-28, pedirselo
en las instrucciones no lo corrige. Quitar los null aqui si: un parametro
opcional ausente es exactamente lo que el modelo queria decir.

Ollama manda cada llamada a herramienta entera en un solo trozo del stream
(comprobado), asi que basta con reescribir esa linea."""

import json


def strip_null_arguments(arguments: str) -> str:
    """Quita las claves con valor null del JSON de argumentos. Si no es un
    objeto JSON valido, se devuelve tal cual (que OpenCode reporte el error)."""
    try:
        data = json.loads(arguments)
    except (TypeError, ValueError):
        return arguments
    if not isinstance(data, dict):
        return arguments
    return json.dumps({k: v for k, v in data.items() if v is not None}, ensure_ascii=False)


def _clean_tool_calls(tool_calls: list) -> bool:
    changed = False
    for call in tool_calls or []:
        fn = call.get("function") or {}
        if isinstance(fn.get("arguments"), str):
            cleaned = strip_null_arguments(fn["arguments"])
            if cleaned != fn["arguments"]:
                fn["arguments"] = cleaned
                changed = True
    return changed


def disable_thinking(body: bytes, models: set[str]) -> bytes:
    """Peticion a un modelo que "piensa en voz alta" (qwen3): se le pide que no
    lo haga. Medido el 2026-09-28 con qwen3:8b: 12,9s -> 0,6s por respuesta con
    la misma respuesta; en el banco de pruebas del agente, pensando se pasaba
    de 3 minutos en tareas de editar o mover. OpenCode no manda este ajuste.
    "/no_think" en el mensaje no sirve (comprobado), reasoning_effort si."""
    if not models or not body:
        return body
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if not isinstance(data, dict) or data.get("model") not in models or "reasoning_effort" in data:
        return body
    data["reasoning_effort"] = "none"
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def clean_completion(body: dict) -> dict:
    """Respuesta sin streaming: choices[].message.tool_calls."""
    for choice in body.get("choices", []):
        _clean_tool_calls((choice.get("message") or {}).get("tool_calls"))
    return body


def clean_stream_line(line: bytes) -> bytes:
    """Una linea "data: {...}" del stream: choices[].delta.tool_calls. Las
    demas lineas (texto, [DONE], vacias) pasan intactas."""
    if not line.startswith(b"data: {") or b"tool_calls" not in line:
        return line
    try:
        chunk = json.loads(line[6:])
    except ValueError:
        return line
    changed = False
    for choice in chunk.get("choices", []):
        changed |= _clean_tool_calls((choice.get("delta") or {}).get("tool_calls"))
    if not changed:
        return line
    return b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8")
