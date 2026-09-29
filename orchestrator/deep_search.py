"""Busqueda profunda (Mis apps, pedido por Sergio el 2026-09-29): una peticion
libre ("buscame casas en compra en Valladolid en La Victoria, Giron o
cerca") y la app se lo curra sola: entiende que se busca y con que
criterios, elige que webs tienen sentido (para casas, portales
inmobiliarios), busca, lee cada pagina y devuelve:
- "listado": resultados concretos (casas, coches, cursos...) con los datos
  que importan en cada caso, ordenados por lo bien que encajan; o
- "informe": si la peticion es una pregunta, una respuesta con sus fuentes.
Se puede afinar despues ("solo con ascensor") sin perder el contexto.

Como las otras apps del navegador (ver web_tools.py): el recorrido lo lleva
el codigo y el modelo decide lo que necesita criterio. Una pagina de listado
de un portal trae decenas de anuncios: se sacan de ahi directamente (con su
enlace) en vez de abrir cada uno."""

import datetime
import re
import threading
import time
import uuid
from typing import Callable

from web_tools import Blocked, Browser, ChatFn, EncryptedStore, json_from, site_label

MAX_HISTORY = 10
# Todo tiene que caber en los 8K de contexto de las apps (ver APPS_NUM_CTX en
# main.py): texto + enlaces + la respuesta con hasta 15 resultados.
MAX_PAGES = 12
PAGE_MAX_CHARS = 8000
MAX_LINKS = 60
MAX_ITEMS_PER_PAGE = 15
MIN_MATCH = 6
_NOT_USEFUL = ("youtube.", "facebook.", "instagram.", "tiktok.", "x.com", "twitter.", "pinterest.")

_store = EncryptedStore("deep_meta.json")


def get_history(user_id: str, dek: bytes, key_generation: int) -> list[dict]:
    return _store.load("deep_history", user_id, dek, key_generation, [])


# ---------- el modelo ----------

PLAN_PROMPT = """Hoy es {today}. El usuario vive en España y pide:
---
{request}
---
Prepara una busqueda en internet para resolverlo. Responde SOLO con JSON:
{{"entendido": "una o dos frases: que busca y con que condiciones, como se lo explicarias al usuario",
  "tipo": "listado" si hay que encontrar cosas concretas (viviendas, coches, productos, cursos, ofertas...); "informe" si es una pregunta que se responde con informacion,
  "criterios": ["condiciones que debe cumplir cada resultado: primero QUE es exactamente (el tipo que nombra el usuario), y luego las demas, incluidas las que se deducen (p.ej. si dice 'o cerca', los barrios vecinos)"],
  "campos": ["hasta 5 datos cortos que importan de cada resultado, p.ej. precio, m2, habitaciones, barrio (solo para listado; nada de descripciones largas)"],
  "limites": [{{"campo": "uno de los campos", "min": numero o null, "max": numero o null}}] (SOLO los que el usuario dice con un numero, p.ej. "menos de 12000 euros" -> precio max 12000; nunca inventes limites; vacio si no dice ninguno),
  "webs": ["hasta 4 webs especializadas que existan de verdad y conozcas bien (p.ej. idealista.com); si dudas, ninguna"],
  "busquedas": ["3 busquedas distintas para un buscador, en español, cortas y como las escribiria una persona: que + donde (p.ej. 'casas en venta La Victoria Valladolid'), sin precios ni otros numeros"]}}"""


def _numbers_in(text: str) -> set[float]:
    """Los numeros que escribio el usuario: "200.000", "12000", "200 mil", "200k"."""
    found = set()
    for m in re.finditer(r"(\d{1,3}(?:[.\s ]\d{3})+|\d+)\s*(mil\b|k\b)?", text.lower()):
        n = float(re.sub(r"[.\s ]", "", m.group(1)))
        found.add(n * 1000 if m.group(2) else n)
    return found


