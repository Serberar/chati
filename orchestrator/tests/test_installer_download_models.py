"""Pruebas deterministas de installer_download_models.py - nunca descarga
ni llama a ollama de verdad, todo mockeado."""

from unittest.mock import MagicMock, patch

import pytest

import installer_download_models
import model_registry


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
    fake_resp.headers = {"content-length": "12"}
    fake_resp.iter_content.return_value = [b"hola ", b"mundo!"]
    fake_resp.__enter__.return_value = fake_resp
    fake_resp.__exit__.return_value = False

    with patch.object(installer_download_models.requests, "get", return_value=fake_resp):
        installer_download_models._download_file("http://fake/url", dest)

    assert dest.read_bytes() == b"hola mundo!"
    assert not dest.with_suffix(dest.suffix + ".part").exists()


def test_pull_ollama_model_calls_ollama_pull():
    with patch.object(installer_download_models.subprocess, "run") as mock_run:
        installer_download_models._pull_ollama_model("qwen2.5:7b")

    mock_run.assert_called_once_with(["ollama", "pull", "qwen2.5:7b"], check=True)


def test_download_selected_routes_text_entries_to_ollama(tmp_path, monkeypatch):
    monkeypatch.setattr(installer_download_models, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(model_registry, "IMG_DIR", tmp_path / "img")
    monkeypatch.setattr(model_registry, "VID_DIR", tmp_path / "vid")

    with patch.object(installer_download_models, "_pull_ollama_model") as mock_pull, \
         patch.object(installer_download_models, "_download_file") as mock_download:
        installer_download_models.download_selected(["texto-rapido"])

    mock_pull.assert_called_once_with("qwen2.5:7b")
    mock_download.assert_not_called()


def test_download_selected_routes_image_entries_to_download(tmp_path, monkeypatch):
    monkeypatch.setattr(installer_download_models, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(model_registry, "IMG_DIR", tmp_path / "img")
    monkeypatch.setattr(model_registry, "VID_DIR", tmp_path / "vid")

    with patch.object(installer_download_models, "_pull_ollama_model") as mock_pull, \
         patch.object(installer_download_models, "_download_file") as mock_download:
        installer_download_models.download_selected(["imagen-flux"])

    mock_pull.assert_not_called()
    mock_download.assert_called_once()


def test_download_selected_ignores_unknown_ids(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(installer_download_models, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(model_registry, "IMG_DIR", tmp_path / "img")
    monkeypatch.setattr(model_registry, "VID_DIR", tmp_path / "vid")

    installer_download_models.download_selected(["no-existe-de-mentira"])

    assert "AVISO" in capsys.readouterr().err
