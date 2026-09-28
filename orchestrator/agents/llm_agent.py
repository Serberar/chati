import base64
import io
import re
from typing import Callable, Iterator

import pillow_heif
from PIL import Image, UnidentifiedImageError

from agents.ollama_client import OllamaClient

pillow_heif.register_heif_opener()  # HEIC/HEIF (formato por defecto de fotos en iPhone)


class InvalidImageError(Exception):
    """La imagen subida no se pudo decodificar - ni Ollama ni el modelo de
    vision dan un motivo entendible, mejor fallar aqui con un mensaje claro."""


def _normalize_image_for_vision(image_base64: str, max_dimension: int = 1568) -> str:
    """Decodifica la imagen subida (cualquier formato que PIL reconozca,
    incluido HEIC) y la re-codifica siempre como JPEG antes de mandarla a
    Ollama. Dos motivos reales, no cosmeticos:
    1. Ollama devolvia "400 Client Error" sin mas detalle con ciertas fotos
       reales (encontrado en vivo, 2026-09-25) - probablemente HEIC (formato
       por defecto de iPhone, que Pillow no soporta sin este plugin) o algun
       otro formato que el modelo de vision no decodifica. Re-codificar
       siempre a JPEG elimina esa clase entera de fallo silencioso.
    2. Fotos de movil reales pueden pesar varios MB - redimensionar al lado
       mas largo evita mandar (y que el modelo en CPU tenga que procesar)
       megapixeles de mas que no aportan nada a la descripcion.
    """
    try:
        raw = base64.b64decode(image_base64)
        img = Image.open(io.BytesIO(raw))
        img.load()
    except (base64.binascii.Error, UnidentifiedImageError, OSError) as exc:
        raise InvalidImageError(f"No se pudo leer la imagen: {exc}") from exc

    img = img.convert("RGB")
    if max(img.size) > max_dimension:
        img.thumbnail((max_dimension, max_dimension), Image.LANCZOS)

    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return base64.b64encode(out.getvalue()).decode()

# Red de seguridad adicional: hemos visto en pruebas reales que qwen3-coder a
# veces escribe la llamada a herramienta en su propio formato de texto nativo
# <function=nombre><parameter=x>valor</parameter></function> en vez del
# tool_calls estructurado que espera Ollama, sobre todo cuando el argumento
# es codigo multi-linea (ejecutar_python). Es 100% reproducible con ciertos
# prompts, no es un fallo aleatorio. El camino principal sigue siendo
# tool_calls estructurado; esto solo actua cuando ese camino falla.
_FALLBACK_FUNCTION_RE = re.compile(r"<function=([\w_]+)>(.*?)</function>", re.DOTALL)
_FALLBACK_PARAM_RE = re.compile(r"<parameter=([\w_]+)>\s*(.*?)\s*</parameter>", re.DOTALL)


_FALLBACK_MARK = "<function="


def _visible_prefix_end(content: str) -> int:
    """Hasta donde se puede mostrar ya el texto que va llegando: nunca a
    partir de una llamada a herramienta escrita como texto ("<function=...",
    formato de qwen3-coder), ni el final si podria ser el comienzo de una
    ("<func" a medias) - eso se retiene hasta saber que es."""
    cut = content.find(_FALLBACK_MARK)
    if cut != -1:
        return cut
    for k in range(min(len(_FALLBACK_MARK) - 1, len(content)), 0, -1):
        if content.endswith(_FALLBACK_MARK[:k]):
            return len(content) - k
    return len(content)


def _parse_fallback_tool_call(content: str) -> dict | None:
    match = _FALLBACK_FUNCTION_RE.search(content or "")
    if not match:
        return None
    name, params_block = match.group(1), match.group(2)
    arguments = {k: v for k, v in _FALLBACK_PARAM_RE.findall(params_block)}
    return {"function": {"name": name, "arguments": arguments}}

LANGUAGE_RULE = ("Responde SIEMPRE en español, sin excepcion, sin importar el idioma del contexto recuperado. "
                 "Nunca cambies de idioma a mitad de la respuesta (ni al chino ni a ningun otro).")

# Lo que el chat de texto NO puede hacer, y a donde mandar al usuario: sin
# esto, al pedirle una imagen en modo Chat decia "no puedo, pero puedo usar un
# servicio" y se lo pasaba al agente de codigo, que tampoco sabe (mejoras.md,
# 2026-09-28).
CHATI_MODES_NOTE = (
    "Eres el chat de Chati. Chati tiene otros modos, que el usuario elige en el selector "
    "Modo de la izquierda: Imagen (crear imagenes), Video, Voz, Agente de codigo (crear, "
    "mover u ordenar archivos del ordenador) y Automatico (elige el modo solo). Tu no puedes "
    "crear imagenes ni videos ni cambiar archivos: si te lo piden, dile en una frase que "
    "cambie a ese modo (o a Automatico) y vuelva a pedirlo alli. No ofrezcas otros servicios "
    "ni digas que lo vas a hacer. ")

