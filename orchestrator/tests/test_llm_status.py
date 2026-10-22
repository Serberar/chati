"""Que esta haciendo el modelo del agente (llm_status.py) y cada generacion
(comfyui_client.job_progress): lo que enseña la tarea en vez de "Trabajando"."""

import json

import pytest

import llm_status
from agents import comfyui_client


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(llm_status, "_active", {})
    monkeypatch.setattr(llm_status, "_unanswered", {"count": 0})
    monkeypatch.setattr(llm_status, "ollama_cpu", lambda: 800.0)
    monkeypatch.setattr(llm_status, "gpu_percent", lambda: 0)
    monkeypatch.setattr(llm_status, "_loaded_models", lambda: {"qwen3-coder:30b-mixto", "qwen3:8b"})


def _chunk(delta: dict) -> bytes:
    return b"data: " + json.dumps({"choices": [{"delta": delta}]}).encode()


def test_reading_then_writing_then_preparing_a_tool():
    rid = llm_status.start(b"x" * 35000, "qwen3-coder:30b-mixto")
    now = llm_status.snapshot()
    assert now["fase"] == "leyendo" and "~10.000 tokens" in now["texto"]

    llm_status.output(rid, _chunk({"content": "Voy"}))
    llm_status.output(rid, _chunk({"content": " a"}))
    assert llm_status.snapshot()["fase"] == "escribiendo" and "2 tokens" in llm_status.snapshot()["texto"]

    call = {"function": {"name": "edit", "arguments": json.dumps({"filePath": "main.py", "oldString": "a"})}}
    llm_status.output(rid, _chunk({"tool_calls": [call]}))
    assert llm_status.snapshot()["texto"] == "Preparando edit: main.py"

    llm_status.finish(rid)
    assert llm_status.snapshot() is None and llm_status.retries() == 0


def test_requests_cut_without_an_answer_are_counted_as_a_retry_loop():
    """El bucle del 2026-10-16: OpenCode cortaba antes de la primera palabra y
    volvia a pedir lo mismo desde cero, una y otra vez."""
    for _ in range(3):
        llm_status.finish(llm_status.start(b"{}", "qwen3-coder:30b-mixto"))
    assert llm_status.retries() == 3
    llm_status.start(b"{}", "qwen3-coder:30b-mixto")
    assert "intento 4" in llm_status.snapshot()["texto"]

    rid = llm_status.start(b"{}", "qwen3-coder:30b-mixto")
    llm_status.output(rid, _chunk({"content": "ya"}))
    assert llm_status.retries() == 0  # en cuanto contesta, deja de contar


def test_reading_with_ollama_idle_says_so_but_the_gpu_counts_as_working(monkeypatch):
    """qwen3:8b lee entero en la tarjeta grafica con el procesador casi parado (medido)."""
    monkeypatch.setattr(llm_status, "ollama_cpu", lambda: 0.0)
    llm_status.start(b"{}", "qwen3:8b")
    assert "ni el procesador ni la tarjeta gráfica" in llm_status.snapshot()["texto"]
    monkeypatch.setattr(llm_status, "gpu_percent", lambda: 95)
    assert "tarjeta" not in llm_status.snapshot()["texto"]


def test_a_model_not_yet_in_memory_is_loading(monkeypatch):
    monkeypatch.setattr(llm_status, "_loaded_models", lambda: set())
    llm_status.start(b"{}", "qwen3-coder:30b-mixto")
    assert llm_status.snapshot()["texto"].startswith("Cargando qwen3-coder:30b-mixto en memoria")


def test_comfyui_progress_names_the_node_and_the_step(monkeypatch):
    monkeypatch.setattr(comfyui_client, "_progress", {"p1": {
        "nodes": {"3": "KSampler", "4": "UNETLoader", "8": "VAEDecode"},
        "node": None, "value": 0, "max": 0, "since": 0}})
    assert comfyui_client.job_progress("p1") is None  # aun no ha empezado ningun nodo

    comfyui_client._on_comfy_event({"type": "executing", "data": {"node": "4", "prompt_id": "p1"}})
    assert comfyui_client.job_progress("p1")["texto"] == "Cargando el modelo"

    comfyui_client._on_comfy_event({"type": "executing", "data": {"node": "3", "prompt_id": "p1"}})
    assert comfyui_client.job_progress("p1")["texto"] == "Generando: pasando el modelo a la tarjeta gráfica"
    comfyui_client._on_comfy_event({"type": "progress", "data": {"node": "3", "value": 12, "max": 20,
                                                                 "prompt_id": "p1"}})
    assert comfyui_client.job_progress("p1")["texto"] == "Generando: paso 12 de 20"

    comfyui_client._on_comfy_event({"type": "executing", "data": {"node": "8", "prompt_id": "p1"}})
    assert comfyui_client.job_progress("p1")["texto"] == "Pasando el resultado a imagen"

    # de otro cliente o ya olvidada: no rompe nada
    comfyui_client._on_comfy_event({"type": "progress", "data": {"prompt_id": "otra"}})
