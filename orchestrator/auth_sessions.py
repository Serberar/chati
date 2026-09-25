"""Sesiones de autenticacion activas - viven SOLO en memoria del proceso,
nunca en disco: cada token identifica una sesion y lleva la DEK del usuario
ya desenvuelta, necesaria para cifrar/descifrar sus datos durante toda la
sesion. Se pierden al reiniciar el servidor (hay que volver a iniciar
sesion) - eso es intencional: la DEK en claro no debe sobrevivir en ningun
sitio persistente. Ver ROADMAP.md, punto 0."""

import secrets
import time

from cryptography.fernet import Fernet

SESSION_TTL_SECONDS = 12 * 60 * 60  # 12 horas

_sessions: dict[str, dict] = {}


def create_session(user_id: str, username: str, role: str, dek: bytes, key_generation: int) -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "user_id": user_id, "username": username, "role": role,
        "dek": dek, "key_generation": key_generation,
        "expires_at": time.time() + SESSION_TTL_SECONDS,
    }
    return token


def create_guest_session() -> str:
    """Invitado: DEK efimera aleatoria, sin usuario real detras. Nada de lo
    que cifre con ella sobrevive a esta sesion (ver ROADMAP.md, punto 0:
    modo invitado no guarda historial)."""
    token = secrets.token_urlsafe(32)
    _sessions[token] = {
        "user_id": None, "username": None, "role": "guest",
        "dek": Fernet.generate_key(), "key_generation": 0,
        "expires_at": time.time() + SESSION_TTL_SECONDS,
    }
    return token


def get_session(token: str) -> dict | None:
    session = _sessions.get(token)
    if session is None:
        return None
    if time.time() > session["expires_at"]:
        del _sessions[token]
        return None
    return session


def destroy_session(token: str) -> None:
    _sessions.pop(token, None)


def destroy_all_sessions_for_user(username: str) -> None:
    """Se usa tras cambiar la contraseña o resetear via pregunta de
    seguridad - las sesiones ya abiertas quedarian con una DEK que ya no
    coincide con lo guardado en disco."""
    for token in [t for t, s in _sessions.items() if s["username"] == username]:
        del _sessions[token]
