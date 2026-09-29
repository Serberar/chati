"""Buscador de empleo autonomo (roadmap n.º 7, pedido por Sergio el 2026-09-28).

Con un clic ("Buscar ofertas") y sin mas intervencion: lee el CV del
usuario, busca en las webs de empleo que tenga activadas (y, si quiere, en
todo internet), abre cada oferta, la puntua segun el CV y prepara una carta
de presentacion para las mejores. NO se inscribe en nada: eso lo hace el
usuario desde el enlace (decision de Sergio: "buscar y preparar").

El recorrido lo lleva este modulo y el modelo solo decide lo que necesita
criterio (que enlaces son ofertas, cuanto encaja cada una, la carta): con
qwen3:8b manejando el navegador el mismo a lo largo de muchas paginas se
perdia. Navegador: Edge invisible (viene con Windows) via Playwright.
Google bloquea a los navegadores automatizados (comprobado); Bing no."""

import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote_plus

import profile_store
from rag import _extract_text
from web_tools import BING, Blocked, Browser, ChatFn, EncryptedStore, json_from, site_label, unbing

# nombres de antes, usados desde los tests y otros modulos
_json_from, _site_label, _unbing = json_from, site_label, unbing
_store = EncryptedStore("jobs_meta.json")

# {puesto} y {lugar} se rellenan con el perfil de busqueda. Una web sin
# {puesto} (las que añade el usuario, solo con su direccion) se busca con
# Bing: "site:<web> <puesto> <lugar>".
DEFAULT_SITES = [
    {"name": "InfoJobs", "url": "https://www.infojobs.net/jobsearch/search-results/list.xhtml?keyword={puesto}",
     "enabled": True},
    {"name": "Indeed", "url": "https://es.indeed.com/jobs?q={puesto}&l={lugar}", "enabled": True},
    {"name": "LinkedIn", "url": "https://www.linkedin.com/jobs/search/?keywords={puesto}&location={lugar}",
     "enabled": True},
    {"name": "Tecnoempleo", "url": "https://www.tecnoempleo.com/ofertas-trabajo/?te={puesto}", "enabled": False},
]
DEFAULT_PREFS = {"puestos": [], "lugar": "", "preferencias": "", "internet": True,
                 "max_ofertas": 12, "sites": DEFAULT_SITES}
MAX_SITES = 30
LETTERS_FOR_TOP = 3
MIN_SCORE_FOR_LETTER = 60
CV_MAX_CHARS = 6000
PAGE_MAX_CHARS = 7000
MAX_LINKS_TO_MODEL = 120

# ---------- guardado (cifrado, como el resto de datos del perfil) ----------

def _load(name: str, user_id: str, dek: bytes, key_generation: int, default):
    return _store.load(name, user_id, dek, key_generation, default)


def _save(name: str, data, user_id: str, dek: bytes, key_generation: int) -> None:
    _store.save(name, data, user_id, dek, key_generation)


def get_prefs(user_id: str, dek: bytes, key_generation: int) -> dict:
    prefs = {**DEFAULT_PREFS, **_load("jobs_prefs", user_id, dek, key_generation, {})}
    prefs["sites"] = [dict(s) for s in prefs["sites"]]
    return prefs


def clean_prefs(prefs: dict) -> dict:
    """Valida lo que manda la interfaz. Lanza ValueError si algo no vale."""
    puestos = [str(p).strip()[:60] for p in prefs.get("puestos", []) if str(p).strip()][:5]
    sites = []
    for s in prefs.get("sites", [])[:MAX_SITES]:
        url = str(s.get("url", "")).strip()[:300]
        if not url:
            continue
        if not url.startswith("http") and "{puesto}" not in url:
            url = url.removeprefix("www.").strip("/")  # solo la direccion de la web
            if "." not in url or " " in url:
                raise ValueError(f'"{url}" no parece la direccion de una web (p.ej. infoempleo.com).')
        name = str(s.get("name", "")).strip()[:40] or _site_label(url)
        sites.append({"name": name, "url": url, "enabled": bool(s.get("enabled", True))})
    return {
        "puestos": puestos,
        "lugar": str(prefs.get("lugar", "")).strip()[:80],
        "preferencias": str(prefs.get("preferencias", "")).strip()[:600],
        "internet": bool(prefs.get("internet", True)),
        "max_ofertas": max(3, min(30, int(prefs.get("max_ofertas", 12) or 12))),
        "sites": sites,
    }


