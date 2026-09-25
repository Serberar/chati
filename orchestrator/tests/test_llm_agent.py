import base64
import io

from PIL import Image

from agents.llm_agent import LLMAgent, _parse_fallback_tool_call


def _real_image_base64(fmt="PNG") -> str:
    """Imagen valida minuscula (1x1) en base64 - respond_with_image_stream
    ahora decodifica/normaliza de verdad la imagen (ver InvalidImageError),
    asi que las pruebas no pueden usar una cadena falsa como antes."""
    buf = io.BytesIO()
    Image.new("RGB", (1, 1), color=(200, 50, 50)).save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode()


class FakeOllamaClient:
    """Stub sin red para probar el bucle de herramientas de LLMAgent de forma
    aislada y determinista (sin Ollama real de por medio). tool_responses es
    la cola de mensajes que devolvera chat_with_tools, en orden."""

    def __init__(self, tool_responses=None, stream_chunks=None, final_chat_response=None):
        self.tool_responses = list(tool_responses or [])
        self.stream_chunks = list(stream_chunks or [])
        self.final_chat_response = final_chat_response
        self.chat_with_tools_calls: list[list[dict]] = []
        self.chat_stream_calls: list[list[dict]] = []
        self.chat_calls: list[list[dict]] = []
        self.models_used: list[str] = []
        self.think_values_used: list[object] = []

    def chat_with_tools(self, model, messages, tools, temperature=0.7, think=None):
        self.chat_with_tools_calls.append(list(messages))
        self.models_used.append(model)
        self.think_values_used.append(think)
        return self.tool_responses.pop(0)

    def chat_stream(self, model, messages, temperature=0.7, think=None):
        self.chat_stream_calls.append(list(messages))
        self.models_used.append(model)
        self.think_values_used.append(think)
        yield from self.stream_chunks

    def chat(self, model, messages, temperature=0.7, think=None):
        self.chat_calls.append(list(messages))
        self.models_used.append(model)
        self.think_values_used.append(think)
        return self.final_chat_response


def _no_tool_call_allowed(name, arguments):
    raise AssertionError(f"no deberia haberse llamado a ninguna herramienta, se llamo a {name!r}")


def test_respond_with_tools_returns_content_directly_when_no_tool_needed():
    client = FakeOllamaClient(tool_responses=[{"role": "assistant", "content": "Hola, como estas?"}])
    agent = LLMAgent("text", "fake-model", client)

    answer, evidence = agent.respond_with_tools("hola", None, [], _no_tool_call_allowed)

    assert answer == "Hola, como estas?"
    assert evidence == []


def test_respond_with_tools_executes_tool_and_uses_result():
    client = FakeOllamaClient(tool_responses=[
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "fecha_actual", "arguments": {}}}
        ]},
        {"role": "assistant", "content": "Hoy es miercoles."},
    ])
    calls = []

    def executor(name, arguments):
        calls.append((name, arguments))
        return "Hoy es miercoles, 23 de septiembre de 2026.", [{"source": "sistema:fecha_actual", "text": "..."}]

    agent = LLMAgent("text", "fake-model", client)
    answer, evidence = agent.respond_with_tools("que dia es hoy", None, [], executor)

    assert answer == "Hoy es miercoles."
    assert calls == [("fecha_actual", {})]
    assert evidence == [{"source": "sistema:fecha_actual", "text": "..."}]
    # el resultado de la herramienta se paso de vuelta al modelo antes de la ronda final
    second_call_messages = client.chat_with_tools_calls[1]
    assert second_call_messages[-1] == {
        "role": "tool", "name": "fecha_actual", "content": "Hoy es miercoles, 23 de septiembre de 2026.",
    }


def test_respond_with_tools_forces_final_answer_after_max_iterations():
    tool_call_msg = {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "fecha_actual", "arguments": {}}}
    ]}
    client = FakeOllamaClient(
        tool_responses=[tool_call_msg, tool_call_msg],
        final_chat_response="Respuesta forzada sin mas herramientas.",
    )

    def executor(name, arguments):
        return "resultado", None

    agent = LLMAgent("text", "fake-model", client)
    answer, evidence = agent.respond_with_tools("pregunta", None, [], executor, max_iterations=2)

    assert answer == "Respuesta forzada sin mas herramientas."
    assert evidence == []
    assert len(client.chat_calls) == 1


def test_respond_with_tools_stream_no_tool_needed_still_streams_final_answer():
    client = FakeOllamaClient(
        tool_responses=[{"role": "assistant", "content": "no hace falta herramienta"}],
        stream_chunks=["Hola", " mundo"],
    )
    agent = LLMAgent("text", "fake-model", client)
    evidence: list[dict] = []

    chunks = list(agent.respond_with_tools_stream("hola", None, [], _no_tool_call_allowed, evidence))

    assert chunks == ["Hola", " mundo"]
    assert evidence == []


