"""Protecciones de la puerta de entrada del orquestador (auditoria 2026-09-29).

- Solo se aceptan peticiones dirigidas a localhost / 127.0.0.1: una web
  maliciosa puede hacer que su dominio apunte a 127.0.0.1 (DNS rebinding) y el
  navegador le dejaria hablar con Chati; entonces la cabecera Host lleva SU
  dominio. Comprobado en vivo: con "Host: evil.example" se obtenia un token de
  invitado.
- Las peticiones que cambian algo (POST, PUT, DELETE...) con cabecera Origin
  tienen que venir de la propia pagina de Chati (mismo host y puerto).
- Cabeceras de seguridad en cada respuesta (CSP, no incrustar en iframes...).
- Desde fuera (el movil por Tailscale, 2026-10-07): solo por las direcciones
  de REMOTE_HOSTS (config.yaml, remote.hosts) y solo un dispositivo vinculado
  (devices.py). Sin llave, lo unico que existe es la pagina para vincularlo.
"""

import logging

import devices

log = logging.getLogger("chati")

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, RedirectResponse

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
# las del ordenador en Tailscale (p.ej. "msi.tailcdce77.ts.net"); las pone main.py
REMOTE_HOSTS: set[str] = set()
# lo unico que ve un dispositivo sin vincular
PAIR_PATHS = {"/pair", "/pair/claim", "/static/js/pair.js", "/favicon.ico", "/client-log"}
# ninguna peticion legitima pasa de esto (10 adjuntos del agente de 20 MB);
# sin tope, un cuerpo de varios GB se leia entero en memoria
MAX_BODY_BYTES = 250 * 1024 * 1024
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Todo el JS y el CSS se sirven desde /static (sin scripts en linea); las
# imagenes y videos son del propio orquestador o blob: (vistas previas);
# las fotos de productos/pisos de las apps vienen de las tiendas (https:).
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; "
    "media-src 'self' blob:; "
    "connect-src 'self'; "
    "object-src 'none'; "
    "base-uri 'none'; "
    "form-action 'self'; "
    "frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def host_name(host_header: str) -> str:
    """"127.0.0.1:8899" -> "127.0.0.1", "[::1]:8899" -> "[::1]"."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if ":" in host else host


def is_local_host(host_header: str) -> bool:
    return host_name(host_header) in LOCAL_HOSTS


def is_remote_host(host_header: str) -> bool:
    return host_name(host_header) in REMOTE_HOSTS


def origin_allowed(origin: str | None, host_header: str) -> bool:
    """Sin Origin (curl, OpenCode, el vigilante) vale: el navegador siempre lo
    manda en peticiones que cambian algo. Con Origin, tiene que ser la propia
    pagina: mismo host y puerto que la peticion (por https si es de fuera)."""
    if origin is None:
        return True
    origin = origin.strip().lower()
    host = host_header.strip().lower()
    if is_remote_host(host_header):
        return origin in (f"https://{host}", f"https://{host_name(host_header)}")
    return origin in (f"http://{host}",) and is_local_host(host_header)


def _rejected(request, status: int, why: str, where: str) -> JSONResponse:
    log.warning("Rechazada %s %s (%d, %s) desde %s", request.method, request.url.path, status, why, where)
    return JSONResponse({"detail": why}, status_code=status)


# respuestas de error que no merecen ir a errores.log: sesion caducada (la
# interfaz vuelve a pedir entrar) y lo que no existe
_QUIET_STATUSES = {401, 404}


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        host = request.headers.get("host", "")
        request.state.device = None
        if is_remote_host(host):
            device = devices.verify(request.cookies.get(devices.COOKIE_NAME))
            where = f"fuera ({device['name']})" if device else "fuera (sin vincular)"
            if device is None and request.url.path not in PAIR_PATHS:
                if request.method == "GET" and request.url.path == "/":
                    return RedirectResponse("/pair", status_code=303)
                return _rejected(request, 403, "Este dispositivo no esta vinculado a Chati.", where)
            request.state.device = device
        elif not is_local_host(host):
            return _rejected(request, 400, "Solo se aceptan conexiones a localhost.", f"host {host_name(host)!r}")
        else:
            where = "el ordenador"
        if request.method not in SAFE_METHODS and not origin_allowed(request.headers.get("origin"), host):
            return _rejected(request, 403, "Origen no permitido.", where)
        length = request.headers.get("content-length")
        if length and (not length.isdigit() or int(length) > MAX_BODY_BYTES):
            return _rejected(request, 413, "Peticion demasiado grande.", where)
        response = await call_next(request)
        # toda respuesta de error queda apuntada: antes muchas se le decian al
        # usuario y no quedaban en ningun sitio (2026-10-07)
        if response.status_code >= 400:
            level = logging.INFO if response.status_code in _QUIET_STATUSES else logging.WARNING
            log.log(level, "Respuesta %d a %s %s desde %s", response.status_code, request.method,
                    request.url.path, where)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        # la pagina y su JS/CSS: que el navegador pregunte siempre si hay version
        # nueva (si no la hay, 304 y usa la suya). Sin esto Safari en el iPhone
        # se quedo con el JS viejo y el menu ☰ no abria (2026-10-07)
        path = request.url.path
        if path in ("/", "/pair") or path.startswith("/static/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response