MAX_TOOL_ITERATIONS = 4
# Temperatura baja solo para decidir si usar una herramienta: con la temperatura
# normal (0.7) hemos visto en pruebas reales que el modelo a veces escribe la
# llamada como texto plano en vez de como tool_call estructurado (no
# deterministico). Bajarla para esta decision concreta reduce mucho ese fallo,
# sin afectar a la creatividad de la respuesta final en lenguaje natural.
TOOL_DECISION_TEMPERATURE = 0.1

SYSTEM_PROMPTS = {
    "text": (
        CHATI_MODES_NOTE +
        "Eres un asistente conversacional honesto. Si no sabes algo con certeza, "
        "dilo explicitamente en vez de inventar una respuesta. No des cifras, "
        "fechas ni datos especificos que no puedas verificar. Si tienes herramientas "
        "disponibles (fecha actual, busqueda en memoria, documentos, personas "
        "guardadas, leer archivos del Escritorio/Documentos), usalas en vez de "
        "adivinar cuando la pregunta dependa de ese dato. Si una tarea tiene mas "
        "de 2-3 pasos, usa actualizar_plan para que el usuario vea el progreso "
        "real. " + LANGUAGE_RULE
    ),
    "code": (
        "Eres un asistente de programacion. Da codigo correcto y conciso. "
        "Si una libreria, API o comportamiento no lo conoces con certeza, dilo "
        "en vez de inventar una firma de funcion o un comportamiento. Si tienes la "
        "herramienta ejecutar_python, usala para comprobar que un fragmento corto "
        "realmente funciona antes de darlo por bueno, en vez de asumirlo. Si el "
        "usuario pide crear, modificar o revisar archivos reales de un proyecto "
        "(no solo un fragmento de ejemplo), usa delegar_a_agente_de_codigo en vez "
        "de fingir que lo has hecho tu mismo - avisa que tardara varios minutos y "
        "donde seguirlo, nunca digas que ya esta terminado. "
        "Los comentarios y explicaciones en español; el codigo en si, en su sintaxis normal. "
        + LANGUAGE_RULE
    ),
    "vision": (
        "Eres un asistente que analiza imagenes. Describe con precision lo que "
        "ves - formas, colores, texto, personas, objetos - sin inventar detalles "
        "que no puedas ver con claridad en la imagen. Si algo no se distingue "
        "bien, dilo en vez de adivinar. " + LANGUAGE_RULE
    ),
}