def _clean_limits(raw, campos: list[str], request: str) -> list[dict]:
    """Solo los limites cuyo numero escribio el usuario: sin pedirlo, el
    modelo se invento "precio entre 50.000 y 200.000" y se descartaron 46
    casas (2026-09-29)."""
    said = _numbers_in(request)
    out = []
    for lim in raw if isinstance(raw, list) else []:
        campo = _match_field(lim.get("campo"), campos) if isinstance(lim, dict) else None
        if not campo:
            continue
        lo, hi = parse_number(lim.get("min")), parse_number(lim.get("max"))
        lo, hi = (lo if lo in said else None), (hi if hi in said else None)
        if lo is not None or hi is not None:
            out.append({"campo": campo, "min": lo, "max": hi})
    return out


def _plain(text) -> str:
    """Minusculas y sin tildes, para comparar palabras."""
    table = str.maketrans("áéíóúüñ", "aeiouun")
    return str(text or "").lower().translate(table)


def _match_field(name, campos: list[str]) -> str | None:
    """El campo al que se refiere un limite aunque el modelo lo llame un poco
    distinto: "habitaciones minimas" -> "habitaciones" (asi se perdia el
    limite de 3 habitaciones, 2026-09-29)."""
    n = re.sub(r"[^a-z0-9]", "", _plain(name))
    if not n:
        return None
    for c in campos:
        cn = re.sub(r"[^a-z0-9]", "", _plain(c))
        if cn and (cn == n or cn in n or n in cn or cn[:5] == n[:5]):
            return c
    return None


# palabras que no distinguen nada: "vivienda" incluye pisos y casas
_GENERIC_KIND = {"vivienda", "viviendas", "inmueble", "inmuebles", "propiedad", "propiedades", "producto",
                 "productos", "articulo", "articulos", "resultado", "resultados", "oferta", "ofertas", "anuncio",
                 "anuncios", "cosa", "cosas", "opcion", "opciones"}

KIND_PROMPT = """El usuario pide: "{request}"

¿Pide un TIPO concreto de cosa? Responde SOLO con JSON: {{"si": [...], "no": [...]}}
- "si": palabras con las que se anuncia exactamente lo que pide, con sus sinonimos.
- "no": cosas parecidas que NO pide y que suelen salir mezcladas en los resultados.
Ejemplos:
- "casas en venta": {{"si": ["casa", "chalet", "adosado", "pareado", "unifamiliar", "villa"], "no": ["piso", "apartamento", "atico", "estudio", "duplex", "local", "garaje"]}}
- "pisos de alquiler": {{"si": ["piso", "apartamento", "atico", "estudio"], "no": ["casa", "chalet", "adosado", "local", "habitacion"]}}
- "un coche familiar": {{"si": ["coche", "turismo", "familiar", "monovolumen", "suv"], "no": ["moto", "furgoneta", "camion"]}}
- "cursos de ingles": {{"si": ["curso", "clases"], "no": []}}
- "que ayudas hay para jovenes": {{"si": [], "no": []}}
Nunca uses palabras generales como vivienda, inmueble, propiedad o producto."""


def plan_kind(chat: ChatFn, request: str) -> dict:
    """El tipo exacto que se pide, en una pregunta aparte: dentro del plan
    completo, qwen3:8b ponia "casas, viviendas, inmuebles" y ningun "no", y
    salian pisos (2026-09-29)."""
    return _clean_kind(json_from(chat(KIND_PROMPT.format(request=request))))


def _clean_kind(raw) -> dict:
    def words(key):
        if not isinstance(raw, dict):
            return []
        out = []
        for w in raw.get(key, []):
            w = _plain(w).strip()
            if w and w not in _GENERIC_KIND and w not in out:
                out.append(w)
        return out[:12]
    si, no = words("si"), words("no")
    return {"si": si, "no": [w for w in no if w not in si]}


