"""
Test unitario (sin ComfyUI real) de la parte que peor se entiende del modulo:
distinguir un fallo real de una cancelacion, a partir de la forma exacta en
la que ComfyUI reporta cada caso en /history (comprobado contra el servidor
real, ver notas en comfyui_client.py)."""

from unittest.mock import patch

import pytest

from agents.comfyui_client import GenerationCancelled, submit_and_wait, upload_unique


def _mock_responses(history_by_call, queue_response=None):
    """Devuelve una funcion que simula requests.get/.post segun la url,
    entregando un historial distinto en cada llamada sucesiva a /history."""
    call_state = {"n": 0}

    class FakeResp:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

        def raise_for_status(self):
            pass

    def fake_request(method, url, **kwargs):
        if url.endswith("/prompt"):
            return FakeResp({"prompt_id": "fake-id"})
        if "/history/" in url:
            idx = min(call_state["n"], len(history_by_call) - 1)
            call_state["n"] += 1
            return FakeResp(history_by_call[idx])
        if url.endswith("/queue"):
            return FakeResp(queue_response or {"queue_running": [], "queue_pending": []})
        if method == "POST" and url.endswith("/history"):  # forget(): borrar el rastro
            return FakeResp({})
        raise AssertionError(f"URL no mockeada: {url}")

    return fake_request


@patch("agents.comfyui_client.requests.post")
@patch("agents.comfyui_client.requests.get")
def test_interrupted_execution_raises_generation_cancelled(mock_get, mock_post):
    interrupted_history = {
        "fake-id": {
            "status": {
                "status_str": "error",
                "messages": [
                    ["execution_start", {}],
                    ["execution_interrupted", {"node_id": "sampler"}],
                ],
            },
            "outputs": {},
        }
    }
    fake = _mock_responses([{}, interrupted_history])
    mock_get.side_effect = lambda url, **kw: fake("GET", url, **kw)
    mock_post.side_effect = lambda url, **kw: fake("POST", url, **kw)

    with pytest.raises(GenerationCancelled):
        submit_and_wait("http://fake", {}, "save_image", "images", timeout=30)


@patch("agents.comfyui_client.requests.post")
@patch("agents.comfyui_client.requests.get")
def test_real_failure_raises_runtime_error_not_cancelled(mock_get, mock_post):
    real_failure_history = {
        "fake-id": {
            "status": {
                "status_str": "error",
                "messages": [["execution_error", {"exception_message": "algo raro paso"}]],
            },
            "outputs": {},
        }
    }
    fake = _mock_responses([{}, real_failure_history])
    mock_get.side_effect = lambda url, **kw: fake("GET", url, **kw)
    mock_post.side_effect = lambda url, **kw: fake("POST", url, **kw)

    with pytest.raises(RuntimeError) as exc_info:
        submit_and_wait("http://fake", {}, "save_image", "images", timeout=30)
    assert not isinstance(exc_info.value, GenerationCancelled)


def test_before_submit_hook_runs_before_sending_the_workflow(monkeypatch):
    from agents import comfyui_client

    calls = []
    monkeypatch.setattr(comfyui_client, "before_submit", lambda: calls.append("liberar"))

    def fake_post(url, json, timeout):
        calls.append("enviar")
        raise RuntimeError("parar aqui")

    monkeypatch.setattr(comfyui_client.requests, "post", fake_post)
    try:
        comfyui_client.submit_and_wait("http://x", {}, "9", "images", timeout=1)
    except RuntimeError:
        pass
    assert calls == ["liberar", "enviar"]


@pytest.fixture(autouse=True)
def _sin_liberar_memoria(monkeypatch):
    # main.py registra before_submit al importarse (libera Ollama de verdad);
    # estos tests simulan la red y no deben ejecutarlo
    from agents import comfyui_client
    monkeypatch.setattr(comfyui_client, "before_submit", None)


def test_free_memory_waits_until_comfyui_really_released_the_gpu(monkeypatch):
    from agents import comfyui_client

    reported = [6 * 2**30, 3 * 2**30, 60 * 2**20]  # descarga en diferido
    gets = []

    class R:
        def __init__(self, data): self.data = data
        def json(self): return self.data

    monkeypatch.setattr(comfyui_client.requests, "post", lambda *a, **k: R({}))
    monkeypatch.setattr(comfyui_client.requests, "get", lambda *a, **k: (
        gets.append(1), R({"devices": [{"torch_vram_total": reported[min(len(gets) - 1, 2)]}]}))[1])
    monkeypatch.setattr(comfyui_client.time, "sleep", lambda s: None)

    comfyui_client.free_memory("http://x")

    assert len(gets) == 3  # siguio mirando hasta verla libre, y paro ahi


def test_free_memory_never_raises_when_comfyui_is_down(monkeypatch):
    from agents import comfyui_client

    def boom(*a, **k):
        raise comfyui_client.requests.ConnectionError("caido")

    monkeypatch.setattr(comfyui_client.requests, "post", boom)
    comfyui_client.free_memory("http://x")


def test_user_queue_ignores_model_warmups(monkeypatch):
    from agents import comfyui_client

    class R:
        def json(self):
            return {
                "queue_running": [[1, "a", {"6": {"inputs": {"text": comfyui_client.WARMUP_PROMPT}}}]],
                "queue_pending": [[2, "b", {"6": {"inputs": {"text": "un gato"}}}],
                                  [3, "c", {"6": {"inputs": {"text": "un perro"}}}]],
            }

    monkeypatch.setattr(comfyui_client.requests, "get", lambda *a, **k: R())
    assert comfyui_client.user_queue("http://x") == (0, 2)