def test_respond_with_tools_stream_executes_tool_then_streams_final_answer():
    client = FakeOllamaClient(
        tool_responses=[
            {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "buscar_en_memoria", "arguments": {"consulta": "vacaciones"}}}
            ]},
            # segunda ronda: el modelo ya no pide mas herramientas -> se pasa a la respuesta final en streaming
            {"role": "assistant", "content": "listo"},
        ],
        stream_chunks=["Segun ", "tu memoria..."],
    )

    def executor(name, arguments):
        assert name == "buscar_en_memoria"
        assert arguments == {"consulta": "vacaciones"}
        return "texto encontrado", [{"source": "doc.pdf", "text": "texto encontrado"}]

    agent = LLMAgent("text", "fake-model", client)
    evidence: list[dict] = []

    chunks = list(agent.respond_with_tools_stream(
        "que dije de mis vacaciones", None, [], executor, evidence))

    assert chunks == ["Segun ", "tu memoria..."]
    assert evidence == [{"source": "doc.pdf", "text": "texto encontrado"}]


def test_parse_fallback_tool_call_reads_qwen3_coder_native_format():
    # reproduccion exacta de lo visto en vivo con qwen3-coder:30b-cpu cuando
    # el argumento es codigo multi-linea (ver comentario en llm_agent.py)
    content = (
        "Voy a calcular 8347 multiplicado por 219 usando una herramienta de "
        "calculo para obtener el resultado exacto.\n\n"
        "<function=ejecutar_python>\n<parameter=codigo>\n"
        "resultado = 8347 * 219\nprint(resultado)\n"
        "</parameter>\n</function>\n</tool_call>"
    )

    parsed = _parse_fallback_tool_call(content)

    assert parsed["function"]["name"] == "ejecutar_python"
    assert parsed["function"]["arguments"]["codigo"] == "resultado = 8347 * 219\nprint(resultado)"


def test_parse_fallback_tool_call_returns_none_for_plain_text():
    assert _parse_fallback_tool_call("Hola, como estas?") is None


def test_respond_with_tools_recovers_from_malformed_tool_call_text():
    client = FakeOllamaClient(tool_responses=[
        {"role": "assistant", "content": "<function=fecha_actual>\n</function>"},
        {"role": "assistant", "content": "Hoy es miercoles."},
    ])
    calls = []

    def executor(name, arguments):
        calls.append((name, arguments))
        return "Hoy es miercoles.", [{"source": "sistema:fecha_actual", "text": "..."}]

    agent = LLMAgent("text", "fake-model", client)
    answer, evidence = agent.respond_with_tools("que dia es hoy", None, [], executor)

    assert answer == "Hoy es miercoles."
    assert calls == [("fecha_actual", {})]
    assert evidence == [{"source": "sistema:fecha_actual", "text": "..."}]


def test_respond_with_tools_stream_recovers_from_malformed_tool_call_text():
    client = FakeOllamaClient(
        tool_responses=[
            {"role": "assistant", "content": (
                "<function=ejecutar_python>\n<parameter=codigo>\nprint(1827993)\n"
                "</parameter>\n</function>"
            )},
            {"role": "assistant", "content": "listo"},
        ],
        stream_chunks=["1827993"],
    )

    def executor(name, arguments):
        assert name == "ejecutar_python"
        assert arguments["codigo"] == "print(1827993)"
        return "1827993", [{"source": "sistema:ejecutar_python", "text": "1827993"}]

    agent = LLMAgent("text", "fake-model", client)
    evidence: list[dict] = []

    chunks = list(agent.respond_with_tools_stream("cuanto es 8347*219", None, [], executor, evidence))

    assert chunks == ["1827993"]
    assert evidence == [{"source": "sistema:ejecutar_python", "text": "1827993"}]


def test_respond_with_tools_uses_default_model_when_no_override():
    client = FakeOllamaClient(tool_responses=[{"role": "assistant", "content": "hola"}])
    agent = LLMAgent("text", "modelo-por-defecto", client)

    agent.respond_with_tools("hola", None, [], _no_tool_call_allowed)

    assert client.models_used == ["modelo-por-defecto"]


def test_respond_with_tools_uses_profile_override_when_given():
    client = FakeOllamaClient(tool_responses=[{"role": "assistant", "content": "hola"}])
    agent = LLMAgent("text", "modelo-por-defecto", client)

    agent.respond_with_tools("hola", None, [], _no_tool_call_allowed, model="modelo-rapido")

    assert client.models_used == ["modelo-rapido"]


