from router import Router


class FakeOllamaClient:
    """Stub sin red: devuelve lo que le digamos, para probar el parseo de
    router.py de forma aislada y rapida (sin Ollama real de por medio)."""

    def __init__(self, canned_response: str):
        self.canned_response = canned_response
        self.last_messages = None

    def chat(self, model, messages, temperature=0.7):
        self.last_messages = messages
        return self.canned_response


def test_router_parses_valid_json():
    client = FakeOllamaClient('{"agent": "code", "factual": false}')
    router = Router(client, routing_model="fake-model")
    result = router.classify("escribe una funcion en python")
    assert result == {"agent": "code", "factual": False}


def test_router_parses_json_with_surrounding_commentary():
    # los modelos a veces envuelven el JSON en texto aunque se les pida que no
    client = FakeOllamaClient('Claro, aqui tienes:\n{"agent": "image", "factual": true}\nEspero que ayude.')
    router = Router(client, routing_model="fake-model")
    result = router.classify("dibuja un gato")
    assert result == {"agent": "image", "factual": True}


def test_router_falls_back_to_text_on_invalid_agent():
    client = FakeOllamaClient('{"agent": "no_existe", "factual": false}')
    router = Router(client, routing_model="fake-model")
    result = router.classify("cualquier cosa")
    assert result["agent"] == "text"


def test_router_falls_back_to_text_on_malformed_json():
    client = FakeOllamaClient("esto no es json en absoluto")
    router = Router(client, routing_model="fake-model")
    result = router.classify("cualquier cosa")
    assert result == {"agent": "text", "factual": False}


def test_router_defaults_factual_to_false_when_missing():
    client = FakeOllamaClient('{"agent": "text"}')
    router = Router(client, routing_model="fake-model")
    result = router.classify("hola")
    assert result["factual"] is False
