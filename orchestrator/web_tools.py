"""Piezas comunes de las apps que navegan por internet (Mis apps: buscador de
empleo y compras): el navegador, la busqueda en Bing y el guardado cifrado de
preferencias y resultados.

Navegador: Edge con la ventana fuera de la pantalla, via Playwright. Medido el
2026-09-28: en modo invisible (headless) InfoJobs pedia "¿eres humano o un
robot?" e Indeed bloqueaba con Cloudflare; con ventana normal, ninguno.
Google bloquea siempre a los navegadores automatizados; Bing no."""

import base64
import ipaddress
import json
import re
import socket
import time
from typing import Callable
from urllib.parse import parse_qs, quote_plus, urlparse

import crypto_utils
import profile_store
import atomic

BING = "https://www.bing.com/search?setlang=es&cc=es&q={q}"

# El navegador de las apps abre webs cualquiera y ejecuta su JavaScript: una
# pagina maliciosa podia atacar desde ahi a los servicios de este ordenador
# (ComfyUI no tiene contraseña) o a la red de casa (auditoria 2026-09-29).
# (es privado, cuando se comprobo). Con caducidad: guardado para siempre, un
# dominio que primero apunta a internet y luego a 192.168.x (DNS rebinding)
# seguia pasando como publico (auditoria 2026-10-05).
_host_cache: dict[str, tuple[bool, float]] = {}
_HOST_CACHE_SECONDS = 30


def _ip_is_private(ip) -> bool:
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified \
        or ip.is_multicast


def is_private_host(host: str | None) -> bool:
    """localhost, 127.x, 192.168.x, 10.x, fe80::... o un nombre que apunte ahi."""
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".lan", ".internal")):
        return True
    try:
        return _ip_is_private(ipaddress.ip_address(host))
    except ValueError:
        pass
    cached = _host_cache.get(host)
    if cached is None or time.monotonic() - cached[1] > _HOST_CACHE_SECONDS:
        try:
            private = any(_ip_is_private(ipaddress.ip_address(info[4][0].split("%")[0]))
                          for info in socket.getaddrinfo(host, None))
        except (OSError, ValueError):
            private = False  # no resuelve: el navegador tampoco podra
        cached = _host_cache[host] = (private, time.monotonic())
    return cached[0]


def _guard(route) -> None:
    if is_private_host(urlparse(route.request.url).hostname):
        route.abort("blockedbyclient")
    else:
        route.continue_()

ChatFn = Callable[[str], str]  # prompt -> respuesta del modelo


def json_from(raw: str) -> dict:
    """El primer objeto JSON de una respuesta del modelo ({} si no hay)."""
    try:
        return json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
    except ValueError:
        return {}


def site_label(url: str) -> str:
    host = urlparse(url if "//" in url else "https://" + url).netloc or url
    return host.removeprefix("www.")


# ---------- guardado cifrado (con la contraseña del usuario) ----------

class EncryptedStore:
    """Archivos <nombre>.json.enc en la carpeta del perfil del usuario. `meta`
    guarda con que generacion de clave se cifro: tras restablecer la
    contraseña (DEK nueva) lo viejo no se puede leer y se trata como vacio."""

    def __init__(self, meta_name: str):
        self.meta_name = meta_name

    def load(self, name: str, user_id: str, dek: bytes, key_generation: int, default):
        d = profile_store._user_dir(user_id)
        path, meta = d / f"{name}.json.enc", d / self.meta_name
        if not path.exists() or not meta.exists():
            return default
        try:
            if json.loads(meta.read_text(encoding="utf-8")).get("key_generation") != key_generation:
                return default
        except ValueError:
            return default
        raw = crypto_utils.decrypt_bytes(dek, path.read_bytes())
        return json.loads(raw.decode("utf-8")) if raw else default

    def save(self, name: str, data, user_id: str, dek: bytes, key_generation: int) -> None:
        d = profile_store._user_dir(user_id)
        d.mkdir(parents=True, exist_ok=True)
        atomic.write_bytes(d / f"{name}.json.enc",
                           crypto_utils.encrypt_bytes(dek, json.dumps(data, ensure_ascii=False).encode("utf-8")))
        atomic.write_text(d / self.meta_name, json.dumps({"key_generation": key_generation}))


# ---------- el navegador ----------

def unbing(href: str) -> str:
    """bing.com/ck/a?...&u=a1<base64 de la url>... -> la url real."""
    if "bing.com/ck/a" not in href:
        return href
    u = parse_qs(urlparse(href).query).get("u", [""])[0]
    if not u.startswith("a1"):
        return href
    try:
        raw = u[2:]
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return href


class Blocked(Exception):
    """La web ha pedido demostrar que no somos un robot."""