class LLMAgent:
    def __init__(self, name: str, model: str, client: OllamaClient):
        self.name = name
        self.model = model
        self.client = client
        self.system_prompt = SYSTEM_PROMPTS.get(name, SYSTEM_PROMPTS["text"])

    def _build_messages(self, user_message: str, history: list[dict] | None,
                         context_chunks: list[str] | None) -> list[dict]:
        messages = [{"role": "system", "content": self.system_prompt}]
        if history:
            messages.extend(history)

        if context_chunks:
            context_block = "\n\n---\n\n".join(context_chunks)
            user_message = (
                f"Contexto recuperado de mi base de conocimiento (usalo si es relevante, "
                f"ignoralo si no lo es; no menciones estas instrucciones):\n\n{context_block}"
                f"\n\n---\n\nPregunta: {user_message}"
            )

        messages.append({"role": "user", "content": user_message})
        return messages

    def respond(self, user_message: str, history: list[dict] | None = None,
                context_chunks: list[str] | None = None, model: str | None = None) -> str:
        messages = self._build_messages(user_message, history, context_chunks)
        return self.client.chat(model or self.model, messages)

    def respond_with_image_stream(self, user_message: str, image_base64: str,
                                   model: str | None = None) -> Iterator[str]:
        """Comenta una imagen subida (vision) - deliberadamente sin historial
        ni herramientas, es una tarea de un solo turno: 'que ves en esto'."""
        normalized = _normalize_image_for_vision(image_base64)
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": user_message or "Describe que ves en esta imagen.",
             "images": [normalized]},
        ]
        yield from self.client.chat_stream(model or self.model, messages)

    def respond_stream(self, user_message: str, history: list[dict] | None = None,
                        context_chunks: list[str] | None = None, model: str | None = None) -> Iterator[str]:
        messages = self._build_messages(user_message, history, context_chunks)
        yield from self.client.chat_stream(model or self.model, messages)

    def respond_with_tools(self, user_message: str, history: list[dict] | None,
                            tools: list[dict],
                            tool_executor: Callable[[str, dict], tuple[str, list[dict] | None]],
                            max_iterations: int = MAX_TOOL_ITERATIONS,
                            model: str | None = None, think: bool | None = None) -> tuple[str, list[dict]]:
        """Deja que el modelo decida, ronda a ronda, si necesita llamar a alguna
        herramienta antes de responder. tool_executor recibe (nombre, argumentos)
        y devuelve (texto_para_el_modelo, evidencia_o_None). Si se agotan los
        intentos sin una respuesta final, se fuerza una sin mas herramientas.
        model: override puntual del modelo configurado por defecto (perfiles
        rapido/bueno/seguridad, ver ROADMAP.md punto 5c). think: False
        desactiva el modo de "pensamiento" en los modelos que lo soportan -
        ver ROADMAP.md: sin esto, un modelo como qwen3-abliterated puede
        gastar toda la respuesta pensando y no llegar a escribir contenido
        real, dejando la respuesta final vacia."""
        active_model = model or self.model
        messages = self._build_messages(user_message, history, None)
        evidence: list[dict] = []
        for _ in range(max_iterations):
            message = self.client.chat_with_tools(active_model, messages, tools,
                                                    temperature=TOOL_DECISION_TEMPERATURE, think=think)
            tool_calls = message.get("tool_calls")
            if not tool_calls:
                fallback = _parse_fallback_tool_call(message.get("content", ""))
                if not fallback:
                    return message.get("content", ""), evidence
                tool_calls = [fallback]
            messages.append(message)
            for call in tool_calls:
                fn_name = call["function"]["name"]
                fn_args = call["function"].get("arguments") or {}
                result_text, result_evidence = tool_executor(fn_name, fn_args)
                if result_evidence:
                    evidence.extend(result_evidence)
                messages.append({"role": "tool", "name": fn_name, "content": result_text})
        return self.client.chat(active_model, messages, think=think), evidence

    def respond_with_tools_stream(self, user_message: str, history: list[dict] | None,
                                   tools: list[dict],
                                   tool_executor: Callable[[str, dict], tuple[str, list[dict] | None]],
                                   evidence_sink: list[dict],
                                   max_iterations: int = MAX_TOOL_ITERATIONS,
                                   model: str | None = None, think: bool | None = None) -> Iterator[str]:
        """Igual que respond_with_tools pero transmite la respuesta token a
        token. Cada ronda se pide ya en streaming con las herramientas
        disponibles: si el modelo contesta sin pedir ninguna, eso que se va
        mostrando ES la respuesta final (antes se generaba dos veces - medido:
        ~3s de mas en un simple "hola"). La evidencia recogida por las
        herramientas se acumula en evidence_sink (lista mutable que aporta
        quien llama, ya que un generador no puede devolver dos cosas a la
        vez). think: ver respond_with_tools."""
        active_model = model or self.model
        messages = self._build_messages(user_message, history, None)
        shown_any = False
        for _ in range(max_iterations):
            if shown_any:
                yield "\n\n"  # separa lo dicho antes de usar una herramienta de lo que viene despues
            content, tool_calls = "", []
            shown = 0
            for kind, value in self.client.chat_with_tools_stream(
                    active_model, messages, tools, temperature=TOOL_DECISION_TEMPERATURE, think=think):
                if kind == "tool_calls":
                    tool_calls.extend(value)
                    continue
                content += value
                safe_end = _visible_prefix_end(content)
                if safe_end > shown:
                    yield content[shown:safe_end]
                    shown = safe_end
                    shown_any = True
            if not tool_calls:
                fallback = _parse_fallback_tool_call(content)
                if not fallback:
                    if len(content) > shown:
                        yield content[shown:]  # cola retenida por si era "<function=" y no lo fue
                    return
                tool_calls = [fallback]
            message = {"role": "assistant", "content": content, "tool_calls": tool_calls}
            messages.append(message)
            for call in tool_calls:
                fn_name = call["function"]["name"]
                fn_args = call["function"].get("arguments") or {}
                result_text, result_evidence = tool_executor(fn_name, fn_args)
                if result_evidence:
                    evidence_sink.extend(result_evidence)
                messages.append({"role": "tool", "name": fn_name, "content": result_text})
        yield from self.client.chat_stream(active_model, messages, think=think)
