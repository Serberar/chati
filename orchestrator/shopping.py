"""Compras (Mis apps, pedido por Sergio el 2026-09-29): le describes lo que
quieres, o le pasas una foto, y busca por todo internet, lee cada producto y
te da la lista ordenada por precio con 2-3 recomendadas explicando por que.
No compra nada: el enlace lleva a la tienda.

Como el buscador de empleo (job_search.py): el recorrido lo lleva el codigo y
el modelo solo decide lo que necesita criterio (que buscar, que enlaces son
productos, que dice cada pagina, cuales recomendar). El precio lo lee el
modelo del texto de la pagina; los datos de producto (schema.org) que ponen
las tiendas son solo una pista: medido el 2026-09-29, PcComponentes decia
1,21 EUR en ellos (la cuota de financiacion) para unos auriculares de 199."""

import logging
import statistics
import threading
import time
import uuid
from typing import Callable

from web_tools import Blocked, Browser, ChatFn, EncryptedStore, json_from, site_label

MAX_HISTORY = 10
PAGE_MAX_CHARS = 6000
MAX_LINKS_TO_MODEL = 120
MIN_MATCH = 6            # 0-10: por debajo, no es lo que se pidio
MIN_RATING = 3.5         # con "priorizar valoraciones", las peores se quitan
SUSPICIOUS_RATIO = 0.4   # por debajo del 40% de la mediana: precio sospechoso
MISREAD_RATIO = 3        # por encima de 3 veces la mediana: precio mal leido
EXACT_MATCH = 9          # "es exactamente eso": solo estas se recomiendan si hay bastantes
# webs que salen en Bing pero no venden nada
_NOT_SHOPS = ("youtube.", "wikipedia.", "reddit.", "facebook.", "instagram.", "tiktok.", "x.com", "twitter.")

_store = EncryptedStore("shopping_meta.json")


def get_history(user_id: str, dek: bytes, key_generation: int) -> list[dict]:
    return _store.load("shopping_history", user_id, dek, key_generation, [])


def clean_request(req: dict) -> dict:
    """Valida lo que manda la interfaz. Lanza ValueError si algo no vale."""
    def money(x):
        if x in (None, ""):
            return None
        try:
            v = float(str(x).replace(",", "."))
        except ValueError:
            raise ValueError(f'"{x}" no es un precio.') from None
        if v < 0:
            raise ValueError("El precio no puede ser negativo.")
        return v

    clean = {
        "descripcion": str(req.get("descripcion") or "").strip()[:500],
        "precio_min": money(req.get("precio_min")),
        "precio_max": money(req.get("precio_max")),
        "solo_espana": bool(req.get("solo_espana", True)),
        "valoraciones": bool(req.get("valoraciones", True)),
        "max_resultados": max(5, min(30, int(req.get("max_resultados") or 15))),
    }
    if clean["precio_min"] is not None and clean["precio_max"] is not None \
            and clean["precio_min"] > clean["precio_max"]:
        raise ValueError("El precio minimo es mayor que el maximo.")
    return clean


# ---------- el modelo ----------

IMAGE_PROMPT = ("Describe el producto que se ve en esta foto para buscarlo en tiendas online: que es, marca y "
                "modelo si se ven, color y caracteristicas importantes. Una o dos frases, en español.")

QUERIES_PROMPT = """Alguien quiere comprar esto: {what}
Prepara la busqueda en tiendas online de España. Responde SOLO con JSON:
{{"producto": "nombre corto de lo que busca",
  "busquedas": ["2 o 3 busquedas distintas, como las escribiria alguien en un buscador para encontrarlo a la venta"],
  "claves": ["2 a 4 caracteristicas imprescindibles para que sea lo que pide"]}}"""


def plan_queries(chat: ChatFn, what: str) -> dict:
    data = json_from(chat(QUERIES_PROMPT.format(what=what)))
    queries = [str(q).strip() for q in data.get("busquedas", []) if str(q).strip()][:3] or [what[:120]]
    return {"producto": str(data.get("producto") or what).strip()[:120], "busquedas": queries,
            "claves": [str(c).strip() for c in data.get("claves", []) if str(c).strip()][:4]}