# ---------- limites escritos por el usuario, sacados por el codigo ----------
_NUM = r"(\d{1,3}(?:[.\s]\d{3})+|\d+(?:[.,]\d+)?)\s*(mil\b|k\b)?"
_UNIT = r"\s*(?:de\s+)?([a-zA-Z€²áéíóúñ/]+)"
_MIN_WORDS = r"(?:al menos|como m[ií]nimo|m[ií]nimo(?: de)?|m[aá]s de|desde|a partir de|no menos de)"
_MAX_WORDS = r"(?:como m[aá]ximo|m[aá]ximo(?: de)?|menos de|hasta|no m[aá]s de|por debajo de|que no pase de)"
# unidades -> que dato es (para encontrar el campo que lo guarda)
_UNIT_FIELDS = [
    (re.compile(r"^(€|eur|euros?|precio)$"), ("precio", "coste", "presupuesto", "importe")),
    (re.compile(r"^(hab|habs|habitacion(es)?|dormitorios?|cuartos?)$"), ("habitacion", "dormitorio", "cuarto")),
    (re.compile(r"^(km|kms|kilometros?|kilometraje)$"), ("km", "kilomet")),
    (re.compile(r"^(m2|m²|metros?|mts)$"), ("m2", "metro", "superficie", "tamano")),
    (re.compile(r"^(banos?|aseos?)$"), ("bano", "aseo")),
    (re.compile(r"^(anos?|antiguedad)$"), ("ano", "antiguedad")),
]


def _value(num: str, mult) -> float:
    n = re.sub(r"[.\s](?=\d{3}\b)", "", num).replace(",", ".")
    return float(n) * (1000 if mult else 1)


def _field_for_unit(unit: str, campos: list[str]) -> str | None:
    u = _plain(unit)
    for pattern, hints in _UNIT_FIELDS:
        if pattern.match(u):
            for c in campos:
                if any(h in _plain(c) for h in hints):
                    return c
            return None
    return _match_field(unit, campos)


def _limits_from_text(request: str, campos: list[str]) -> list[dict]:
    """"al menos 3 habitaciones", "maximo 260.000 euros", "menos de 150000 km",
    "entre 100 y 200 m2"... - reglas del español, sin depender del modelo (se
    le escapaba "al menos 3 habitaciones", 2026-09-29)."""
    text = _plain(request)
    found: dict[str, dict] = {}

    def add(field, lo=None, hi=None):
        if not field:
            return
        cur = found.setdefault(field, {"campo": field, "min": None, "max": None})
        cur["min"] = lo if lo is not None else cur["min"]
        cur["max"] = hi if hi is not None else cur["max"]

    for m in re.finditer(r"entre\s+" + _NUM + r"\s+y\s+" + _NUM + _UNIT, text):
        add(_field_for_unit(m.group(5), campos), _value(m.group(1), m.group(2)), _value(m.group(3), m.group(4)))
    for m in re.finditer(_MIN_WORDS + r"\s+" + _NUM + _UNIT, text):
        add(_field_for_unit(m.group(3), campos), lo=_value(m.group(1), m.group(2)))
    for m in re.finditer(_MAX_WORDS + r"\s+" + _NUM + _UNIT, text):
        add(_field_for_unit(m.group(3), campos), hi=_value(m.group(1), m.group(2)))
    return list(found.values())


def _merge_limits(from_text: list[dict], from_model: list[dict]) -> list[dict]:
    """Manda lo que saca el codigo del texto; el modelo solo añade campos nuevos."""
    have = {l["campo"] for l in from_text}
    return from_text + [l for l in from_model if l["campo"] not in have]


