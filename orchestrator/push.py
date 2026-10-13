"""Avisos al movil aunque Chati este cerrado (Web Push), para dejar un encargo
al agente e irse (Sergio, 2026-10-13). En el iPhone funciona con Chati
instalado en la pantalla de inicio (iOS 16.4 o mas nuevo).

El aviso pasa por el servicio del navegador (Apple, Google o Mozilla), pero
cifrado de punta a punta (RFC 8291): ellos no pueden leerlo. Sin
dependencias nuevas: el cifrado (aes128gcm) y la firma (VAPID, RFC 8292)
con la libreria cryptography que Chati ya usa.

- claves VAPID: se crean la primera vez, en DATA_DIR/push/vapid.json
- dispositivos: DATA_DIR/push/suscripciones.json, {usuario: [suscripcion]}"""
import base64
import json
import logging
import os
import struct
import threading
import time
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from paths import DATA_DIR

log = logging.getLogger("chati")
PUSH_DIR = DATA_DIR / "push"
KEYS_FILE = PUSH_DIR / "vapid.json"
SUBS_FILE = PUSH_DIR / "suscripciones.json"
# quien manda los avisos (lo pide Apple: un mailto o una web del dueño)
CONTACT = "https://serberar.github.io"
MAX_PER_USER = 10
RECORD_SIZE = 4096
_lock = threading.Lock()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _raw_public(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def _keys() -> ec.EllipticCurvePrivateKey:
    with _lock:
        if KEYS_FILE.exists():
            pem = json.loads(KEYS_FILE.read_text(encoding="utf-8"))["private_pem"].encode()
            return serialization.load_pem_private_key(pem, password=None)
        key = ec.generate_private_key(ec.SECP256R1())
        PUSH_DIR.mkdir(parents=True, exist_ok=True)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        KEYS_FILE.write_text(json.dumps({"private_pem": pem}), encoding="utf-8")
        return key


def public_key() -> str:
    """La clave que el navegador necesita para suscribirse (applicationServerKey)."""
    return _b64(_raw_public(_keys().public_key()))


# ---------- dispositivos ----------
def _load() -> dict:
    try:
        return json.loads(SUBS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _store(data: dict) -> None:
    PUSH_DIR.mkdir(parents=True, exist_ok=True)
    tmp = SUBS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, SUBS_FILE)


def _valid(sub: dict) -> bool:
    try:
        url = urlparse(sub["endpoint"])
        keys = sub["keys"]
        return url.scheme == "https" and bool(url.hostname) and len(_unb64(keys["p256dh"])) == 65 \
            and len(_unb64(keys["auth"])) == 16
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


def subscribe(user_id: str, sub: dict, device: str = "") -> None:
    if not _valid(sub):
        raise ValueError("Suscripcion no valida.")
    clean = {"endpoint": sub["endpoint"], "keys": {"p256dh": sub["keys"]["p256dh"], "auth": sub["keys"]["auth"]},
             "dispositivo": device[:80], "desde": time.time()}
    with _lock:
        data = _load()
        subs = [s for s in data.get(user_id, []) if s["endpoint"] != clean["endpoint"]]
        data[user_id] = (subs + [clean])[-MAX_PER_USER:]
        _store(data)


def unsubscribe(user_id: str, endpoint: str) -> None:
    with _lock:
        data = _load()
        data[user_id] = [s for s in data.get(user_id, []) if s["endpoint"] != endpoint]
        _store(data)


def has_devices(user_id: str) -> bool:
    return bool(_load().get(user_id))


def forget_user(user_id: str) -> None:
    with _lock:
        data = _load()
        if data.pop(user_id, None) is not None:
            _store(data)


# ---------- cifrado y firma ----------
def encrypt(payload: bytes, p256dh: str, auth: str, salt: bytes | None = None,
            sender: ec.EllipticCurvePrivateKey | None = None) -> bytes:
    """Cuerpo cifrado aes128gcm (RFC 8291 + RFC 8188), en un solo registro."""
    ua_public = _unb64(p256dh)
    auth_secret = _unb64(auth)
    sender = sender or ec.generate_private_key(ec.SECP256R1())
    as_public = _raw_public(sender.public_key())
    shared = sender.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public))
    ikm = HKDF(hashes.SHA256(), 32, salt=auth_secret,
               info=b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    salt = salt or os.urandom(16)
    cek = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    if len(payload) + 17 > RECORD_SIZE:
        raise ValueError("Aviso demasiado largo.")
    cipher = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", RECORD_SIZE, len(as_public)) + as_public + cipher


def _vapid_header(endpoint: str) -> str:
    key = _keys()
    url = urlparse(endpoint)
    head = _b64(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = _b64(json.dumps({"aud": f"{url.scheme}://{url.netloc}", "exp": int(time.time()) + 12 * 3600,
                              "sub": CONTACT}).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    token = f"{head}.{claims}.{_b64(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
    return f"vapid t={token}, k={_b64(_raw_public(key.public_key()))}"


def _send_one(sub: dict, payload: bytes) -> int:
    body = encrypt(payload, sub["keys"]["p256dh"], sub["keys"]["auth"])
    resp = requests.post(sub["endpoint"], data=body, timeout=20, headers={
        "Authorization": _vapid_header(sub["endpoint"]), "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream", "TTL": "86400", "Urgency": "high"})
    return resp.status_code


def notify(user_id: str | None, title: str, body: str, tag: str = "chati", url: str = "/") -> None:
    """Manda el aviso a todos los dispositivos del usuario, en segundo plano.
    Los que ya no existen (404/410: se borro la app o se quito el permiso) se
    olvidan."""
    subs = _load().get(user_id or "", [])
    if not subs:
        return
    payload = json.dumps({"title": title[:120], "body": body[:300], "tag": tag, "url": url},
                         ensure_ascii=False).encode("utf-8")

    def work():
        for sub in subs:
            try:
                status = _send_one(sub, payload)
            except (requests.RequestException, ValueError) as exc:
                log.warning("Aviso al movil no enviado (%s): %s", urlparse(sub["endpoint"]).hostname, exc)
                continue
            if status in (404, 410):
                log.info("Aviso al movil: dispositivo dado de baja (%s)", sub.get("dispositivo") or "?")
                unsubscribe(user_id, sub["endpoint"])
            elif status >= 400:
                log.warning("Aviso al movil rechazado: %s %s", status, urlparse(sub["endpoint"]).hostname)

    threading.Thread(target=work, daemon=True).start()