PICK_PROMPT = """Esta es una pagina con varios productos (tienda, listado o comparador de precios).
Se busca: {producto}. Enlaces de la pagina (numero: texto):
{links}

¿Cuales llevan a UN producto concreto a la venta que coincida con lo que se busca (en una tienda o
en la oferta de una tienda)? No elijas categorias, filtros, login, ayuda ni otros productos.
Responde SOLO con JSON: {{"productos": [numeros]}} (como mucho {n})."""


def pick_product_links(chat: ChatFn, producto: str, links: list[tuple[str, str]], n: int) -> list[str]:
    candidates = [(t, h) for t, h in links if 6 <= len(t) <= 200][:MAX_LINKS_TO_MODEL]
    if not candidates:
        return []
    listing = "\n".join(f"{i}: {t[:100]}" for i, (t, _h) in enumerate(candidates))
    data = json_from(chat(PICK_PROMPT.format(producto=producto, links=listing, n=n)))
    picked = []
    for i in data.get("productos", []):
        try:
            href = candidates[int(i)][1]
        except (ValueError, IndexError, TypeError):
            continue
        if href not in picked:
            picked.append(href)
    return picked[:n]


EVAL_PROMPT = """Se busca: {producto}. Caracteristicas imprescindibles: {claves}.

Texto de una pagina web ({url}):
---
{page}
---
{hint}
Responde SOLO con JSON:
{{"tipo": "producto" si la pagina vende UN producto concreto; "listado" si muestra varios productos o es un comparador de precios; "otra" si no vende nada (noticia, analisis, foro...),
  "nombre": "nombre del producto", "tienda": "nombre de la tienda",
  "precio": precio final en numero (sin simbolo; el precio de compra, no una cuota mensual) o null si no aparece,
  "moneda": "EUR" u otra,
  "envio_espana": "si" | "no" | "desconocido",
  "coste_envio": numero, 0 si es gratis, o null si no se sabe,
  "estado": "nuevo" | "usado" | "reacondicionado",
  "disponible": true | false,
  "valoracion": nota de 0 a 5 o null, "opiniones": numero de opiniones o null,
  "encaje": 0-10, cuanto coincide con lo que se busca (10 = exactamente eso)}}"""


def _num(x):
    if x in (None, "", "null"):
        return None
    try:
        return float(str(x).replace("€", "").replace(",", ".").strip())
    except ValueError:
        return None


def evaluate_page(chat: ChatFn, plan: dict, page: dict) -> dict | None:
    """El producto leido de la pagina; {"tipo": "listado"} si es una lista o
    un comparador; None si no vende nada."""
    product = page.get("product") or {}
    hint = ""
    if product:
        hint = ("Datos que la pagina declara para los buscadores (pueden estar mal; manda lo que diga el "
                f"texto): {product}\n")
    data = json_from(chat(EVAL_PROMPT.format(
        producto=plan["producto"], claves=", ".join(plan["claves"]) or "-", url=page["url"],
        page=page["text"][:PAGE_MAX_CHARS], hint=hint)))
    kind = str(data.get("tipo", "")).strip().lower()
    if kind == "listado":
        return {"tipo": "listado"}
    if kind != "producto":
        return None
    try:
        match = max(0, min(10, int(data.get("encaje", 0))))
    except (TypeError, ValueError):
        match = 0
    rating = _num(data.get("valoracion"))
    if rating is None and product.get("valoracion") is not None:
        rating = round(product["valoracion"], 1)
    opinions = _num(data.get("opiniones")) or product.get("opiniones")
    return {
        "url": page["url"],
        "nombre": str(data.get("nombre") or product.get("nombre") or "Producto").strip()[:150],
        "tienda": str(data.get("tienda") or "").strip()[:60] or site_label(page["url"]),
        "precio": _num(data.get("precio")),
        "moneda": str(data.get("moneda") or product.get("moneda") or "EUR").strip().upper()[:5],
        "envio_espana": str(data.get("envio_espana", "desconocido")).strip().lower(),
        "coste_envio": _num(data.get("coste_envio")),
        "estado": str(data.get("estado") or "nuevo").strip().lower(),
        "disponible": data.get("disponible") is not False and product.get("disponible") is not False,
        "valoracion": rating if rating is None or 0 <= rating <= 5 else None,
        "opiniones": int(opinions) if opinions else None,
        "encaje": match,
        "imagen": page.get("image"),
    }