def matches_kind(item: dict, kind: dict) -> bool:
    """Es del tipo pedido: fuera lo que en el titulo se anuncia como otra cosa
    ("Piso en..." cuando se piden casas, 2026-09-29), salvo que tambien diga
    lo pedido ("casa o piso"). Si el titulo no dice nada, se deja."""
    words = re.sub(r"[^a-z0-9]+", " ", _plain(item["titulo"] + " " + item["datos"].get("tipo", ""))).split()

    def has(text_words, w):
        return any(t in (w, w + "s", w + "es") for t in text_words)

    # manda como EMPIEZA: los anuncios empiezan por el tipo ("Piso en ...").
    # Si no, "Piso en ..., Giron - Villa del Prado" pasaba por "villa".
    head = words[:3]
    if any(has(head, w) for w in kind.get("no", [])) and not any(has(head, w) for w in kind.get("si", [])):
        return False
    if any(has(words, w) for w in kind.get("si", [])):
        return True
    return not any(has(words, w) for w in kind.get("no", []))


def parse_number(value) -> float | None:
    """El primer numero de un dato: "14.900 €" -> 14900, "1.150.000" ->
    1150000, "146.000 km" -> 146000, 12000 -> 12000. Los puntos (y espacios)
    de miles se quitan; los decimales se ignoran (bastan para comparar)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    date = re.search(r"\b\d{1,2}/(\d{4})\b", str(value))  # "02/2020": el año
    if date:
        return float(date.group(1))
    m = re.search(r"\d{1,3}(?:[.\s\u00a0]\d{3})+|\d+", str(value))
    return float(re.sub(r"[.\s\u00a0]", "", m.group(0))) if m else None


def within_limits(item: dict, limits: list[dict]) -> bool:
    """Lo decide el codigo, no el modelo: qwen3:8b ponia un 10 a un coche de
    25.000 EUR con un maximo de 12.000 (2026-09-29). Sin el dato, se deja."""
    for lim in limits:
        n = parse_number(item["datos"].get(lim["campo"]))
        if n is None:
            continue
        # "18.2" por 18.200 EUR o "237 km" por 237.000: el modelo corta los
        # miles (coches, 2026-09-29). Cien veces por debajo del maximo es mal leido.
        if lim["max"] is not None and lim["max"] >= 1000 and n < lim["max"] / 100:
            return False
        if (lim["min"] is not None and n < lim["min"]) or (lim["max"] is not None and n > lim["max"]):
            return False
    return True


def plan_search(chat: ChatFn, request: str) -> dict:
    data = json_from(chat(PLAN_PROMPT.format(request=request, today=datetime.date.today().isoformat())))
    clean = lambda key, n, size=120: [str(x).strip()[:size] for x in data.get(key, []) if str(x).strip()][:n]
    webs = [w.lower().removeprefix("https://").removeprefix("http://").removeprefix("www.").strip("/")
            for w in clean("webs", 4, 60)]
    return {
        "entendido": str(data.get("entendido") or request).strip()[:400],
        "tipo": "informe" if str(data.get("tipo", "")).strip().lower() == "informe" else "listado",
        "criterios": clean("criterios", 8, 150),
        "campos": clean("campos", 5, 30),
        "limites": _merge_limits(_limits_from_text(request, clean("campos", 5, 30)),
                                 _clean_limits(data.get("limites"), clean("campos", 5, 30), request)),
        "webs": [w for w in webs if "." in w and " " not in w],
        "busquedas": clean("busquedas", 3) or [request[:120]],
    }


EXTRACT_PROMPT = """Se busca: {entendido}
Criterios: {criterios}
Datos que importan de cada resultado: {campos}

Texto de una pagina web ({url}):
---
{page}
---
Enlaces de la pagina (numero: texto):
{links}

Saca de esta pagina los resultados concretos que haya (anuncios, fichas...), como mucho {n}.
Responde SOLO con JSON, asi:
{{"pagina": "listado" | "ficha" | "otra",
  "r": [{{"t": "titulo corto", "v": [{campos_json}], "e": numero del enlace de ese resultado o null, "p": 0-10}}]}}
- "pagina": "listado" si muestra varios resultados, "ficha" si es UNO, "otra" si no hay ninguno.
- "v": los valores en este orden: {campos}; null si no aparece.
- "p": 10 si cumple TODOS los criterios. Si incumple claramente alguno (es de otra zona, de otro tipo
  o de otra clase de lo pedido...), 4 o menos. Si falta el dato para saberlo, 7.