def save_prefs(prefs: dict, user_id: str, dek: bytes, key_generation: int) -> dict:
    clean = clean_prefs(prefs)
    _save("jobs_prefs", clean, user_id, dek, key_generation)
    return clean


def get_results(user_id: str, dek: bytes, key_generation: int) -> dict:
    return _load("jobs_results", user_id, dek, key_generation, {"offers": [], "run_at": None, "seen": []})


# ---------- el modelo ----------

def cv_text(filename: str, content: bytes) -> str:
    suffix = Path(filename).suffix.lower() or ".txt"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"cv{suffix}"
        path.write_bytes(content)
        text = _extract_text(path)
    return re.sub(r"\n{3,}", "\n\n", text).strip()[:CV_MAX_CHARS]


PROFILE_PROMPT = """Este es el CV de una persona que busca trabajo:
---
{cv}
---
Propon como buscarle ofertas. Responde SOLO con JSON:
{{"puestos": ["2 o 3 nombres de puesto cortos, como los escribiria en un buscador de empleo"],
  "lugar": "ciudad o provincia donde vive segun el CV, o vacio si no aparece"}}"""


def suggest_profile(chat: ChatFn, cv: str) -> dict:
    data = _json_from(chat(PROFILE_PROMPT.format(cv=cv)))
    puestos = [str(p).strip() for p in data.get("puestos", []) if str(p).strip()][:3]
    return {"puestos": puestos, "lugar": str(data.get("lugar", "")).strip()}


PICK_PROMPT = """Esta es una pagina de resultados de una web de empleo. Enlaces de la pagina (numero: texto):
{links}

¿Cuales llevan a una oferta de trabajo concreta (no a categorias, filtros, login, empresas o ayuda)?
Responde SOLO con JSON: {{"ofertas": [numeros]}} (como mucho {n})."""


def pick_offer_links(chat: ChatFn, links: list[tuple[str, str]], n: int) -> list[str]:
    candidates = [(t, h) for t, h in links if 12 <= len(t) <= 160][:MAX_LINKS_TO_MODEL]
    if not candidates:
        return []
    listing = "\n".join(f"{i}: {t[:90]}" for i, (t, _h) in enumerate(candidates))
    data = _json_from(chat(PICK_PROMPT.format(links=listing, n=n)))
    picked = []
    for i in data.get("ofertas", []):
        try:
            href = candidates[int(i)][1]
        except (ValueError, IndexError, TypeError):
            continue
        if href not in picked:
            picked.append(href)
    return picked[:n]


EVAL_PROMPT = """Eres un orientador laboral. CV de la persona:
---
{cv}
---
Busca: {puestos}. Zona: {lugar}. Preferencias: {prefs}

Texto de una pagina web ({url}):
---
{page}
---
Responde SOLO con JSON:
{{"tipo": "oferta" si la pagina describe UN solo puesto abierto; "listado" si muestra varios puestos distintos (aunque uno salga destacado); "otra" si no es una oferta o esta caducada,
  "titulo": "puesto", "empresa": "empresa o vacio", "ubicacion": "lugar o remoto",
  "encaje": 0-10, cuanto se parecen las funciones del puesto a la experiencia del CV (10 = casi lo mismo, 5 = parecido, 0 = nada que ver),
  "requisitos": "cumple" | "parcial" | "no cumple" (titulacion, años, idiomas, herramientas que pide),
  "zona": "si" (en su zona) | "cerca" (se puede ir a diario) | "remoto" | "no" (lejos),
  "motivos": "una o dos frases: por que encaja y que le falta"}}"""


def evaluate_offer(chat: ChatFn, cv: str, prefs: dict, url: str, page: str) -> dict | None:
    """La oferta puntuada; {"tipo": "listado"} si la pagina es una lista de
    ofertas (pasa con resultados de Bing); None si no es una oferta."""
    data = _json_from(chat(EVAL_PROMPT.format(
        cv=cv, puestos=", ".join(prefs["puestos"]) or "(lo que encaje con el CV)",
        lugar=prefs["lugar"] or "cualquiera", prefs=prefs["preferencias"] or "ninguna",
        url=url, page=page[:PAGE_MAX_CHARS])))
    kind = str(data.get("tipo", "")).strip().lower()
    if kind == "listado":
        return {"tipo": "listado"}
    if kind != "oferta":
        return None
    return {"url": url, "titulo": str(data.get("titulo", "")).strip()[:120] or "Oferta",
            "empresa": str(data.get("empresa", "")).strip()[:80],
            "ubicacion": str(data.get("ubicacion", "")).strip()[:80],
            "puntuacion": score_offer(data), "motivos": str(data.get("motivos", "")).strip()[:400]}