RECOMMEND_PROMPT = """Alguien quiere comprar: {producto}.
{priorizar}
Estas son las ofertas encontradas (n: nombre | tienda | precio | envio | valoracion | estado):
{table}

Elige las 2 o 3 que le recomendarias (por ejemplo la mas barata que sea de fiar y la de mejor relacion
calidad-precio), evitando las marcadas como SOSPECHOSO o AGOTADO. Prefiere tiendas conocidas y de fiar
antes que vendedores desconocidos de marketplaces (AliExpress, Wish...), salvo que la diferencia de precio
compense de verdad. Responde SOLO con JSON:
{{"recomendadas": [{{"n": numero, "por_que": "una frase"}}]}}"""


def recommend(chat: ChatFn, plan: dict, offers: list[dict], prioritize_ratings: bool) -> list[dict]:
    # solo lo que es exactamente lo pedido (una imitacion barata con encaje 8
    # salia recomendada el 2026-09-29), si hay al menos dos asi
    exact = [o for o in offers if o["encaje"] >= EXACT_MATCH]
    offers = exact if len(exact) >= 2 else offers
    if not offers:
        return []
    rows = []
    for i, o in enumerate(offers):
        flags = " SOSPECHOSO" if o.get("sospechoso") else ""
        flags += " AGOTADO" if not o["disponible"] else ""
        rating = f"{o['valoracion']}/5 ({o['opiniones'] or '?'} opiniones)" if o["valoracion"] else "sin datos"
        rows.append(f"{i}: {o['nombre'][:70]} | {o['tienda']} | {o['precio']} {o['moneda']} | "
                    f"{o['envio_espana']} | {rating} | {o['estado']}{flags}")
    priorizar = "Da mucha importancia a las buenas valoraciones." if prioritize_ratings else ""
    data = json_from(chat(RECOMMEND_PROMPT.format(producto=plan["producto"], priorizar=priorizar,
                                                   table="\n".join(rows))))
    picked, seen = [], set()
    for r in data.get("recomendadas", []):
        try:
            i = int(r.get("n"))
        except (TypeError, ValueError, AttributeError):
            continue
        if 0 <= i < len(offers) and i not in seen:
            seen.add(i)
            picked.append({"url": offers[i]["url"], "por_que": str(r.get("por_que", "")).strip()[:300]})
    return picked[:3]


# ---------- la busqueda completa ----------

class Cancelled(Exception):
    pass


def total_price(o: dict) -> float:
    return (o["precio"] or 0) + (o["coste_envio"] or 0)


def fix_misread_prices(offers: list[dict]) -> None:
    """Amazon y otras pintan el precio como "249€00": el modelo lo leia como
    24900 (medido el 2026-09-29). Si un precio multiplica la mediana y
    dividido entre 100 queda normal, se corrige; si no, se deja sin precio
    (se descarta: mejor no mostrar que mostrar un precio falso)."""
    eur = [o["precio"] for o in offers if o["precio"] and o["moneda"] == "EUR" and o["encaje"] >= MIN_MATCH]
    if len(eur) < 3:
        return
    median = statistics.median(eur)
    for o in offers:
        if o["precio"] and o["moneda"] == "EUR" and o["precio"] > median * MISREAD_RATIO:
            fixed = o["precio"] / 100
            o["precio"] = fixed if median * 0.5 <= fixed <= median * 2 else None


def apply_filters(offers: list[dict], req: dict) -> list[dict]:
    """Quita lo que no pidio el usuario, marca los precios sospechosos y
    ordena por precio final (con envio cuando se sabe)."""
    fix_misread_prices(offers)
    kept = []
    for o in offers:
        if o["precio"] is None or o["encaje"] < MIN_MATCH:
            continue
        if req["precio_min"] is not None and o["precio"] < req["precio_min"]:
            continue
        if req["precio_max"] is not None and o["precio"] > req["precio_max"]:
            continue
        if req["solo_espana"] and o["envio_espana"] == "no":
            continue
        if req["valoraciones"] and o["valoracion"] is not None and o["valoracion"] < MIN_RATING:
            continue
        kept.append(o)
    eur = [o["precio"] for o in kept if o["moneda"] == "EUR"]
    median = statistics.median(eur) if len(eur) >= 3 else None
    for o in kept:
        o["sospechoso"] = bool(median and o["moneda"] == "EUR" and o["precio"] < median * SUSPICIOUS_RATIO)
    kept.sort(key=lambda o: (not o["disponible"], total_price(o)))
    return kept


