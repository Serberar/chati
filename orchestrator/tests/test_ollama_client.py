"""Test unitario (sin Ollama real) de OllamaClient.unload() - la pieza clave
de la politica de "un solo modelo pesado en RAM a la vez" (ver ROADMAP.md,
punto 14: varios modelos CPU grandes cargados a la vez agotaban los 32GB
de RAM reales y causaban lentitud severa por intercambio a disco)."""

from unittest.mock import MagicMock, patch

from agents.ollama_client import OllamaClient


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
