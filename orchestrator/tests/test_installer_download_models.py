"""Pruebas deterministas de installer_download_models.py - nunca descarga
ni llama a ollama de verdad, todo mockeado."""

from unittest.mock import MagicMock, patch

import pytest

import installer_download_models
import model_registry


@pytest.fixture(autouse=True)
def _ollama_found_and_running(monkeypatch, tmp_path):
    """Por defecto: Ollama encontrado y ya arrancado (los tests de abajo que lo
    necesitan lo cambian). El registro va a una carpeta temporal."""
    monkeypatch.setattr(installer_download_models, "_ollama_exe", lambda: "ollama")
    monkeypatch.setattr(installer_download_models, "_ensure_ollama_server", lambda: None)
    monkeypatch.setattr(installer_download_models, "LOG_FILE", tmp_path / "instalacion_modelos.log")


def test_ollama_is_found_even_if_it_is_not_in_the_path(tmp_path, monkeypatch):
    """El instalador lanza el script con el PATH de antes de instalar Ollama
    (Windows Sandbox, 2026-09-30: la descarga fallaba en silencio)."""
    monkeypatch.undo()
    exe = tmp_path / "Programs" / "Ollama" / "ollama.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(installer_download_models.shutil, "which", lambda name: None)
    assert installer_download_models._ollama_exe() == str(exe)


def test_one_failed_model_does_not_stop_the_others_and_is_reported(tmp_path):
    def fake_run(cmd, check):
        if "qwen3:8b" in cmd:
            raise installer_download_models.subprocess.CalledProcessError(1, cmd)

    with patch.object(installer_download_models.subprocess, "run", side_effect=fake_run):
        failed = installer_download_models.download_selected(["texto-rapido", "texto-calidad"])
    assert failed == ["Chat rapido"]
    log = (tmp_path / "instalacion_modelos.log").read_text(encoding="utf-8")
    assert "FALLO: Chat rapido" in log and "hecho: Chat de mejor calidad" in log


def test_destination_for_image_entry(tmp_path, monkeypatch):
    img = tmp_path / "img"
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    entry = next(e for e in __import__("model_catalog").catalog_for_tier("8gb") if e.id == "imagen-flux")

    dest = installer_download_models._destination_for(entry)

    assert dest == img / "diffusion_models" / "flux" / "flux1-schnell-Q4_K_S.gguf"


def test_destination_for_voice_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(installer_download_models, "MODELS_DIR", tmp_path)
    entry = next(e for e in __import__("model_catalog").catalog_for_tier("8gb") if e.id == "voz-piper")

    dest = installer_download_models._destination_for(entry)

    assert dest == tmp_path / "voice" / "piper" / "es_ES-davefx-medium.onnx"


def test_download_file_skips_if_already_present(tmp_path):
    dest = tmp_path / "modelo.safetensors"
    dest.write_bytes(b"ya existe")

    with patch.object(installer_download_models.requests, "get") as mock_get:
        installer_download_models._download_file("http://fake/url", dest)

    mock_get.assert_not_called()
    assert dest.read_bytes() == b"ya existe"


def test_download_file_writes_the_full_content(tmp_path):
    dest = tmp_path / "sub" / "modelo.bin"
    fake_resp = MagicMock()
    fake_resp.headers = {"content-length": "11"}
    fake_resp.iter_content.return_value = [b"hola ", b"mundo!"]
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    with patch.object(installer_download_models.requests, "get", return_value=fake_resp):
        installer_download_models._download_file("http://fake/url", dest)

    assert dest.read_bytes() == b"hola mundo!"
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_download_file_cut_halfway_leaves_no_broken_model(tmp_path):
    dest = tmp_path / "modelo.bin"
    fake_resp = MagicMock()
    fake_resp.headers = {"content-length": "1000"}
    fake_resp.iter_content.return_value = [b"solo un trozo"]
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    with patch.object(installer_download_models.requests, "get", return_value=fake_resp), \
            pytest.raises(OSError, match="incompleta"):
        installer_download_models._download_file("http://fake/url", dest)

    assert not dest.exists()
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_cpu_variant_pulls_base_and_creates_variant_locally():
    """Bug real: 'ollama pull qwen3-coder:30b-cpu' falla en un equipo nuevo -
    las variantes -cpu solo existen en local, nunca en el registro."""
    modelfiles = []

    def fake_run(cmd, check):
        if cmd[1] == "create":
            modelfiles.append(open(cmd[4], encoding="utf-8").read())

    with patch.object(installer_download_models.subprocess, "run", side_effect=fake_run) as mock_run:
        installer_download_models._pull_ollama_model("qwen3-coder:30b-cpu")

    cmds = [c.args[0][:3] for c in mock_run.call_args_list]
    assert cmds == [["ollama", "pull", "qwen3-coder:30b"], ["ollama", "create", "qwen3-coder:30b-cpu"]]
    assert modelfiles == ["FROM qwen3-coder:30b\nPARAMETER num_gpu 0\nPARAMETER num_ctx 32768\n"]