def run_search(chat: ChatFn, browser, req: dict, progress: Callable[[str, int, int], None],
               cancelled: Callable[[], bool], image_description: str = "") -> dict:
    def check():
        if cancelled():
            raise Cancelled()

    what = " ".join(x for x in (req["descripcion"], image_description) if x).strip()
    if not what:
        raise ValueError("Describe lo que buscas o añade una foto.")
    progress("Pensando que buscar…", 0, 0)
    plan = plan_queries(chat, what)
    warnings: list[str] = []

    candidates: list[str] = []
    for i, query in enumerate(plan["busquedas"]):
        check()
        progress(f"Buscando «{query}»…", i, len(plan["busquedas"]))
        try:
            found = browser.bing(f"{query} comprar precio", 10)
        except Exception:
            found = []
        for url in found:
            if url not in candidates and not any(bad in url for bad in _NOT_SHOPS):
                candidates.append(url)
    if not candidates:
        warnings.append("Bing no devolvio resultados; prueba a describirlo de otra forma.")

    max_checks = req["max_resultados"] + 10
    queue, pos, offers, seen_urls, blocked = candidates[:max_checks], 0, [], set(), set()
    while pos < len(queue) and pos < max_checks + 10 and len(offers) < req["max_resultados"]:
        url = queue[pos]
        pos += 1
        check()
        progress(f"Mirando tiendas ({pos} de {len(queue)})…", pos - 1, len(queue))
        try:
            page = browser.fetch_page(url)
            item = evaluate_page(chat, plan, page)
        except Cancelled:
            raise
        except Blocked:
            blocked.add(site_label(url))
            continue
        except Exception:
            continue
        if item and item.get("tipo") == "listado":
            known = set(queue)
            queue += [h for h in pick_product_links(chat, plan["producto"], page["links"], 4) if h not in known]
            continue
        if item and item["url"] not in seen_urls:
            seen_urls.add(item["url"])
            offers.append(item)
    if blocked:
        warnings.append("No dejaron mirar: " + ", ".join(sorted(blocked)) + ".")

    check()
    progress("Comparando y eligiendo las mejores…", 0, 0)
    kept = apply_filters(offers, req)
    if offers and not kept:
        warnings.append("Se encontraron productos, pero ninguno cumple los filtros.")
    recommended = recommend(chat, plan, kept, req["valoraciones"])
    return {"producto": plan["producto"], "busquedas": plan["busquedas"], "descripcion_foto": image_description,
            "ofertas": kept, "recomendadas": recommended, "avisos": warnings,
            "revisadas": len(offers)}


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


def start(user_id: str, dek: bytes, key_generation: int, req: dict, chat: ChatFn,
          describe_image: Callable[[str], str] | None = None, image_b64: str | None = None,
          before: Callable[[], None] | None = None, browser_factory=Browser) -> None:
    """Lanza la busqueda en un hilo. Lanza ValueError si falta algo o si ya
    hay una en marcha."""
    req = clean_request(req)
    if not req["descripcion"] and not image_b64:
        raise ValueError("Describe lo que buscas o añade una foto.")
    with _runs_lock:
        if (_runs.get(user_id) or {}).get("running"):
            raise ValueError("Ya hay una busqueda en marcha.")
        state = {"running": True, "cancel": False, "text": "Empezando…", "done": 0, "total": 0,
                 "started": time.time(), "error": None, "found": None}
        _runs[user_id] = state

    def progress(text: str, done: int, total: int) -> None:
        state.update(text=text, done=done, total=total)

    def work():
        try:
            if before:
                before()
            description = ""
            if image_b64 and describe_image:
                progress("Mirando la foto…", 0, 0)
                description = describe_image(image_b64).strip()
            with browser_factory() as browser:
                result = run_search(chat, browser, req, progress, lambda: state["cancel"], description)
            entry = {"id": uuid.uuid4().hex[:10], "fecha": time.time(), "peticion": req, **result}
            history = [entry] + get_history(user_id, dek, key_generation)
            _store.save("shopping_history", history[:MAX_HISTORY], user_id, dek, key_generation)
            state.update(found=len(result["ofertas"]), last_id=entry["id"])
        except Cancelled:
            state.update(error="Busqueda cancelada.")
        except Exception as exc:
            logging.getLogger("chati").exception("Busqueda de compras fallida")
            state.update(error=f"La busqueda fallo: {exc}")
        finally:
            state.update(running=False)

    threading.Thread(target=work, daemon=True).start()