Copia los datos tal como aparecen en el texto. Si la pagina no tiene resultados, "r": []. No inventes nada."""


def _links_block(links: list[tuple[str, str]], page_url: str) -> list[tuple[str, str]]:
    """Enlaces candidatos a llevar a un resultado: con texto y a la misma web."""
    host = site_label(page_url)
    out, seen = [], set()
    for text, href in links:
        if 12 <= len(text) <= 200 and site_label(href) == host and href not in seen and href != page_url:
            seen.add(href)
            out.append((text, href))
    return out[:MAX_LINKS]


def extract_items(chat: ChatFn, plan: dict, page: dict) -> list[dict]:
    links = _links_block(page["links"], page["url"])
    campos = plan["campos"] or ["precio"]
    data = json_from(chat(EXTRACT_PROMPT.format(
        entendido=plan["entendido"], criterios="; ".join(plan["criterios"]) or "-",
        campos=", ".join(campos), campos_json=", ".join(f'"{c}"' for c in campos),
        url=page["url"], page=page["text"][:PAGE_MAX_CHARS], n=MAX_ITEMS_PER_PAGE,
        links="\n".join(f"{i}: {t[:90]}" for i, (t, _h) in enumerate(links)) or "(ninguno)")))
    kind = str(data.get("pagina", "")).strip().lower()
    items = []
    for r in (data.get("r") or [])[:MAX_ITEMS_PER_PAGE]:
        if not isinstance(r, dict):
            continue
        try:
            match = max(0, min(10, int(r.get("p", 0))))
        except (TypeError, ValueError):
            match = 0
        title = str(r.get("t") or "").strip()
        url = page["url"] if kind == "ficha" else _pick_link(links, r.get("e"), title)
        values = r.get("v") if isinstance(r.get("v"), list) else []
        datos = {c: str(v).strip()[:80] for c, v in zip(campos, values)
                 if v not in (None, "", "null", "...") and str(v).strip()}
        item = {"titulo": str(r.get("t") or "").strip()[:150] or "Resultado", "datos": datos,
                "url": url or page["url"], "fuente": site_label(page["url"]), "encaje": match, "nota": ""}
        if grounded(item, page["text"]):
            items.append(item)
    return items


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9áéíóúñü]{4,}", text.lower())}


def _pick_link(links: list[tuple[str, str]], chosen, title: str) -> str | None:
    """El enlace que eligio el modelo, si su texto comparte alguna palabra
    con el titulo (en autoscout24 elegia "/account" o "/financiacion"); si
    no, el enlace mas parecido al titulo; si ninguno se parece, None."""
    words = _words(title)
    try:
        text, href = links[int(chosen)]
        if words & _words(text):
            return href
    except (TypeError, ValueError, IndexError):
        pass
    best = max(links, key=lambda l: len(words & _words(l[0])), default=None)
    return best[1] if best and words & _words(best[0]) else None


def _norm(text: str) -> str:
    return re.sub(r"[\s.\u00a0]+", "", text.lower())


def grounded(item: dict, page_text: str) -> bool:
    """El resultado tiene que estar de verdad en la pagina: con una pagina
    vacia, el modelo se invento 15 anuncios (medido el 2026-09-29). Vale si
    aparece al menos un dato concreto (precio, calle...) o el titulo."""
    text = _norm(page_text)
    if not text:
        return False
    for value in list(item["datos"].values()) + [item["titulo"][:25]]:
        v = _norm(value)
        if len(v) >= 3 and v in text:
            return True
    return False


FACTS_PROMPT = """Pregunta: {entendido}

