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
