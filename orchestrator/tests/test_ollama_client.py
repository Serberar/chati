"""Test unitario (sin Ollama real) de OllamaClient.unload() - la pieza clave
de la politica de "un solo modelo pesado en RAM a la vez" (ver ROADMAP.md,
punto 14: varios modelos CPU grandes cargados a la vez agotaban los 32GB
de RAM reales y causaban lentitud severa por intercambio a disco)."""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from agents.ollama_client import OllamaClient


def _fake_error_response(status_code=400, error_body=None, raw_text=""):
    """Simula una respuesta real de requests para un fallo - MagicMock no
    vale aqui porque .ok tiene que ser False de verdad (un MagicMock
    cualquiera evalua a True), y raise_for_status tiene que lanzar de
    verdad para probar que _raise_with_body la envuelve bien."""
    resp = requests.Response()
    resp.status_code = status_code
    if error_body is not None:
        resp._content = json.dumps(error_body).encode()
    else:
        resp._content = raw_text.encode()
    return resp


def _fake_chat_response(content="hola"):
    resp = MagicMock()
    resp.json.return_value = {"message": {"content": content}}
    return resp


def test_unload_sends_keep_alive_zero_for_the_given_model():
    client = OllamaClient("http://localhost:11434")
    with patch("agents.ollama_client.requests.post") as mock_post:
        client.unload("qwen3-coder:30b-cpu")

    mock_post.assert_called_once()
    args, kwargs = mock_post.call_args
    assert args[0] == "http://localhost:11434/api/generate"
    assert kwargs["json"] == {"model": "qwen3-coder:30b-cpu", "keep_alive": 0}


def test_unload_swallows_network_errors_without_raising():
    import requests

    client = OllamaClient("http://localhost:11434")
    with patch("agents.ollama_client.requests.post", side_effect=requests.ConnectionError("sin red")):
        client.unload("qwen3-coder:30b-cpu")  # no debe lanzar


def test_chat_omits_think_from_payload_when_not_given():
    """Por defecto (think=None) no se manda el campo 'think' a Ollama - los
    modelos que no soportan pensar no tienen por que recibirlo."""
    client = OllamaClient("http://localhost:11434")
    with patch("agents.ollama_client.requests.post", return_value=_fake_chat_response()) as mock_post:
        client.chat("qwen2.5:7b", [{"role": "user", "content": "hola"}])

    assert "think" not in mock_post.call_args.kwargs["json"]


def test_chat_includes_think_false_when_given():
    """Ver ROADMAP.md: sin esto, un modelo con modo de pensamiento (p.ej.
    qwen3-abliterated) puede gastar toda la respuesta pensando y dejar el
    contenido real vacio."""
    client = OllamaClient("http://localhost:11434")
    with patch("agents.ollama_client.requests.post", return_value=_fake_chat_response()) as mock_post:
        client.chat("qwen3-abliterated:14b-cpu", [{"role": "user", "content": "hola"}], think=False)

    assert mock_post.call_args.kwargs["json"]["think"] is False


def test_chat_stream_includes_think_false_when_given():
    client = OllamaClient("http://localhost:11434")
    fake_resp = MagicMock()
    fake_resp.iter_lines.return_value = [b'{"message": {"content": "hola"}, "done": true}']
    with patch("agents.ollama_client.requests.post", return_value=fake_resp) as mock_post:
        list(client.chat_stream("qwen3-abliterated:14b-cpu", [{"role": "user", "content": "hola"}], think=False))

    assert mock_post.call_args.kwargs["json"]["think"] is False


def test_chat_with_tools_includes_think_false_when_given():
    client = OllamaClient("http://localhost:11434")
    fake_resp = MagicMock()
    fake_resp.json.return_value = {"message": {"content": "hola"}}
    with patch("agents.ollama_client.requests.post", return_value=fake_resp) as mock_post:
        client.chat_with_tools("qwen3-abliterated:14b-cpu", [{"role": "user", "content": "hola"}], [], think=False)

    assert mock_post.call_args.kwargs["json"]["think"] is False


# --- Surfacear el cuerpo real del error de Ollama, no solo "400 Client
# Error" (bug real, ver ROADMAP.md 2026-09-25: un fallo de vision totalmente
# opaco que no dejaba diagnosticar nada de verdad).

def test_chat_raises_with_ollamas_actual_error_message():
    client = OllamaClient("http://localhost:11434")
    fake_resp = _fake_error_response(400, error_body={"error": "image bytes could not be decoded"})
    with patch("agents.ollama_client.requests.post", return_value=fake_resp):
        with pytest.raises(requests.HTTPError, match="image bytes could not be decoded"):
            client.chat("qwen2.5vl:7b-cpu", [{"role": "user", "content": "hola"}])


def test_chat_stream_raises_with_ollamas_actual_error_message():
    client = OllamaClient("http://localhost:11434")
    fake_resp = _fake_error_response(400, error_body={"error": "context length exceeded"})
    with patch("agents.ollama_client.requests.post", return_value=fake_resp):
        with pytest.raises(requests.HTTPError, match="context length exceeded"):
            list(client.chat_stream("qwen2.5vl:7b-cpu", [{"role": "user", "content": "hola"}]))


def test_raises_plain_status_error_when_body_has_no_json():
    client = OllamaClient("http://localhost:11434")
    fake_resp = _fake_error_response(500, raw_text="")
    with patch("agents.ollama_client.requests.post", return_value=fake_resp):
        with pytest.raises(requests.HTTPError):
            client.chat("qwen2.5:7b", [{"role": "user", "content": "hola"}])
