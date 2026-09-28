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


def test_router_maps_agente_to_opencode():
    client = FakeOllamaClient('{"agent": "agente", "factual": false}')
    router = Router(client, routing_model="fake-model")
    assert router.classify("crea una carpeta fotos en el escritorio")["agent"] == "opencode"


def test_explicit_agent_request_skips_the_model():
    client = FakeOllamaClient("no deberia llamarse")
    router = Router(client, routing_model="fake-model")
    for msg in ["usa un agente y crea un archivo", "Utiliza el agente para ordenar Descargas",
                "agente: renombra las fotos"]:
        assert router.classify(msg) == {"agent": "opencode", "factual": False}
    assert client.last_messages is None


def test_mentioning_agents_mid_sentence_does_not_force_the_agent():
    client = FakeOllamaClient('{"agent": "text", "factual": false}')
    router = Router(client, routing_model="fake-model")
    assert router.classify("que es un agente de IA?")["agent"] == "text"


def test_small_talk_skips_the_model():
    client = FakeOllamaClient("no deberia llamarse")
    router = Router(client, routing_model="fake-model")
    for msg in ["hola", "Hola!", "¡Buenas tardes!", "gracias chati", "vale", "¿Qué tal?".replace("é", "e"), "adios"]:
        assert router.classify(msg) == {"agent": "text", "factual": False}, msg
    assert client.last_messages is None


def test_greeting_followed_by_a_request_still_asks_the_model():
    client = FakeOllamaClient('{"agent": "image", "factual": false}')
    router = Router(client, routing_model="fake-model")
    assert router.classify("hola, dibujame un gato")["agent"] == "image"
    assert client.last_messages is not None


def test_assess_agent_task_reads_the_verdict():
    client = FakeOllamaClient('{"dificultad": "compleja", "motivo": "hay que depurar codigo"}')
    router = Router(client, routing_model="fake-model")
    assert router.assess_agent_task("arregla el bug") == {"complex": True, "reason": "hay que depurar codigo"}


def test_assess_agent_task_hints_catch_what_the_small_model_misses():
    client = FakeOllamaClient('{"dificultad": "simple", "motivo": "pocos pasos"}')
    router = Router(client, routing_model="fake-model")
    verdict = router.assess_agent_task("crea un script que cada noche copie mis documentos al disco D")
    assert verdict["complex"] is True and "se repite" in verdict["reason"]
    assert router.assess_agent_task("mueve las fotos del escritorio a imagenes")["complex"] is False


def test_assess_agent_task_never_blocks_on_garbage():
    router = Router(FakeOllamaClient("no se que decirte"), routing_model="fake-model")
    assert router.assess_agent_task("renombra el archivo") == {"complex": False, "reason": ""}
