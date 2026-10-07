"""Dispositivos vinculados para usar Chati desde fuera del ordenador (el movil
por Tailscale; Sergio, 2026-10-07).

Desde el propio ordenador, con la sesion iniciada, se pide un codigo de
vinculacion (QR con un secreto largo, y un codigo corto por si se teclea),
valido 2 minutos y de un solo uso. El dispositivo que lo presenta recibe una
llave propia (cookie); aqui solo se guarda su huella (sha256), no la llave.
Cualquier peticion que llegue de fuera sin una llave valida se rechaza antes
de llegar a nada (security.LocalOnlyMiddleware): para un desconocido, Chati
no existe. La llave dura hasta que se desconecta el dispositivo desde el
ordenador. Encima de esto, se inicia sesion como siempre.
"""

import hashlib
import json
import secrets
import threading
import time
import uuid

from paths import DATA_DIR

STORE = DATA_DIR / "devices.json"
PAIRING_SECONDS = 120
# codigo corto para teclear: sin letras que se confunden (0/O, 1/I/L)
_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
SHORT_CODE_LEN = 8
# intentos fallidos de reclamar un codigo, en total, por minuto: con 8
# caracteres de 31 simbolos (8,5e11) no hay manera de acertar por fuerza bruta
MAX_FAILED_PER_MINUTE = 10
COOKIE_NAME = "chati_device"
# para no escribir el archivo en cada peticion
LAST_SEEN_EVERY = 300

_lock = threading.Lock()
_pairings: dict[str, dict] = {}  # secreto largo -> {user_id, short, expires}
_failed: list[float] = []


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _load() -> list[dict]:
    try:
        return json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(devices: list[dict]) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(devices, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(STORE)


def _purge(now: float) -> None:
    for secret in [s for s, p in _pairings.items() if p["expires"] < now]:
        del _pairings[secret]
    while _failed and _failed[0] < now - 60:
        _failed.pop(0)


def start_pairing(user_id: str) -> dict:
    """Un codigo nuevo para vincular un dispositivo a este usuario. Anula los
    anteriores sin usar de ese usuario."""
    now = time.time()
    with _lock:
        _purge(now)
        for secret in [s for s, p in _pairings.items() if p["user_id"] == user_id]:
            del _pairings[secret]
        secret = secrets.token_urlsafe(24)
        short = "".join(secrets.choice(_ALPHABET) for _ in range(SHORT_CODE_LEN))
        _pairings[secret] = {"user_id": user_id, "short": short, "expires": now + PAIRING_SECONDS}
    return {"secret": secret, "short": short, "expires_in": PAIRING_SECONDS}


def claim(code: str, name: str) -> str | None:
    """El dispositivo presenta el codigo (el secreto del QR o el corto). Si
    vale, se vincula y devuelve su llave; si no, None. Cada codigo sirve una
    sola vez."""
    code = (code or "").strip()
    now = time.time()
    with _lock:
        _purge(now)
        if len(_failed) >= MAX_FAILED_PER_MINUTE:
            return None
        secret = code if code in _pairings else next(
            (s for s, p in _pairings.items() if secrets.compare_digest(p["short"], code.upper().replace(" ", ""))),
            None)
        if secret is None:
            _failed.append(now)
            return None
        pairing = _pairings.pop(secret)
        token = secrets.token_urlsafe(32)
        devices = _load()
        devices.append({
            "id": uuid.uuid4().hex[:12],
            "user_id": pairing["user_id"],
            "name": (name or "Dispositivo").strip()[:60] or "Dispositivo",
            "token_hash": _hash(token),
            "created": now,
            "last_seen": now,
        })
        _save(devices)
    return token


def verify(token: str | None) -> dict | None:
    """El dispositivo vinculado de esta llave, o None."""
    if not token:
        return None
    digest = _hash(token)
    with _lock:
        devices = _load()
        for device in devices:
            if secrets.compare_digest(device["token_hash"], digest):
                now = time.time()
                if now - device.get("last_seen", 0) > LAST_SEEN_EVERY:
                    device["last_seen"] = now
                    _save(devices)
                return device
    return None


def list_for(user_id: str) -> list[dict]:
    return [{k: d[k] for k in ("id", "name", "created", "last_seen")}
            for d in _load() if d["user_id"] == user_id]


def revoke(device_id: str, user_id: str) -> bool:
    with _lock:
        devices = _load()
        keep = [d for d in devices if not (d["id"] == device_id and d["user_id"] == user_id)]
        if len(keep) == len(devices):
            return False
        _save(keep)
    return True