Texto de una pagina web ({url}):
---
{page}
---
¿Que dice esta pagina que sirva para responder? Responde SOLO con JSON:
{{"util": true | false, "titulo": "titulo de la pagina",
  "datos": ["hasta 6 datos concretos y utiles (con cifras, fechas, requisitos...) tal como aparecen"]}}"""


def extract_facts(chat: ChatFn, plan: dict, page: dict) -> dict | None:
    data = json_from(chat(FACTS_PROMPT.format(entendido=plan["entendido"], url=page["url"],
                                              page=page["text"][:PAGE_MAX_CHARS])))
    facts = [str(f).strip()[:300] for f in data.get("datos", []) if str(f).strip()][:6]
    if not data.get("util") or not facts:
        return None
    return {"titulo": str(data.get("titulo") or site_label(page["url"])).strip()[:150],
            "url": page["url"], "fuente": site_label(page["url"]), "datos": facts}


SUMMARY_PROMPT = """El usuario pidio: {entendido}
Se han encontrado {total} resultados en total. Los primeros (n: titulo | datos | fuente):
{table}

Responde SOLO con JSON:
{{"resumen": "3 a 5 frases para el usuario: cuantos hay, rangos (precios, tamaños...), zonas, y lo que mas destaca",
  "destacados": [{{"n": numero, "por_que": "una frase"}}]}}  (los 3 que mas le pueden interesar)"""


def summarize(chat: ChatFn, plan: dict, items: list[dict]) -> tuple[str, list[dict]]:
    if not items:
        return "", []
    table = "\n".join(f"{i}: {it['titulo'][:70]} | " + ", ".join(f"{k}: {v}" for k, v in it["datos"].items())
                      + f" | {it['fuente']}" for i, it in enumerate(items[:40]))
    data = json_from(chat(SUMMARY_PROMPT.format(entendido=plan["entendido"], table=table, total=len(items))))
    picked, seen = [], set()
    for d in data.get("destacados", []):
        try:
            i = int(d.get("n"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= i < len(items) and i not in seen:
            seen.add(i)
            picked.append({"url": items[i]["url"], "titulo": items[i]["titulo"],
                           "por_que": str(d.get("por_que", "")).strip()[:300]})
    return str(data.get("resumen") or "").strip()[:1500], picked[:3]


REPORT_PROMPT = """Pregunta del usuario: {entendido}

Informacion encontrada, por fuente:
{sources}