def test_forget_deletes_comfyui_copies_but_nothing_outside(tmp_path, monkeypatch):
    """Auditoria 2026-09-29: ComfyUI guardaba en claro cada foto subida y cada
    resultado. forget() los borra, y no toca nada fuera de su carpeta."""
    from agents import comfyui_client

    (tmp_path / "input").mkdir()
    (tmp_path / "output" / "sub").mkdir(parents=True)
    face = tmp_path / "input" / "cara.png"
    face.write_bytes(b"x")
    result = tmp_path / "output" / "sub" / "res_0001.png"
    result.write_bytes(b"y")
    outside = tmp_path / "secreto.txt"
    outside.write_bytes(b"z")
    monkeypatch.setattr(comfyui_client, "COMFY_DIR", tmp_path)
    workflow = {"1": {"class_type": "LoadImage", "inputs": {"image": "cara.png"}},
                "2": {"class_type": "LoadImage", "inputs": {"image": "../secreto.txt"}},
                "3": {"class_type": "KSampler", "inputs": {"image": "cara.png"}}}
    with patch("agents.comfyui_client.requests.post") as mock_post:
        comfyui_client.forget("http://fake", "pid", workflow,
                              {"filename": "res_0001.png", "subfolder": "sub", "type": "output"})
    assert not face.exists() and not result.exists()
    assert outside.exists()
    mock_post.assert_called_once_with("http://fake/history", json={"delete": ["pid"]}, timeout=5)


def test_each_upload_gets_its_own_name():
    # con el mismo nombre y overwrite, una peticion en cola usaba la foto de otra
    # (auditoria 2026-10-05)
    from unittest.mock import MagicMock
    resp = MagicMock()
    resp.json.side_effect = lambda: {"name": mock_post.call_args.kwargs["files"]["image"][0]}
    with patch("agents.comfyui_client.requests.post", return_value=resp) as mock_post:
        a = upload_unique("http://x", b"1", "reference.png")
        b = upload_unique("http://x", b"2", "reference.png")
    assert a != b and a.endswith(".png") and b.startswith("chati_")
    assert "overwrite" not in (mock_post.call_args.kwargs.get("data") or {})


def test_each_generation_remembers_who_asked_for_it():
    from unittest.mock import MagicMock
    from agents import comfyui_client
    post = MagicMock()
    post.json.return_value = {"prompt_id": "p-1"}
    hist = MagicMock()
    hist.json.return_value = {}
    token = comfyui_client.current_owner.set("usuario-a")
    try:
        with patch("agents.comfyui_client.requests.post", return_value=post), \
             patch("agents.comfyui_client.requests.get", return_value=hist), \
             patch("agents.comfyui_client._is_still_queued", return_value=True), \
             patch("agents.comfyui_client.time.sleep", side_effect=lambda s: None):
            with pytest.raises(TimeoutError):
                comfyui_client.submit_and_wait("http://x", {}, "save", "images", timeout=0)
    finally:
        comfyui_client.current_owner.reset(token)
    # se apunta al enviarla y se olvida al terminar (aqui, al agotar el tiempo)
    assert comfyui_client.owner_of("p-1") is None


def test_a_slow_answer_from_comfyui_does_not_kill_the_generation():
    # 2026-10-06: "Read timed out" en una consulta de estado tiraba una edicion de 4 min
    import requests as real_requests
    from unittest.mock import MagicMock
    done = {"p": {"status": {"status_str": "success"},
                  "outputs": {"save": {"images": [{"filename": "a.png", "type": "output"}]}}}}
    post = MagicMock()
    post.json.return_value = {"prompt_id": "p"}
    answers = iter([real_requests.ReadTimeout("lento"), MagicMock(json=lambda: done),
                    MagicMock(content=b"png", raise_for_status=lambda: None)])
    with patch("agents.comfyui_client.requests.post", return_value=post), \
         patch("agents.comfyui_client.requests.get", side_effect=lambda *a, **k: (
             (_ for _ in ()).throw(v) if isinstance(v := next(answers), Exception) else v)), \
         patch("agents.comfyui_client.time.sleep"), \
         patch("agents.comfyui_client.forget"):
        assert submit_and_wait("http://x", {}, "save", "images", timeout=60)["content"] == b"png"


@patch("agents.comfyui_client.time.sleep", lambda s: None)
@patch("agents.comfyui_client.requests.post")
@patch("agents.comfyui_client.requests.get")
def test_a_job_that_ends_between_looking_at_history_and_queue_is_not_cancelled(mock_get, mock_post):
    """2026-10-07: una ampliacion ESRGAN de 17 s salio bien, pero termino justo
    entre mirar /history (aun no estaba) y /queue (ya no estaba) y se dio por
    cancelada: la edicion entera fallaba con "Generacion cancelada"."""
    done = {"fake-id": {"status": {"status_str": "success", "messages": []},
                        "outputs": {"save_image": {"images": [{"filename": "x.png", "type": "output"}]}}}}
    fake = _mock_responses([{}, done, done])  # 1: en marcha; 2 y 3: ya terminado

    class Img:
        content = b"png"

        def raise_for_status(self):
            pass

    def get(url, **kw):
        return Img() if url.endswith("/view") else fake("GET", url, **kw)

    mock_get.side_effect = get
    mock_post.side_effect = lambda url, **kw: fake("POST", url, **kw)
    clock = iter(range(0, 10_000, 3))
    with patch("agents.comfyui_client.time.time", lambda: next(clock)):
        result = submit_and_wait("http://fake", {}, "save_image", "images", timeout=600)
    assert result["content"] == b"png"
