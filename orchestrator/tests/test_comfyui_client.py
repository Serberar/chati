"""
Test unitario (sin ComfyUI real) de la parte que peor se entiende del modulo:
distinguir un fallo real de una cancelacion, a partir de la forma exacta en
la que ComfyUI reporta cada caso en /history (comprobado contra el servidor
real, ver notas en comfyui_client.py)."""

from unittest.mock import patch

import pytest

from agents.comfyui_client import GenerationCancelled, submit_and_wait


def _mock_responses(history_by_call, queue_response=None):
    """Devuelve una funcion que simula requests.get/.post segun la url,
    entregando un historial distinto en cada llamada sucesiva a /history."""
    call_state = {"n": 0}

    class FakeResp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

        def raise_for_status(self):
            pass

    def fake_request(method, url, **kwargs):
        if url.endswith("/prompt"):
            return FakeResp({"prompt_id": "fake-id"})
        if "/history/" in url:
            idx = min(call_state["n"], len(history_by_call) - 1)
            call_state["n"] += 1
            return FakeResp(history_by_call[idx])
        if url.endswith("/queue"):
            return FakeResp(queue_response or {"queue_running": [], "queue_pending": []})
        raise AssertionError(f"URL no mockeada: {url}")

    return fake_request


@patch("agents.comfyui_client.requests.post")
@patch("agents.comfyui_client.requests.get")
def test_interrupted_execution_raises_generation_cancelled(mock_get, mock_post):
    interrupted_history = {
        "fake-id": {
            "status": {
                "status_str": "error",
                "messages": [
                    ["execution_start", {}],
                    ["execution_interrupted", {"node_id": "sampler"}],
                ],
            },
            "outputs": {},
        }
    }
    fake = _mock_responses([{}, interrupted_history])
    mock_get.side_effect = lambda url, **kw: fake("GET", url, **kw)
    mock_post.side_effect = lambda url, **kw: fake("POST", url, **kw)

    with pytest.raises(GenerationCancelled):
        submit_and_wait("http://fake", {}, "save_image", "images", timeout=30)


@patch("agents.comfyui_client.requests.post")
@patch("agents.comfyui_client.requests.get")
def test_real_failure_raises_runtime_error_not_cancelled(mock_get, mock_post):
    real_failure_history = {
        "fake-id": {
            "status": {
                "status_str": "error",
                "messages": [["execution_error", {"exception_message": "algo raro paso"}]],
            },
            "outputs": {},
        }
    }
    fake = _mock_responses([{}, real_failure_history])
    mock_get.side_effect = lambda url, **kw: fake("GET", url, **kw)
    mock_post.side_effect = lambda url, **kw: fake("POST", url, **kw)

    with pytest.raises(RuntimeError) as exc_info:
        submit_and_wait("http://fake", {}, "save_image", "images", timeout=30)
    assert not isinstance(exc_info.value, GenerationCancelled)


def test_before_submit_hook_runs_before_sending_the_workflow(monkeypatch):
    from unittest.mock import MagicMock
    from agents import comfyui_client

    calls = []
    monkeypatch.setattr(comfyui_client, "before_submit", lambda: calls.append("liberar"))

    def fake_post(url, json, timeout):
        calls.append("enviar")
        raise RuntimeError("parar aqui")

    monkeypatch.setattr(comfyui_client.requests, "post", fake_post)
    try:
        comfyui_client.submit_and_wait("http://x", {}, "9", "images", timeout=1)
    except RuntimeError:
        pass
    assert calls == ["liberar", "enviar"]


@pytest.fixture(autouse=True)
def _sin_liberar_memoria(monkeypatch):
    # main.py registra before_submit al importarse (libera Ollama de verdad);
    # estos tests simulan la red y no deben ejecutarlo
    from agents import comfyui_client
    monkeypatch.setattr(comfyui_client, "before_submit", None)


def test_free_memory_waits_until_comfyui_really_released_the_gpu(monkeypatch):
    from agents import comfyui_client

    reported = [6 * 2**30, 3 * 2**30, 60 * 2**20]  # descarga en diferido
    gets = []

    class R:
        def __init__(self, data): self.data = data
        def json(self): return self.data

    monkeypatch.setattr(comfyui_client.requests, "post", lambda *a, **k: R({}))
    monkeypatch.setattr(comfyui_client.requests, "get", lambda *a, **k: (
        gets.append(1), R({"devices": [{"torch_vram_total": reported[min(len(gets) - 1, 2)]}]}))[1])
    monkeypatch.setattr(comfyui_client.time, "sleep", lambda s: None)

    comfyui_client.free_memory("http://x")

    assert len(gets) == 3  # siguio mirando hasta verla libre, y paro ahi


def test_free_memory_never_raises_when_comfyui_is_down(monkeypatch):
    from agents import comfyui_client

    def boom(*a, **k):
        raise comfyui_client.requests.ConnectionError("caido")

    monkeypatch.setattr(comfyui_client.requests, "post", boom)
    comfyui_client.free_memory("http://x")


def test_user_queue_ignores_model_warmups(monkeypatch):
    from agents import comfyui_client

    class R:
        def json(self):
            return {
                "queue_running": [[1, "a", {"6": {"inputs": {"text": comfyui_client.WARMUP_PROMPT}}}]],
                "queue_pending": [[2, "b", {"6": {"inputs": {"text": "un gato"}}}],
                                  [3, "c", {"6": {"inputs": {"text": "un perro"}}}]],
            }

    monkeypatch.setattr(comfyui_client.requests, "get", lambda *a, **k: R())
    assert comfyui_client.user_queue("http://x") == (0, 2)