# La nota la calcula el codigo a partir de criterios separados: pidiendo
# directamente "0-100" el modelo daba 85 a casi todo (8 de 8 el 2026-09-28).
_REQ_POINTS = {"cumple": 20, "parcial": 10, "no cumple": 0}
_ZONE_POINTS = {"si": 10, "sí": 10, "remoto": 10, "cerca": 5, "no": -25}


def score_offer(data: dict) -> int:
    try:
        fit = max(0, min(10, int(data.get("encaje", 0))))
    except (TypeError, ValueError):
        fit = 0
    req = _REQ_POINTS.get(str(data.get("requisitos", "")).strip().lower(), 10)
    zone = _ZONE_POINTS.get(str(data.get("zona", "")).strip().lower(), 0)
    return max(0, min(100, fit * 7 + req + zone))


LETTER_PROMPT = """Escribe una carta de presentacion breve (150-220 palabras), en español, en primera
persona, para esta oferta. Usa SOLO datos que esten en el CV: no inventes experiencia, titulos,
cifras ni habilidades que el CV no diga. Respeta el genero con el que se presenta la persona en el
CV. Sin encabezado de direcciones ni fecha; empieza por el saludo.

CV:
---
{cv}
---
Oferta: {titulo} en {empresa}
---
{page}
---"""


def write_letter(chat: ChatFn, cv: str, offer: dict, page: str) -> str:
    return chat(LETTER_PROMPT.format(cv=cv, titulo=offer["titulo"], empresa=offer["empresa"] or "la empresa",
                                     page=page[:4000])).strip()


# ---------- la busqueda completa ----------

class Cancelled(Exception):
    pass


def _search_site(chat: ChatFn, browser, site: dict, puesto: str, lugar: str, n: int) -> list[str]:
    if "{puesto}" in site["url"]:
        url = site["url"].format(puesto=quote_plus(puesto), lugar=quote_plus(lugar))
        _final, _text, links = browser.fetch(url)
        return pick_offer_links(chat, links, n)
    return browser.bing(f"site:{site['url']} oferta {puesto} {lugar}".strip(), n)


