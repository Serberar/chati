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