def test_respond_with_tools_stream_uses_profile_override_when_given():
    client = FakeOllamaClient(
        tool_responses=[{"role": "assistant", "content": "no hace falta herramienta"}],
        stream_chunks=["hola"],
    )
    agent = LLMAgent("text", "modelo-por-defecto", client)

    list(agent.respond_with_tools_stream("hola", None, [], _no_tool_call_allowed, [], model="modelo-seguridad"))

    assert client.models_used == ["modelo-seguridad", "modelo-seguridad"]


def test_respond_with_tools_passes_think_false_through_to_the_client():
    client = FakeOllamaClient(tool_responses=[{"role": "assistant", "content": "hola"}])
    agent = LLMAgent("text", "modelo-por-defecto", client)

    agent.respond_with_tools("hola", None, [], _no_tool_call_allowed, think=False)

    assert client.think_values_used == [False]


def test_respond_with_tools_think_defaults_to_none_when_not_given():
    client = FakeOllamaClient(tool_responses=[{"role": "assistant", "content": "hola"}])
    agent = LLMAgent("text", "modelo-por-defecto", client)

    agent.respond_with_tools("hola", None, [], _no_tool_call_allowed)

    assert client.think_values_used == [None]


def test_respond_with_tools_stream_passes_think_false_through_to_the_client():
    client = FakeOllamaClient(
        tool_responses=[{"role": "assistant", "content": "no hace falta herramienta"}],
        stream_chunks=["hola"],
    )
    agent = LLMAgent("text", "modelo-por-defecto", client)

    list(agent.respond_with_tools_stream("hola", None, [], _no_tool_call_allowed, [], think=False))

    # una llamada de decision (chat_with_tools) + una de streaming (chat_stream), las dos con think=False
    assert client.think_values_used == [False, False]


def test_respond_with_image_stream_sends_image_on_user_message():
    client = FakeOllamaClient(stream_chunks=["Veo ", "un circulo rojo."])
    agent = LLMAgent("vision", "modelo-vision", client)

    chunks = list(agent.respond_with_image_stream("que ves?", _real_image_base64()))

    assert chunks == ["Veo ", "un circulo rojo."]
    sent_messages = client.chat_stream_calls[0]
    user_msg = [m for m in sent_messages if m["role"] == "user"][0]
    # la imagen se re-codifica siempre a JPEG (ver _normalize_image_for_vision) -
    # no sera un byte a byte igual a la entrada, pero debe seguir siendo una
    # imagen valida y no vacia
    assert len(user_msg["images"]) == 1
    assert Image.open(io.BytesIO(base64.b64decode(user_msg["images"][0]))).format == "JPEG"
    assert user_msg["content"] == "que ves?"
    assert client.models_used == ["modelo-vision"]


def test_respond_with_image_stream_uses_model_override():
    client = FakeOllamaClient(stream_chunks=["ok"])
    agent = LLMAgent("vision", "modelo-por-defecto", client)

    list(agent.respond_with_image_stream("hola", _real_image_base64(), model="otro-modelo-vision"))

    assert client.models_used == ["otro-modelo-vision"]


def test_respond_with_image_stream_defaults_message_when_empty():
    client = FakeOllamaClient(stream_chunks=["ok"])
    agent = LLMAgent("vision", "modelo-vision", client)

    list(agent.respond_with_image_stream("", _real_image_base64()))

    user_msg = [m for m in client.chat_stream_calls[0] if m["role"] == "user"][0]
    assert "Describe" in user_msg["content"]


def test_respond_with_image_stream_raises_clearly_on_invalid_image():
    client = FakeOllamaClient(stream_chunks=["no deberia llegar aqui"])
    agent = LLMAgent("vision", "modelo-vision", client)

    try:
        list(agent.respond_with_image_stream("que ves?", "esto-no-es-una-imagen-de-verdad"))
        assert False, "deberia haber lanzado InvalidImageError"
    except Exception as exc:
        assert type(exc).__name__ == "InvalidImageError"
    assert client.chat_stream_calls == [], "no deberia haber llamado a Ollama con una imagen invalida"


def test_normalize_image_for_vision_converts_heic_to_jpeg():
    """HEIC (formato por defecto de fotos en iPhone) es el sospechoso real
    del bug en vivo (ver ROADMAP.md, 2026-09-25: 'Fallo analizando la
    imagen: 400 Client Error') - Pillow no lo soporta sin pillow-heif."""
    from agents.llm_agent import _normalize_image_for_vision

    heic_bytes = _make_tiny_heic_bytes()
    normalized = _normalize_image_for_vision(base64.b64encode(heic_bytes).decode())

    result_img = Image.open(io.BytesIO(base64.b64decode(normalized)))
    assert result_img.format == "JPEG"


def _make_tiny_heic_bytes() -> bytes:
    import pillow_heif
    heif_file = pillow_heif.from_pillow(Image.new("RGB", (4, 4), color=(10, 20, 30)))
    buf = io.BytesIO()
    heif_file.save(buf, format="HEIF")
    return buf.getvalue()