def run_search(chat: ChatFn, browser, cv: str, prefs: dict, seen: set[str],
               progress: Callable[[str, int, int], None],
               cancelled: Callable[[], bool]) -> tuple[list[dict], list[str]]:
    """(ofertas de mejor a peor, avisos para el usuario). progress(texto, hechas, total)."""
    warnings: list[str] = []
    def check():
        if cancelled():
            raise Cancelled()

    puestos = prefs["puestos"] or ["empleo"]
    lugar = prefs["lugar"]
    sources = [s for s in prefs["sites"] if s.get("enabled")]
    total_sources = len(sources) + (1 if prefs["internet"] else 0)
    if not total_sources:
        raise ValueError("No hay ninguna web activada ni la busqueda en internet.")
    per_source = max(2, -(-prefs["max_ofertas"] // total_sources))  # redondeo hacia arriba

    candidates: list[tuple[str, str]] = []  # (url, fuente)
    done = 0
    for site in sources:
        check()
        progress(f"Buscando en {site['name']}…", done, total_sources)
        found: list[str] = []
        try:
            for puesto in puestos:
                if len(found) >= per_source:
                    break
                try:
                    found += [h for h in _search_site(chat, browser, site, puesto, lugar, per_source)
                              if h not in found]
                except Blocked:
                    raise
                except Exception:  # un reintento: Bing fallo una vez sin motivo aparente
                    found += [h for h in _search_site(chat, browser, site, puesto, lugar, per_source)
                              if h not in found]
        except Cancelled:
            raise
        except Blocked:
            warnings.append(f"{site['name']} no ha dejado buscar (pide comprobar que no eres un robot).")
        except Exception:
            # una web caida o cambiada no para la busqueda: se sigue con las demas
            warnings.append(f"No se pudo buscar en {site['name']}.")
        if not found and not any(site["name"] in w for w in warnings):
            warnings.append(f"En {site['name']} no se encontraron ofertas.")
        candidates += [(h, site["name"]) for h in found[:per_source]]
        done += 1
    if prefs["internet"]:
        check()
        progress("Buscando en internet…", done, total_sources)
        try:
            for puesto in puestos:
                query = f"oferta de empleo {puesto} {lugar}".strip()
                candidates += [(h, "Internet") for h in browser.bing(query, per_source)]
        except Exception:
            pass

    unique: dict[str, str] = {}
    for url, source in candidates:
        unique.setdefault(url, source)
    to_check = list(unique.items())[:prefs["max_ofertas"] + 6]  # margen: algunas no seran ofertas
    max_checks = len(to_check) + 8  # los listados añaden sus ofertas a la cola

    offers, pages, same = [], {}, set()
    i = 0
    while i < len(to_check) and i < max_checks:
        url, source = to_check[i]
        i += 1
        check()
        if len(offers) >= prefs["max_ofertas"]:
            break
        progress(f"Leyendo ofertas ({i} de {len(to_check)})…", i - 1, len(to_check))
        try:
            final_url, text, links = browser.fetch(url)
            offer = evaluate_offer(chat, cv, prefs, final_url, text)
        except Cancelled:
            raise
        except Exception:
            continue
        if offer and offer.get("tipo") == "listado":
            # un resultado de Bing que es una lista: se miran sus ofertas
            known = {u for u, _s in to_check}
            to_check += [(h, source) for h in pick_offer_links(chat, links, 2) if h not in known]
            continue
        key = (offer["titulo"].lower(), offer["empresa"].lower()) if offer else None
        if offer and offer["url"] not in pages and key not in same:
            same.add(key)
            offer["fuente"] = source
            offer["nueva"] = offer["url"] not in seen
            offer["carta"] = ""
            pages[offer["url"]] = text
            offers.append(offer)

    offers.sort(key=lambda o: o["puntuacion"], reverse=True)
    best = [o for o in offers if o["puntuacion"] >= MIN_SCORE_FOR_LETTER][:LETTERS_FOR_TOP]
    for i, offer in enumerate(best):
        check()
        progress(f"Escribiendo cartas de presentacion ({i + 1} de {len(best)})…", i, len(best))
        try:
            offer["carta"] = write_letter(chat, cv, offer, pages[offer["url"]])
        except Exception:
            pass
    return offers, warnings


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


def start(user_id: str, dek: bytes, key_generation: int, chat: ChatFn,
          before: Callable[[], None] | None = None, browser_factory=Browser) -> None:
    """Lanza la busqueda en un hilo. Lanza ValueError si falta algo (CV) o
    si ya hay una en marcha."""
    filename = profile_store.cv_filename(user_id=user_id)
    content = profile_store.get_cv_bytes(user_id, dek, key_generation)
    if not filename or not content:
        raise ValueError("Primero sube tu CV (pestaña «Mi CV»).")
    with _runs_lock:
        if (_runs.get(user_id) or {}).get("running"):
            raise ValueError("Ya hay una busqueda en marcha.")
        state = {"running": True, "cancel": False, "text": "Leyendo tu CV…", "done": 0, "total": 0,
                 "started": time.time(), "error": None, "found": None}
        _runs[user_id] = state

    def progress(text: str, done: int, total: int) -> None:
        state.update(text=text, done=done, total=total)

    def work():
        try:
            if before:
                before()
            cv = cv_text(filename, content)
            prefs = get_prefs(user_id, dek, key_generation)
            if not prefs["puestos"]:
                # primera vez: el perfil de busqueda sale del CV (luego se puede corregir)
                progress("Pensando que puestos buscarte segun tu CV…", 0, 0)
                suggested = suggest_profile(chat, cv)
                prefs["puestos"] = suggested["puestos"]
                prefs["lugar"] = prefs["lugar"] or suggested["lugar"]
                save_prefs(prefs, user_id, dek, key_generation)
            previous = get_results(user_id, dek, key_generation)
            seen = set(previous.get("seen", []))
            with browser_factory() as browser:
                offers, warnings = run_search(chat, browser, cv, prefs, seen, progress, lambda: state["cancel"])
            seen |= {o["url"] for o in offers}
            _save("jobs_results", {"offers": offers, "warnings": warnings, "run_at": time.time(),
                                   "seen": sorted(seen)[-2000:]}, user_id, dek, key_generation)
            state.update(found=len(offers))
        except Cancelled:
            state.update(error="Busqueda cancelada.")
        except Exception as exc:
            state.update(error=f"La busqueda fallo: {exc}")
        finally:
            state.update(running=False)

    threading.Thread(target=work, daemon=True).start()
