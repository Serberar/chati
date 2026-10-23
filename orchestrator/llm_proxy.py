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
import re


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


_FUNCTION_RE = re.compile(r"<function=([\w.-]+)>(.*?)</function>", re.DOTALL)
_PARAM_RE = re.compile(r"<parameter=([\w.-]+)>\s*(.*?)\s*</parameter>", re.DOTALL)
_MARK = "<function="


def _argument(value: str):
    """Los parametros llegan como texto: numeros y si/no a su tipo, como en JSON."""
    try:
        parsed = json.loads(value)
    except ValueError:
        return value
    return parsed if isinstance(parsed, (int, float, bool)) else value


class TextToolCalls:
    """qwen3-coder a veces escribe la llamada a una herramienta como texto
    (<function=...><parameter=...>, sin el <tool_call> de apertura) y Ollama la
    deja como texto: OpenCode lo tomaba como respuesta final y la tarea se
    quedaba parada sin hacer nada (banco del 2026-10-23). Desde <function= se
    retiene el texto y, al acabar, se manda como una llamada de verdad."""

    def __init__(self):
        self.text = ""  # todo el texto visto, para notar <function= aunque llegue partido
        self.held = None  # desde <function=; None mientras no aparezca
        self.last = None  # el ultimo trozo, para copiar id/modelo en los nuevos

    def feed(self, line: bytes) -> list[bytes]:
        if line.startswith(b"data: [DONE]"):
            return self.finish() + [line]
        if not line.startswith(b"data: {"):
            return [line]
        try:
            chunk = json.loads(line[6:])
        except ValueError:
            return [line]
        self.last = chunk
        choice = (chunk.get("choices") or [{}])[0]
        content = (choice.get("delta") or {}).get("content") or ""
        if self.held is not None:
            self.held += content
            return []
        start = len(self.text)
        self.text += content
        mark = self.text.find(_MARK)
        if mark == -1:
            return [line]
        self.held = self.text[mark:]
        before = self.text[start:mark] if mark >= start else ""
        if not before:
            return []
        choice["delta"]["content"] = before
        choice["finish_reason"] = None
        return [b"data: " + json.dumps(chunk, ensure_ascii=False).encode("utf-8")]

    def finish(self) -> list[bytes]:
        if self.held is None or self.last is None:
            return []
        held, self.held = self.held, None
        match = _FUNCTION_RE.search(held)
        base = {k: v for k, v in self.last.items() if k != "choices"}
        if match:
            args = {k: _argument(v) for k, v in _PARAM_RE.findall(match.group(2))}
            call = {"index": 0, "id": f"call_{abs(hash(held)) % 10**8}", "type": "function",
                    "function": {"name": match.group(1), "arguments": json.dumps(args, ensure_ascii=False)}}
            chunks = [{"delta": {"tool_calls": [call]}, "finish_reason": None},
                      {"delta": {}, "finish_reason": "tool_calls"}]
        else:  # no era una llamada: el texto tal cual
            chunks = [{"delta": {"content": held}, "finish_reason": None}, {"delta": {}, "finish_reason": "stop"}]
        return [b"data: " + json.dumps({**base, "choices": [{"index": 0, **c}]}, ensure_ascii=False).encode("utf-8")
                for c in chunks]


MIXED_SUFFIX = "-mixto"


def cpu_when_gpu_busy(body: bytes, gpu_busy: bool) -> bytes:
    """El modelo del agente de codigo en version "-mixto" (12 capas en la
    tarjeta grafica: lee 7 veces mas rapido que solo en el procesador, medido
    el 2026-10-13) pasa a su version "-cpu" mientras ComfyUI genera una
    imagen: con los dos en los 8 GB, la edicion de fotos se atascaba."""
    if not gpu_busy or not body:
        return body
    try:
        data = json.loads(body)
    except ValueError:
        return body
    model = data.get("model") if isinstance(data, dict) else None
    if not isinstance(model, str) or not model.endswith(MIXED_SUFFIX):
        return body
    data["model"] = model.removesuffix(MIXED_SUFFIX) + "-cpu"
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def model_of(body: bytes) -> str | None:
    try:
        data = json.loads(body) if body else None
    except ValueError:
        return None
    model = data.get("model") if isinstance(data, dict) else None
    return model if isinstance(model, str) else None
