"""Prueba de regresion para un bug real: al cambiar las plantillas de
workflow a placeholders (__CHECKPOINT_PATH__/__UNET_PATH__, ver
model_registry.py), video_agent.py se quedo sin el codigo que los rellena -
toda generacion de video devolvia un error de ComfyUI (checkpoint
"__CHECKPOINT_PATH__" no encontrado) que el endpoint /video_with_face
disfrazaba como "genero solo la imagen base, sin animar". Encontrado por
Sergio insistiendo en que se arreglase aunque fuese un bug preexistente,
no algo tocado en esta sesion.

Estas pruebas no llaman a ComfyUI de verdad - interceptan submit_and_wait
para comprobar que el workflow que se le manda ya no tiene ningun
placeholder de checkpoint sin rellenar, sea cual sea el agente."""

import model_registry
from agents import image_agent, video_agent


def _make_image_tree(tmp_path, monkeypatch):
    img = tmp_path / "img"
    vid = tmp_path / "vid"
    (img / "checkpoints" / "sdxl").mkdir(parents=True)
    (img / "diffusion_models" / "flux").mkdir(parents=True)
    (img / "checkpoints" / "sdxl" / "modelo.safetensors").write_bytes(b"x")
    (img / "diffusion_models" / "flux" / "modelo.gguf").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)
    return img, vid


def _make_video_tree(tmp_path, monkeypatch):
    img = tmp_path / "img"
    vid = tmp_path / "vid"
    (vid / "checkpoints" / "ltxv").mkdir(parents=True)
    (vid / "checkpoints" / "ltxv" / "modelo.safetensors").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)
    return img, vid


def _capture_workflow(monkeypatch, module):
    captured = {}

    def fake_submit_and_wait(base_url, workflow, save_node, output_keys, timeout):
        captured["workflow"] = workflow
        return {"content": b"fake-bytes", "prompt_id": "fake"}

    monkeypatch.setattr(module, "submit_and_wait", fake_submit_and_wait)
    return captured


def _assert_no_unfilled_placeholders(workflow: dict):
    raw = str(workflow)
    assert "__CHECKPOINT_PATH__" not in raw, "quedo un placeholder de checkpoint sin rellenar"
    assert "__UNET_PATH__" not in raw, "quedo un placeholder de unet sin rellenar"


def test_image_agent_generate_fills_the_checkpoint_placeholder(tmp_path, monkeypatch):
    _make_image_tree(tmp_path, monkeypatch)
    captured = _capture_workflow(monkeypatch, image_agent)
    agent = image_agent.ImageAgent("http://fake")

    agent.generate("a cat", model_id="sdxl:modelo")

    _assert_no_unfilled_placeholders(captured["workflow"])
    assert "modelo.safetensors" in captured["workflow"]["checkpoint_loader"]["inputs"]["ckpt_name"]


def test_image_agent_generate_flux_fills_the_unet_placeholder(tmp_path, monkeypatch):
    _make_image_tree(tmp_path, monkeypatch)
    captured = _capture_workflow(monkeypatch, image_agent)
    agent = image_agent.ImageAgent("http://fake")

    agent.generate("a cat", model_id="flux:modelo")

    _assert_no_unfilled_placeholders(captured["workflow"])
    assert "modelo.gguf" in captured["workflow"]["unet_loader"]["inputs"]["unet_name"]


def test_image_agent_generate_with_face_fills_the_checkpoint_placeholder(tmp_path, monkeypatch):
    _make_image_tree(tmp_path, monkeypatch)
    captured = _capture_workflow(monkeypatch, image_agent)
    monkeypatch.setattr(image_agent.ImageAgent, "upload_image_bytes", lambda self, *a, **k: "ref.png")
    agent = image_agent.ImageAgent("http://fake")

    agent.generate_with_face("a cat", b"fake-image-bytes")

    _assert_no_unfilled_placeholders(captured["workflow"])


def test_image_agent_generate_raises_clearly_when_nothing_installed(tmp_path, monkeypatch):
    img = tmp_path / "img"
    vid = tmp_path / "vid"
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)
    agent = image_agent.ImageAgent("http://fake")

    try:
        agent.generate("a cat")
        assert False, "deberia haber lanzado NoModelInstalledError"
    except image_agent.NoModelInstalledError:
        pass


def test_video_agent_generate_fills_the_checkpoint_placeholder(tmp_path, monkeypatch):
    _make_video_tree(tmp_path, monkeypatch)
    captured = _capture_workflow(monkeypatch, video_agent)
    agent = video_agent.VideoAgent("http://fake")

    agent.generate("a dog running")

    _assert_no_unfilled_placeholders(captured["workflow"])
    assert "modelo.safetensors" in captured["workflow"]["checkpoint_loader"]["inputs"]["ckpt_name"]


def test_video_agent_generate_from_image_fills_the_checkpoint_placeholder(tmp_path, monkeypatch):
    _make_video_tree(tmp_path, monkeypatch)
    captured = _capture_workflow(monkeypatch, video_agent)
    monkeypatch.setattr(video_agent.VideoAgent, "upload_image", lambda self, *a, **k: "frame.png")
    agent = video_agent.VideoAgent("http://fake")

    agent.generate_from_image("a dog running", "/fake/frame.png")

    _assert_no_unfilled_placeholders(captured["workflow"])


def test_video_agent_generate_raises_clearly_when_nothing_installed(tmp_path, monkeypatch):
    img = tmp_path / "img"
    vid = tmp_path / "vid"
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)
    agent = video_agent.VideoAgent("http://fake")

    try:
        agent.generate("a dog running")
        assert False, "deberia haber lanzado NoModelInstalledError"
    except image_agent.NoModelInstalledError:
        pass
