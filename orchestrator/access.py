"""Quien puede hacer que (auditoria 2026-09-29). Lo usan el middleware de
main.py y las rutas; aparte para que no dependan unos de otros.

- Invitado: lista cerrada de rutas (chat, imagen, video, voz). Antes se
  bloqueaba endpoint a endpoint y uno nuevo quedaba abierto si se olvidaba.
- "Puede usar el ordenador" (agente, leer archivos, ejecutar codigo): el
  admin siempre, invitados nunca, el resto si el admin se lo activa.
"""

from fastapi import Request
from fastapi.responses import JSONResponse

import auth_sessions
import users

SESSION_COOKIE = "chati_session"

GUEST_PATHS = {
    "/auth/me", "/auth/logout", "/chat", "/chat/stream", "/cancel", "/voice_chat", "/speak",
    "/image_with_face", "/video_with_face", "/image_with_controlnet", "/image/upscale", "/image/inpaint",
    "/models/status", "/models/profiles", "/models/roles", "/models/prepare", "/models/image",
    "/models/video", "/work/pending",
}
GUEST_PATH_PREFIXES = ("/plan/", "/media/")
# Tocan el ordenador de verdad (agente, proyectos de codigo)
PC_PATH_PREFIXES = ("/agent/", "/code/", "/work/agent/")


def session_token(request: Request) -> str | None:
    """La pagina usa una cookie HttpOnly (el JavaScript no la ve: un script
    colado no puede llevarsela). Antes iba en localStorage y en la URL de cada
    imagen (?session=...), que acababa en el historial del navegador. La
    cabecera queda para programas y tests."""
    return request.headers.get("X-Session-Token") or request.cookies.get(SESSION_COOKIE)


def current_session(request: Request) -> dict | None:
    token = session_token(request)
    return auth_sessions.get_session(token) if token else None


def guest_may_use(path: str) -> bool:
    return path in GUEST_PATHS or path.startswith(GUEST_PATH_PREFIXES)


def can_use_pc(session: dict) -> bool:
    """Con los permisos de Windows de quien arranco Chati: cualquier usuario o
    invitado leia el Escritorio del dueño y podia ejecutar codigo."""
    if session.get("role") == "admin":
        return True
    return session.get("role") != "guest" and users.can_use_pc(session.get("username"))


def guest_blocked() -> JSONResponse:
    """Modo invitado: chat/imagen/video/voz, pero sin galeria de caras, sin
    documentos, sin CV - ver ROADMAP.md, punto 0."""
    return JSONResponse({"detail": "No disponible en modo invitado."}, status_code=403)


def pc_blocked() -> JSONResponse:
    return JSONResponse({"detail": "Tu cuenta no tiene permiso para usar el ordenador (agente, archivos, "
                                   "ejecutar codigo). Pideselo al administrador."}, status_code=403)