Escribe la respuesta en español, clara y ordenada (puedes usar listas), usando SOLO esta informacion.
Pon el numero de la fuente entre corchetes tras cada dato, p.ej. [2]. Si algo no esta claro o las fuentes
no coinciden, dilo. Termina con una frase de lo que conviene comprobar por su cuenta si hace falta."""


def write_report(chat: ChatFn, plan: dict, sources: list[dict]) -> str:
    if not sources:
        return ""
    block = "\n".join(f"[{i + 1}] {s['titulo']} ({s['fuente']}): " + " / ".join(s["datos"])
                      for i, s in enumerate(sources))
    return chat(REPORT_PROMPT.format(entendido=plan["entendido"], sources=block)).strip()


# ---------- la busqueda completa ----------

class Cancelled(Exception):
    pass


def _number_key(item: dict) -> tuple:
    """Los numeros de sus datos (precio, m2, habitaciones...): el mismo anuncio
    en otro portal los tiene iguales aunque los escriba distinto ("260.000 €",
    "260000") - con el texto completo salia hasta 4 veces la misma casa."""
    nums = []
    for v in item["datos"].values():
        if len(str(v)) <= 40:  # las descripciones largas no cuentan
            n = parse_number(v)
            if n is not None:
                nums.append(int(n))
    return tuple(sorted(nums))


def _dedupe(items: list[dict]) -> list[dict]:
    """Fuera los repetidos: el mismo enlace y titulo (sale en varias
    busquedas), o los mismos datos en otro portal (el mismo piso en idealista
    y en yaencontre) - este ultimo se queda uno, con "tambien en"."""
    out, seen, by_data = [], set(), {}
    for it in items:
        key = (it["url"], it["titulo"].lower()[:60])
        if key in seen:
            continue
        seen.add(key)
        values = _number_key(it)
        if len(values) >= 2 and values in by_data:
            first = by_data[values]
            if it["fuente"] != first["fuente"] and it["fuente"] not in first.setdefault("tambien_en", []):
                first["tambien_en"].append(it["fuente"])
            continue
        if len(values) >= 2:
            by_data[values] = it
        out.append(it)
    return out


def run_search(chat: ChatFn, browser, request: str, progress: Callable[[str, int, int], None],
               cancelled: Callable[[], bool]) -> dict:
    def check():
        if cancelled():
            raise Cancelled()

    progress("Entendiendo lo que buscas…", 0, 0)
    plan = plan_search(chat, request)
    plan["que_es"] = plan_kind(chat, request) if plan["tipo"] == "listado" else {"si": [], "no": []}
    warnings: list[str] = []

    candidates: list[str] = []

    def search(query: str, n: int) -> None:
        try:
            found = browser.bing(query, n)
        except Exception:
            found = []
        for url in found:
            if url not in candidates and not any(bad in url for bad in _NOT_USEFUL):
                candidates.append(url)

    total = len(plan["busquedas"]) + len(plan["webs"])
    for i, query in enumerate(plan["busquedas"]):
        check()
        progress(f"Buscando «{query}»…", i, total)
        search(query, 6)
    # dentro de las webs que propone el modelo, solo si han salido en Bing: a
    # veces se inventaba algunas ("inmobiliaria.com", "anuncios.com")
    seen_sites = {site_label(u) for u in candidates}
    plan["webs"] = [w for w in plan["webs"] if any(s == w or s.endswith("." + w) for s in seen_sites)]
    for i, web in enumerate(plan["webs"]):
        check()
        progress(f"Buscando en {web}…", len(plan["busquedas"]) + i, total)
        search(f"site:{web} {plan['busquedas'][0]}", 4)
    if not candidates:
        warnings.append("Bing no devolvio resultados; prueba a pedirlo de otra forma.")

    items, sources, blocked = [], [], set()
    pages = candidates[:MAX_PAGES]
    for i, url in enumerate(pages):
        check()
        progress(f"Leyendo páginas ({i + 1} de {len(pages)})…", i, len(pages))
        try:
            page = browser.fetch_page(url)
            if len(page["text"].strip()) < 200:
                continue  # pagina vacia o que no llego a cargar
            if plan["tipo"] == "informe":
                facts = extract_facts(chat, plan, page)
                if facts:
                    sources.append(facts)
            else:
                items += extract_items(chat, plan, page)
        except Cancelled:
            raise
        except Blocked:
            blocked.add(site_label(url))
        except Exception:
            continue
    if blocked:
        warnings.append("No dejaron mirar: " + ", ".join(sorted(blocked)) + ".")

    check()
    progress("Juntándolo todo…", 0, 0)
    result = {"entendido": plan["entendido"], "tipo": plan["tipo"], "criterios": plan["criterios"],
              "limites": plan["limites"], "que_es": plan["que_es"],
              "campos": plan["campos"], "webs": plan["webs"], "busquedas": plan["busquedas"],
              "paginas": len(pages), "avisos": warnings}
    if plan["tipo"] == "informe":
        result.update(informe=write_report(chat, plan, sources), fuentes=sources, resultados=[],
                      resumen="", destacados=[])
        if not sources:
            warnings.append("No se encontro informacion util en las paginas revisadas.")
        return result
    unique = _dedupe(items)
    other_kind = [it for it in unique if not matches_kind(it, plan["que_es"])]
    limit_fields = {l["campo"] for l in plan["limites"]}
    # sin ninguno de los datos que se piden con numero, no se puede saber si cumple
    empty = [it for it in unique if limit_fields and not limit_fields & set(it["datos"])]
    kept = [it for it in unique if it["encaje"] >= MIN_MATCH and within_limits(it, plan["limites"])
            and it not in other_kind and it not in empty]
    kept.sort(key=lambda it: (-it["encaje"], -len(it["datos"])))
    if items and not kept:
        warnings.append("Se encontraron resultados, pero ninguno cumple bien lo pedido.")
    if other_kind:
        warnings.append(f"{len(other_kind)} descartados por ser de otro tipo "
                        f"({', '.join(plan['que_es']['no'][:4])}…).")
    if empty:
        warnings.append(f"{len(empty)} descartados por no indicar {', '.join(sorted(limit_fields))}.")
    out_of_limits = sum(1 for it in unique if not within_limits(it, plan["limites"]))
    if out_of_limits:
        warnings.append(f"{out_of_limits} descartados por no cumplir " + ", ".join(
            f"{l['campo']} {'≥ ' + format(l['min'], 'g') if l['min'] is not None else ''}"
            f"{' y ' if l['min'] is not None and l['max'] is not None else ''}"
            f"{'≤ ' + format(l['max'], 'g') if l['max'] is not None else ''}" for l in plan["limites"]) + ".")
    resumen, destacados = summarize(chat, plan, kept)
    result.update(resultados=kept, resumen=resumen, destacados=destacados, informe="", fuentes=[])
    return result


# ---------- ejecucion en segundo plano (una por usuario) ----------

_runs: dict[str, dict] = {}
_runs_lock = threading.Lock()


def status(user_id: str) -> dict:
    with _runs_lock:
        return dict(_runs.get(user_id) or {"running": False})


def cancel(user_id: str) -> None:
    with _runs_lock:
        if user_id in _runs:
            _runs[user_id]["cancel"] = True


def full_request(consulta: str, previous: dict | None, afinar: str) -> str:
    """Al afinar se suma a lo que ya se pidio: el contexto no se pierde."""
    if not previous:
        return consulta
    return f"{previous['consulta']}\nAdemas: {afinar}"


def start(user_id: str, dek: bytes, key_generation: int, consulta: str, chat: ChatFn,
          afinar_de: str | None = None, before: Callable[[], None] | None = None,
          browser_factory=Browser) -> None:
    """Lanza la busqueda en un hilo. Con afinar_de (id de una busqueda
    anterior), `consulta` es lo que se añade a aquella. Lanza ValueError si
    falta algo o si ya hay una en marcha."""
    consulta = (consulta or "").strip()[:1000]
    if not consulta:
        raise ValueError("Escribe que quieres buscar.")
    previous = None
    if afinar_de:
        previous = next((h for h in get_history(user_id, dek, key_generation) if h["id"] == afinar_de), None)
        if previous is None:
            raise ValueError("Esa busqueda ya no esta en el historial.")
    request = full_request(consulta, previous, consulta)
    with _runs_lock:
        if (_runs.get(user_id) or {}).get("running"):
            raise ValueError("Ya hay una busqueda en marcha.")
        state = {"running": True, "cancel": False, "text": "Empezando…", "done": 0, "total": 0,
                 "started": time.time(), "error": None}
        _runs[user_id] = state

    def progress(text: str, done: int, total: int) -> None:
        state.update(text=text, done=done, total=total)

    def work():
        try:
            if before:
                before()
            with browser_factory() as browser:
                result = run_search(chat, browser, request, progress, lambda: state["cancel"])
            entry = {"id": uuid.uuid4().hex[:10], "fecha": time.time(), "consulta": request, **result}
            history = [entry] + get_history(user_id, dek, key_generation)
            _store.save("deep_history", history[:MAX_HISTORY], user_id, dek, key_generation)
            state.update(last_id=entry["id"])
        except Cancelled:
            state.update(error="Busqueda cancelada.")
        except Exception as exc:
            state.update(error=f"La busqueda fallo: {exc}")
        finally:
            state.update(running=False)

    threading.Thread(target=work, daemon=True).start()
