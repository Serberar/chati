"""Vincular el movil para usar Chati desde fuera (devices.py y la puerta de
security.LocalOnlyMiddleware, 2026-10-07)."""
import time

import pytest
from fastapi.testclient import TestClient

import devices
import main
import security

REMOTE = "movil-de-prueba.ts.net"


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "STORE", tmp_path / "devices.json")
    devices._pairings.clear()
    devices._failed.clear()
    security.REMOTE_HOSTS.add(REMOTE)
    yield
    security.REMOTE_HOSTS.discard(REMOTE)


def test_a_code_links_one_device_once():
    pairing = devices.start_pairing("u1")
    token = devices.claim(pairing["secret"], "iPhone")
    assert token and devices.verify(token)["name"] == "iPhone"
    assert devices.claim(pairing["secret"], "otro") is None      # un solo uso
    assert devices.verify("llave-inventada") is None


def test_the_short_code_works_and_expires(monkeypatch):
    pairing = devices.start_pairing("u1")
    assert devices.claim(pairing["short"].lower(), "Android")     # sin importar mayusculas
    pairing = devices.start_pairing("u1")
    now = time.time()
    monkeypatch.setattr(devices.time, "time", lambda: now + devices.PAIRING_SECONDS + 1)
    assert devices.claim(pairing["short"], "tarde") is None


def test_guessing_codes_is_cut_off():
    pairing = devices.start_pairing("u1")
    for _ in range(devices.MAX_FAILED_PER_MINUTE):
        assert devices.claim("NOPE1234", "x") is None
    assert devices.claim(pairing["short"], "y") is None            # ni con el bueno, este minuto


def test_a_device_is_disconnected_only_by_its_user():
    token = devices.claim(devices.start_pairing("u1")["secret"], "iPhone")
    device = devices.verify(token)
    assert not devices.revoke(device["id"], "otro-usuario")
    assert devices.revoke(device["id"], "u1")
    assert devices.verify(token) is None


def test_from_outside_nothing_exists_without_a_linked_device():
    client = TestClient(main.app)
    remote = {"Host": REMOTE}
    assert client.get("/", headers=remote, follow_redirects=False).headers["location"] == "/pair"
    assert client.get("/pair", headers=remote).status_code == 200
    assert client.post("/auth/login", headers=remote, json={"username": "a", "password": "b"}).status_code == 403
    assert client.get("/sessions", headers=remote).status_code == 403
    # otro nombre cualquiera, ni eso
    assert client.get("/pair", headers={"Host": "evil.example"}).status_code == 400


def test_a_linked_device_gets_through_to_the_login():
    client = TestClient(main.app, base_url=f"https://{REMOTE}")
    pairing = devices.start_pairing("u1")
    resp = client.post("/pair/claim", json={"code": pairing["secret"], "name": "iPhone"})
    assert resp.status_code == 200 and devices.COOKIE_NAME in resp.cookies
    # con la llave ya llega a Chati (que pide iniciar sesion como siempre)
    assert client.get("/", follow_redirects=False).status_code == 200
    assert client.get("/sessions").status_code == 401


def test_codes_are_only_made_on_the_computer():
    client = TestClient(main.app, base_url=f"https://{REMOTE}")
    client.cookies.set(devices.COOKIE_NAME, devices.claim(devices.start_pairing("u1")["secret"], "iPhone"))
    # aunque tuviera sesion, desde el movil no se puede vincular otro
    resp = client.post("/devices/pair", headers={"X-Session-Token": "x"})
    assert resp.status_code in (401, 403)


def test_errors_on_the_screen_reach_the_log(caplog):
    """2026-10-07: lo que fallaba en Safari del iPhone no llegaba al registro."""
    client = TestClient(main.app)
    with caplog.at_level("WARNING", logger="chati"):
        resp = client.post("/client-log", json={"message": "TypeError: Load failed", "page": "/"})
    assert resp.status_code == 200
    assert any("TypeError: Load failed" in r.getMessage() for r in caplog.records)


def test_error_responses_are_logged(caplog):
    client = TestClient(main.app)
    with caplog.at_level("WARNING", logger="chati"):
        client.get("/sessions", headers={"Host": REMOTE})          # movil sin vincular
    assert any("no esta vinculado" in r.getMessage() and "fuera (sin vincular)" in r.getMessage()
               for r in caplog.records)
