"""Avisos al movil (push.py): el cifrado se comprueba descifrandolo como lo
haria el navegador, y la firma VAPID con la clave publica."""
import base64
import json
import os
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

import push


def _b64(b):
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _browser():
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return key, _b64(raw), os.urandom(16)


def _decrypt(body, key, auth):
    """Lo que hace el navegador al recibirlo (RFC 8291)."""
    salt, idlen = body[:16], body[20]
    as_public, cipher = body[21:21 + idlen], body[21 + idlen:]
    ua_public = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), as_public))
    ikm = HKDF(hashes.SHA256(), 32, salt=auth, info=b"WebPush: info\x00" + ua_public + as_public).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt=salt, info=b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt=salt, info=b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, cipher, None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


@pytest.fixture
def push_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(push, "PUSH_DIR", tmp_path)
    monkeypatch.setattr(push, "KEYS_FILE", tmp_path / "vapid.json")
    monkeypatch.setattr(push, "SUBS_FILE", tmp_path / "subs.json")
    return tmp_path


def test_the_browser_can_decrypt_the_notice():
    key, p256dh, auth = _browser()
    body = push.encrypt("✓ Tarea terminada".encode(), p256dh, _b64(auth))
    assert _decrypt(body, key, auth) == "✓ Tarea terminada".encode()


def test_vapid_signature_is_valid(push_dir):
    header = push._vapid_header("https://web.push.apple.com/abc")
    token = header.split("t=")[1].split(",")[0]
    head, claims, sig = token.split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    assert json.loads(pad(claims))["aud"] == "https://web.push.apple.com"
    raw = pad(sig)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pad(push.public_key()))
    pub.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # lanza si no vale
    assert push.public_key() == push.public_key()  # la misma clave siempre


def test_devices_per_user_and_gone_ones_are_forgotten(push_dir):
    key, p256dh, auth = _browser()
    sub = {"endpoint": "https://web.push.apple.com/xyz", "keys": {"p256dh": p256dh, "auth": _b64(auth)}}
    with pytest.raises(ValueError):
        push.subscribe("u1", {"endpoint": "http://x", "keys": {}})
    push.subscribe("u1", sub, "iPhone")
    push.subscribe("u1", sub, "iPhone")  # la misma: no se repite
    assert push.has_devices("u1") and not push.has_devices("u2")
    sent = []

    class Resp:
        status_code = 410

    def fake_post(url, data, timeout, headers):
        sent.append(_decrypt(data, key, auth))
        return Resp()

    started = []
    with patch.object(push.requests, "post", side_effect=fake_post), \
         patch.object(push.threading, "Thread", side_effect=lambda target, daemon: started.append(target) or
                      type("T", (), {"start": lambda self: None})()):
        push.notify("u1", "✋ Chati necesita tu respuesta", "Permiso para editar app.py", tag="chati-1")
        started[0]()
    assert json.loads(sent[0])["title"] == "✋ Chati necesita tu respuesta"
    assert not push.has_devices("u1")  # 410: ya no existe


def test_watch_notifies_asks_once_and_done_after_chati_checks():
    import main
    state = {"busy": set(), "asked": set(), "done": set(), "progress": {}}
    rounds = iter([({"a"}, {}), (set(), {"b": "permission"}), (set(), {"b": "permission"}), (set(), {})])
    sent = []

    def tick(watching):
        busy, waiting = next(rounds)
        main._code_watch_running.clear()
        main._code_watch_running.update(watching)
        with patch.object(main.requests, "get"), \
             patch.object(main.opencode_client, "busy_session_ids", return_value=list(busy)), \
             patch.object(main.opencode_client, "waiting_sessions", return_value=waiting), \
             patch.object(main.opencode_client, "task_summary", side_effect=lambda b, sid: f"tarea {sid}"), \
             patch.object(main.task_owners, "owner", return_value="u1"), \
             patch.object(main.push, "has_devices", return_value=True), \
             patch.object(main.push, "notify", side_effect=lambda u, t, b, tag: sent.append((t, b))):
            main._push_tick(state)

    main._code_watch["a"] = {"estado": "✓ Chati ha ejecutado los tests y pasan."}
    try:
        tick(set())       # "a" trabajando
        tick({"a"})       # "a" para, pero Chati aun la comprueba; "b" pide permiso
        tick({"a"})       # nada nuevo (el permiso ya se aviso)
        tick(set())       # Chati termina de comprobar "a"
    finally:
        main._code_watch_running.clear()
        main._code_watch.pop("a", None)
    assert sent == [("✋ Chati necesita tu respuesta", "Te pide permiso: tarea b"),
                    ("✓ Tarea terminada", "tarea a · ✓ Chati ha ejecutado los tests y pasan.")]


def test_a_task_without_progress_is_reported_as_stuck_once():
    import main
    state = {"busy": set(), "asked": set(), "done": set(), "progress": {}}
    sent = []
    clock = {"now": 1000.0}

    def tick():
        with patch.object(main.time, "time", side_effect=lambda: clock["now"]), \
             patch.object(main.requests, "get"), \
             patch.object(main.opencode_client, "busy_session_ids", return_value=["s"]), \
             patch.object(main.opencode_client, "waiting_sessions", return_value={}), \
             patch.object(main.opencode_client, "get_task_view", return_value={"steps": [1, 2]}), \
             patch.object(main.opencode_client, "task_summary", return_value="renombrar"), \
             patch.object(main.task_owners, "owner", return_value="u1"), \
             patch.object(main.push, "has_devices", return_value=True), \
             patch.object(main.push, "notify", side_effect=lambda u, t, b, tag: sent.append((t, b))):
            main._push_tick(state)

    main._llm_activity["last"] = 1000.0
    try:
        tick()                                  # primera vez: se apunta
        clock["now"] += 300
        tick()                                  # 5 min: todavia no
        assert sent == [] and "s" not in main._stalled
        clock["now"] += 400
        tick()                                  # 11 min sin pasos ni modelo: atascada
        clock["now"] += 60
        tick()                                  # no se repite el aviso
        assert sent == [("⚠ El agente parece atascado", "Lleva 11 min sin avanzar: renombrar. Puedes detenerlo y repetirlo.")]
        assert "s" in main._stalled
        main._llm_activity["last"] = clock["now"]  # el modelo vuelve a escribir
        tick()
        assert "s" not in main._stalled
    finally:
        main._stalled.clear()