def test_pull_ollama_model_calls_ollama_pull():
    with patch.object(installer_download_models.subprocess, "run") as mock_run:
        installer_download_models._pull_ollama_model("nomic-embed-text")

    mock_run.assert_called_once_with(["ollama", "pull", "nomic-embed-text"], check=True)


def test_chat_model_gets_the_context_the_agent_needs():
    """qwen2.5:7b es tambien el modelo del agente rapido de OpenCode: con el
    contexto por defecto sus instrucciones no caben."""
    modelfiles = []

    def fake_run(cmd, check):
        if cmd[1] == "create":
            modelfiles.append(open(cmd[4], encoding="utf-8").read())

    with patch.object(installer_download_models.subprocess, "run", side_effect=fake_run):
        installer_download_models._pull_ollama_model("qwen2.5:7b")

    assert modelfiles == ["FROM qwen2.5:7b\nPARAMETER num_ctx 16384\n"]


def test_download_selected_routes_text_entries_to_ollama(tmp_path, monkeypatch):
    monkeypatch.setattr(installer_download_models, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(model_registry, "IMG_DIR", tmp_path / "img")
    monkeypatch.setattr(model_registry, "VID_DIR", tmp_path / "vid")

    with patch.object(installer_download_models, "_pull_ollama_model") as mock_pull, \
         patch.object(installer_download_models, "_download_file") as mock_download:
        installer_download_models.download_selected(["texto-rapido"])

    mock_pull.assert_called_once_with("qwen3:8b")  # el chat usa el modelo del agente rapido
    mock_download.assert_not_called()


def test_download_selected_routes_image_entries_to_download(tmp_path, monkeypatch):
    monkeypatch.setattr(installer_download_models, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(model_registry, "IMG_DIR", tmp_path / "img")
    monkeypatch.setattr(model_registry, "VID_DIR", tmp_path / "vid")

    with patch.object(installer_download_models, "_pull_ollama_model") as mock_pull, \
         patch.object(installer_download_models, "_download_file") as mock_download:
        installer_download_models.download_selected(["imagen-flux"])

    mock_pull.assert_not_called()
    assert mock_download.call_args_list[0].args[1].name == "flux1-schnell-Q4_K_S.gguf"


def test_download_selected_ignores_unknown_ids(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(installer_download_models, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(model_registry, "IMG_DIR", tmp_path / "img")
    monkeypatch.setattr(model_registry, "VID_DIR", tmp_path / "vid")

    installer_download_models.download_selected(["no-existe-de-mentira"])

    assert "AVISO" in capsys.readouterr().err


def test_image_models_bring_the_files_they_need_to_work(tmp_path, monkeypatch):
    """Sin codificadores ni VAE, FLUX no funcionaba en un equipo limpio aunque
    el modelo se hubiera descargado (2026-10-01)."""
    monkeypatch.setattr(model_registry, "IMG_DIR", tmp_path / "img")
    downloaded = []
    with patch.object(installer_download_models, "_download_file",
                      side_effect=lambda url, dest, sha256=None: downloaded.append(dest)):
        assert installer_download_models.download_selected(["imagen-editar"]) == []
    relative = {d.relative_to(tmp_path / "img").as_posix() for d in downloaded}
    assert {"diffusion_models/flux_kontext/flux1-kontext-dev-Q4_K_S.gguf", "text_encoders/clip_l.safetensors",
            "text_encoders/t5xxl_fp8_e4m3fn.safetensors", "vae/flux-vae-bf16.safetensors",
            "matting/modnet.onnx", "upscale_models/RealESRGAN_x4plus.pth"} <= relative


def test_download_file_rejects_a_file_with_another_fingerprint(tmp_path):
    dest = tmp_path / "modelo.bin"
    fake_resp = MagicMock()
    fake_resp.headers = {"content-length": "11"}
    fake_resp.iter_content.return_value = [b"hola ", b"mundo!"]
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    with patch.object(installer_download_models.requests, "get", return_value=fake_resp), \
            pytest.raises(OSError, match="huella"):
        installer_download_models._download_file("http://fake/url", dest, "0" * 64)
    assert not dest.exists()

    good = __import__("hashlib").sha256(b"hola mundo!").hexdigest()
    with patch.object(installer_download_models.requests, "get", return_value=fake_resp):
        installer_download_models._download_file("http://fake/url", dest, good)
    assert dest.read_bytes() == b"hola mundo!"


def test_every_model_download_is_pinned_and_fingerprinted():
    import model_catalog
    for entry in model_catalog._BASE_CATALOG:
        files = ([(entry.download_url, entry.sha256)] if entry.download_url else []) + \
                [(url, sha) for url, _, sha in entry.extra_files]
        for url, sha in files:
            assert "/resolve/main/" not in url, url  # "main" lo puede cambiar el autor
            assert sha and len(sha) == 64, url
