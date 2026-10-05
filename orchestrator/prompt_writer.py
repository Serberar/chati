"""Pasa una peticion sencilla en español ("un perro en la nieve") a la
descripcion detallada en ingles que necesitan los generadores (FLUX, SDXL,
LTX-Video). Sergio, 2026-10-01: "quiero darle ordenes sencillas sin palabras
tecnicas ni prompt positivo ni negativo". Antes la frase llegaba tal cual:
SDXL apenas entiende español y una frase corta da imagenes pobres. El
prompt negativo no hace falta: va fijo en cada plantilla (y FLUX no lo usa).

Para editar una foto adjunta esta photo_edit.plan_edit, que es otra cosa:
alli se describe un CAMBIO sobre una foto, no una imagen nueva."""

import json
import re

IMAGE_SIZES = {"cuadrado": (1024, 1024), "vertical": (832, 1216), "horizontal": (1216, 832)}

IMAGE_PROMPT = """Eres quien escribe las descripciones para un generador de imagenes con IA
(FLUX/SDXL), que solo entiende ingles. El usuario lo pide con palabras
sencillas en español; tu lo conviertes en una buena descripcion.

Peticion del usuario: "{request}"

Devuelve SOLO un JSON con:
- "prompt": la descripcion en ingles, una o dos frases. Primero QUE se ve
  (lo que pidio, sin cambiarlo: si pide "un perro", no lo conviertas en "un
  golden retriever con bufanda"; si da detalles, respetalos todos), despues
  como se ve: tipo de imagen (si no dice nada, foto realista; si pide dibujo,
  acuarela, dibujos animados, logo... ese estilo), luz, encuadre y calidad
  ("natural light", "sharp focus", "highly detailed"). Si pide texto escrito
  en la imagen, ponlo entre comillas tal cual.
  No copies para quien o para que es ("para mi sobrino", "fondo de pantalla
  del movil"): usalo solo como pista (para un niño: dibujo amable y colorido;
  para el movil: formato vertical).
- "formato": "vertical" (movil, retrato, poster, fondo de pantalla del movil,
  una persona de cuerpo entero), "horizontal" (paisaje, panoramica, fondo de
  escritorio, portada, banner) o "cuadrado" (lo demas, o si no esta claro).

Ejemplos:
"un perro en la nieve" -> {{"prompt": "A realistic photo of a dog standing in fresh snow, soft winter daylight, snowflakes in the air, shallow depth of field, natural colors, sharp focus, highly detailed.", "formato": "cuadrado"}}
"un fondo de pantalla para el movil de una playa al atardecer" -> {{"prompt": "A realistic photo of a tropical beach at sunset, golden sky reflected on calm waves, palm trees silhouetted against the horizon, warm natural light, high detail.", "formato": "vertical"}}
"dibujame un gato astronauta para mi hija" -> {{"prompt": "A cute colorful cartoon illustration of a cat astronaut floating in space among stars and planets, friendly expression, clean lines, bright cheerful colors, children's book style.", "formato": "cuadrado"}}

Antes de responder, revisa el "prompt": no puede decir para quien es ni para
que se usa (nada de "for my nephew", "for my daughter", "phone wallpaper",
"desktop background"): solo lo que se VE en la imagen."""

VIDEO_PROMPT = """Eres quien escribe las descripciones para un generador de video con IA
(LTX-Video), que solo entiende ingles y funciona mejor con una descripcion
detallada de lo que se ve y de como se mueve. El usuario lo pide con
palabras sencillas en español.
{from_image}
Peticion del usuario: "{request}"

Devuelve SOLO un JSON con "prompt": la descripcion en ingles, tres o cuatro
frases en presente. Que se ve (lo que pidio, sin cambiarlo), que movimiento
hay de principio a fin (acciones concretas, lentas y naturales), como se
mueve la camara (fija, travelling lento, acercamiento suave...) y la luz y el
ambiente. Si no pide otra cosa, que parezca grabado de verdad.

Ejemplo:
"olas rompiendo en una playa" -> {{"prompt": "Gentle ocean waves roll onto a sandy beach and break into white foam that spreads across the shore before slowly receding. The camera stays still at a low angle near the waterline. Warm late afternoon sunlight reflects on the wet sand and the water surface, with a clear sky above. Realistic footage with natural motion."}}"""

FROM_IMAGE = """
El video parte de una FOTO que ya existe (el primer fotograma): no
describas otra escena ni otras personas. Habla de "the people in the image"
(o "the man", "the woman", lo que diga el usuario), di que se mueven como
pide sin cambiar su aspecto, mirando mas o menos hacia la camara, y deja la
camara fija o con un acercamiento muy suave (nunca por detras).
"""


# qwen3:8b los cuela aunque el prompt lo prohiba (probado 2026-10-01); al
# generador no le dicen nada de lo que se ve y "wallpaper" le hace pintar
# marcos e iconos de movil
_PURPOSE = [
    (re.compile(r"\b(?:phone |mobile |smartphone |desktop |computer )?wallpaper\b(?:\s+(?:featuring|with|of|showing))?",
                re.I), "image of"),
    (re.compile(r",?\s*\bfor (?:my|his|her|your|a|the) (?:little |young )?"
                r"(?:nephew|niece|son|daughter|mother|father|mom|dad|wife|husband|friend|kid|child|children|"
                r"brother|sister|grandson|granddaughter|boy|girl)\b", re.I), ""),
]


def _clean(prompt: str) -> str:
    for pattern, replacement in _PURPOSE:
        prompt = pattern.sub(replacement, prompt)
    return re.sub(r"\s{2,}", " ", prompt).strip()


def _ask(ollama, model: str, content: str) -> dict:
    raw = ollama.chat(model, [{"role": "user", "content": content}], temperature=0.2, think=False)
    try:
        return json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {}


def image_prompt(ollama, model: str, request: str) -> tuple[str, tuple[int, int]]:
    """(descripcion en ingles, (ancho, alto)). Si el modelo no responde bien
    se usa la frase tal cual en cuadrado: peor, pero algo sale."""
    data = _ask(ollama, model, IMAGE_PROMPT.format(request=request))
    prompt = _clean(str(data.get("prompt") or "")) or request
    return prompt, IMAGE_SIZES.get(str(data.get("formato", "")).strip().lower(), IMAGE_SIZES["cuadrado"])


def video_prompt(ollama, model: str, request: str, from_image: bool = False) -> str:
    content = VIDEO_PROMPT.format(request=request, from_image=FROM_IMAGE if from_image else "")
    return str(_ask(ollama, model, content).get("prompt") or "").strip() or request
