import json

import llm_proxy


def test_strip_null_arguments_removes_only_nulls():
    out = json.loads(llm_proxy.strip_null_arguments('{"command": "dir", "workdir": null, "timeout": null, "n": 0}'))
    assert out == {"command": "dir", "n": 0}


def test_strip_null_arguments_leaves_garbage_alone():
    assert llm_proxy.strip_null_arguments("no es json") == "no es json"
    assert llm_proxy.strip_null_arguments("[1, null]") == "[1, null]"


def test_clean_stream_line_rewrites_tool_calls_and_passes_everything_else():
    chunk = {"choices": [{"delta": {"tool_calls": [{"function": {"name": "bash",
             "arguments": '{"command": "Get-ChildItem", "workdir": null}'}}]}}]}
    cleaned = llm_proxy.clean_stream_line(b"data: " + json.dumps(chunk).encode())
    args = json.loads(json.loads(cleaned[6:])["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"])
    assert args == {"command": "Get-ChildItem"}
    for line in (b"data: [DONE]", b'data: {"choices": [{"delta": {"content": "hola"}}]}', b""):
        assert llm_proxy.clean_stream_line(line) == line


def test_clean_completion_non_streaming():
    body = {"choices": [{"message": {"tool_calls": [{"function": {"arguments": '{"a": 1, "b": null}'}}]}}]}
    assert json.loads(llm_proxy.clean_completion(body)["choices"][0]["message"]["tool_calls"][0]["function"]["arguments"]) == {"a": 1}


def test_disable_thinking_only_for_listed_models_and_respects_explicit_values():
    body = json.dumps({"model": "qwen3:8b", "messages": []}).encode()
    assert json.loads(llm_proxy.disable_thinking(body, {"qwen3:8b"}))["reasoning_effort"] == "none"
    assert llm_proxy.disable_thinking(body, {"otro"}) == body
    explicit = json.dumps({"model": "qwen3:8b", "reasoning_effort": "high"}).encode()
    assert llm_proxy.disable_thinking(explicit, {"qwen3:8b"}) == explicit
    assert llm_proxy.disable_thinking(b"", {"qwen3:8b"}) == b""


def _chunk(content=None, finish=None):
    delta = {"content": content} if content is not None else {}
    return b"data: " + json.dumps({"id": "c1", "model": "qwen3-coder:30b-mixto",
                                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}).encode()


def _run(lines):
    fixer = llm_proxy.TextToolCalls()
    out = [o for line in lines for o in fixer.feed(line)] + fixer.finish()
    return [json.loads(o[6:]) for o in out if o.startswith(b"data: {")], out


def test_a_tool_call_written_as_text_becomes_a_real_tool_call():
    """Lo que escribio qwen3-coder en el banco del 2026-10-23: OpenCode lo tomo
    como respuesta final y la tarea se quedo parada."""
    text = ("Primero, voy a probarlo.\n\n<function=chati_probar_programa>\n<parameter=ruta>\n"
            "C:/Users/S B/Desktop/pruebas/facturas/calcular_total.py\n</parameter>\n<parameter=segundos>\n8\n"
            "</parameter>\n</function>\n</tool_call>")
    pieces = [text[i:i + 7] for i in range(0, len(text), 7)]  # "<function=" llega partido
    chunks, raw = _run([_chunk(p) for p in pieces] + [_chunk("", "stop"), b"data: [DONE]"])
    said = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert said.startswith("Primero, voy a probarlo.") and "parameter" not in said
    calls = [c["choices"][0]["delta"]["tool_calls"][0] for c in chunks if c["choices"][0]["delta"].get("tool_calls")]
    assert calls[0]["function"]["name"] == "chati_probar_programa"
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "ruta": "C:/Users/S B/Desktop/pruebas/facturas/calcular_total.py", "segundos": 8}
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert raw[-1] == b"data: [DONE]"


def test_normal_answers_and_text_that_only_looks_like_a_call_pass_unchanged():
    lines = [_chunk("Hola"), _chunk(", ya esta."), _chunk("", "stop"), b"data: [DONE]"]
    assert _run(lines)[1] == lines
    chunks, _ = _run([_chunk("usa <function=x> asi"), _chunk("", "stop"), b"data: [DONE]"])
    said = "".join(c["choices"][0]["delta"].get("content") or "" for c in chunks)
    assert said == "usa <function=x> asi" and chunks[-1]["choices"][0]["finish_reason"] == "stop"
