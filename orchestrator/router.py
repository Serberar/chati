import json

from agents.ollama_client import OllamaClient

VALID_AGENTS = {"text", "code", "image", "video"}

ROUTE_PROMPT = """Analiza la peticion del usuario y responde EXACTAMENTE con
este formato JSON, sin nada mas alrededor:

{{"agent": "<text|code|image|video>", "factual": <true|false>}}

- agent:
  - text: conversacion, preguntas, razonamiento, explicaciones, traduccion
  - code: programar, depurar, explicar codigo, ejecutar scripts
  - image: generar o crear una imagen, foto, ilustracion, dibujo
  - video: generar o crear un video o clip
- factual: true si responder correctamente requiere datos verificables
  (fechas, cifras, nombres propios, eventos concretos que podrian ser
  incorrectos). false si es conversacion, opinion, creatividad o codigo.

Peticion del usuario: "{query}"
"""


class Router:
    """Combina en una sola llamada al modelo lo que antes eran dos: elegir
    agente y decidir si la pregunta es factual (para el verificador). Menos
    llamadas en serie = menos latencia por turno."""

    def __init__(self, client: OllamaClient, routing_model: str):
        self.client = client
        self.routing_model = routing_model

    def classify(self, query: str) -> dict:
        raw = self.client.chat(
            self.routing_model,
            [{"role": "user", "content": ROUTE_PROMPT.format(query=query)}],
            temperature=0.0,
        )
        try:
            start = raw.index("{")
            end = raw.rindex("}") + 1
            data = json.loads(raw[start:end])
            agent = str(data.get("agent", "text")).strip().lower()
            factual = bool(data.get("factual", False))
        except (ValueError, json.JSONDecodeError):
            agent, factual = "text", False

        if agent not in VALID_AGENTS:
            agent = "text"
        return {"agent": agent, "factual": factual}