_BLOCK_PAGE = re.compile(r"eres humano o un robot|not a robot|are you a human|captcha|cloudflare|"
                         r"verificaci[oó]n adicional|tr[aá]fico inusual|unusual traffic", re.I)


def _as_list(x) -> list:
    return x if isinstance(x, list) else [x] if x else []


def product_data(ld_blocks: list[str]) -> dict:
    """Precio, moneda y valoracion de los datos schema.org (JSON-LD) que las
    tiendas ponen en la pagina para los buscadores. {} si no hay Product."""
    found: dict = {}

    def visit(node):
        if isinstance(node, list):
            for n in node:
                visit(n)
            return
        if not isinstance(node, dict) or found:
            return
        types = [t.lower() for t in _as_list(node.get("@type")) if isinstance(t, str)]
        if "product" in types:
            offers = _as_list(node.get("offers"))
            offer = offers[0] if offers and isinstance(offers[0], dict) else {}
            price = offer.get("price") or offer.get("lowPrice")
            rating = node.get("aggregateRating") if isinstance(node.get("aggregateRating"), dict) else {}
            try:
                found.update(
                    nombre=str(node.get("name") or "")[:150],
                    precio=float(str(price).replace(",", ".")) if price not in (None, "") else None,
                    moneda=str(offer.get("priceCurrency") or ""),
                    valoracion=float(rating["ratingValue"]) if rating.get("ratingValue") else None,
                    opiniones=int(float(rating.get("reviewCount") or rating.get("ratingCount") or 0)) or None,
                    disponible=("instock" in str(offer.get("availability", "")).lower())
                    if offer.get("availability") else None,
                )
            except (TypeError, ValueError):
                found.clear()
            return
        for key in ("@graph", "mainEntity", "itemListElement"):
            if key in node:
                visit(node[key])

    for block in ld_blocks:
        try:
            visit(json.loads(block))
        except ValueError:
            continue
        if found:
            break
    return found


class Browser:
    """fetch(url) -> (url final, texto visible, [(texto, href)]);
    fetch_page(url) -> lo mismo en un dict, mas la imagen principal y los
    datos de producto (schema.org) si la pagina los tiene."""

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        args = ["--window-position=-32000,-32000", "--window-size=1280,900",
                "--disable-blink-features=AutomationControlled"]
        try:
            self._browser = self._pw.chromium.launch(channel="msedge", headless=False, args=args)
        except Exception:
            self._browser = self._pw.chromium.launch(headless=False, args=args)  # sin Edge: el de Playwright
        self._ctx = self._browser.new_context(locale="es-ES", viewport={"width": 1280, "height": 900})
        self._ctx.route("**/*", _guard)
        return self

    def __exit__(self, *exc):
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    def fetch_page(self, url: str) -> dict:
        page = self._ctx.new_page()
        try:
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)  # muchas webs pintan el contenido con JavaScript
            text = re.sub(r"\s+\n", "\n", page.inner_text("body"))
            if _BLOCK_PAGE.search(text[:1500]):
                raise Blocked(url)
            links = page.eval_on_selector_all(
                "a[href]", "els => els.map(e => [e.innerText.trim().replace(/\\s+/g, ' '), e.href])")
            image = page.eval_on_selector_all(
                'meta[property="og:image"]', "els => els.map(e => e.content)")
            if not image:  # Amazon y Fnac no la declaran: la foto principal, o la primera grande
                image = page.eval_on_selector_all(
                    "#landingImage, #imgBlkFront, img[itemprop=image]", "els => els.map(e => e.currentSrc || e.src)")
            if not image:
                image = page.eval_on_selector_all(
                    "img", "els => els.filter(e => e.naturalWidth >= 250 && e.naturalHeight >= 250"
                           " && e.naturalWidth < e.naturalHeight * 3).map(e => e.currentSrc || e.src)")
            ld = page.eval_on_selector_all(
                'script[type="application/ld+json"]', "els => els.map(e => e.textContent)")
            return {"url": page.url, "text": text,
                    "links": [(t, h) for t, h in links if h.startswith("http")],
                    "image": next((i for i in image if i and i.startswith("http")), None),
                    "product": product_data(ld)}
        finally:
            page.close()

    def fetch(self, url: str) -> tuple[str, str, list[tuple[str, str]]]:
        p = self.fetch_page(url)
        return p["url"], p["text"], p["links"]

    def bing(self, query: str, n: int) -> list[str]:
        page = self._ctx.new_page()
        try:
            page.goto(BING.format(q=quote_plus(query)), timeout=30000, wait_until="domcontentloaded")
            page.wait_for_timeout(1500)
            hrefs = page.eval_on_selector_all("li.b_algo h2 a", "els => els.map(e => e.href)")
            return [unbing(h) for h in hrefs if h.startswith("http")][:n]
        finally:
            page.close()
