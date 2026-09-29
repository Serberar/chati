"""Protecciones de la puerta de entrada del orquestador (auditoria 2026-09-29).

- Solo se aceptan peticiones dirigidas a localhost / 127.0.0.1: una web
  maliciosa puede hacer que su dominio apunte a 127.0.0.1 (DNS rebinding) y el
  navegador le dejaria hablar con Chati; entonces la cabecera Host lleva SU
  dominio. Comprobado en vivo: con "Host: evil.example" se obtenia un token de
  invitado.
- Las peticiones que cambian algo (POST, PUT, DELETE...) con cabecera Origin
  tienen que venir de la propia pagina de Chati (mismo host y puerto).
- Cabeceras de seguridad en cada respuesta (CSP, no incrustar en iframes...).
"""

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
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


def origin_allowed(origin: str | None, host_header: str) -> bool:
    """Sin Origin (curl, OpenCode, el vigilante) vale: el navegador siempre lo
    manda en peticiones que cambian algo. Con Origin, tiene que ser la propia
    pagina: mismo host y puerto que la peticion."""
    if origin is None:
        return True
    origin = origin.strip().lower()
    return origin in (f"http://{host_header.strip().lower()}",) and is_local_host(host_header)


class LocalOnlyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        host = request.headers.get("host", "")
        if not is_local_host(host):
            return JSONResponse({"detail": "Solo se aceptan conexiones a localhost."}, status_code=400)
        if request.method not in SAFE_METHODS and not origin_allowed(request.headers.get("origin"), host):
            return JSONResponse({"detail": "Origen no permitido."}, status_code=403)
        response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response
