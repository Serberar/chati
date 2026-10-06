"""Pruebas deterministas de persona_trainer.py - el subproceso de
entrenamiento real (sd-scripts) nunca se lanza aqui, se mockea
subprocess.Popen. Ver ROADMAP.md y pendiente/pendiente.md (fase 1: solo
SDXL, pedido por Sergio el 2026-09-24 tras semanas esperando esta pieza)."""

from unittest.mock import MagicMock, patch

import pytest

import model_registry
import persona_trainer


def _make_sdxl_tree(tmp_path, monkeypatch, label="sd_xl_base_1.0"):
    img = tmp_path / "img"
    (img / "checkpoints" / "sdxl").mkdir(parents=True)
    (img / "checkpoints" / "sdxl" / f"{label}.safetensors").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    return img


def _isolate_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(persona_trainer, "PERSONAS_DIR", tmp_path / "personas")
    monkeypatch.setattr(persona_trainer, "DATASETS_DIR", tmp_path / "_datasets")
    monkeypatch.setattr(persona_trainer, "ACCELERATE_CONFIG", tmp_path / "_accelerate_config.yaml")


def test_sanitize_name_strips_unsafe_characters():
    assert persona_trainer._sanitize_name("Sergio Bernabé!!") == "Sergio-Bernab"


def test_sanitize_name_falls_back_when_nothing_left():
    result = persona_trainer._sanitize_name("!!!")
    assert result.startswith("persona-")


def test_class_token_uses_a_rare_prefix():
    token = persona_trainer._class_token("sergio")
    assert token == "ohwx-sergio person"


def test_pick_sdxl_base_checkpoint_prefers_the_base_variant(tmp_path, monkeypatch):
    img = tmp_path / "img"
    (img / "checkpoints" / "sdxl").mkdir(parents=True)
    (img / "checkpoints" / "sdxl" / "RealVisXL_V5.0.safetensors").write_bytes(b"x")
    (img / "checkpoints" / "sdxl" / "sd_xl_base_1.0.safetensors").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)

    path = persona_trainer._pick_sdxl_base_checkpoint()

    assert path.name == "sd_xl_base_1.0.safetensors"


def test_pick_sdxl_base_checkpoint_raises_when_nothing_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(model_registry, "IMG_DIR", tmp_path / "img")
    with pytest.raises(persona_trainer.NoBaseModelError):
        persona_trainer._pick_sdxl_base_checkpoint()


def test_get_training_status_sin_empezar_when_nothing_exists(tmp_path, monkeypatch):
    _isolate_dirs(tmp_path, monkeypatch)
    assert persona_trainer.get_training_status("u1", "nueva-persona") == {"status": "sin_empezar"}


def test_get_training_status_listo_when_the_file_exists(tmp_path, monkeypatch):
    _isolate_dirs(tmp_path, monkeypatch)
    out_dir = persona_trainer.persona_dir("u1", "sergio") / "sdxl"
    out_dir.mkdir(parents=True)
    (out_dir / "sergio_sdxl.safetensors").write_bytes(b"x")

    status = persona_trainer.get_training_status("u1", "sergio")

    assert status["status"] == "listo"


def test_get_training_status_error_when_process_died_without_output(tmp_path, monkeypatch):
    _isolate_dirs(tmp_path, monkeypatch)
    persona_trainer._write_status("u1", "sergio", "sdxl", {"status": "training", "pid": 999999999})
    with patch.object(persona_trainer, "_pid_alive", return_value=False):
        status = persona_trainer.get_training_status("u1", "sergio")
    assert status["status"] == "error"


def test_get_training_status_still_training_when_pid_alive(tmp_path, monkeypatch):
    _isolate_dirs(tmp_path, monkeypatch)
    persona_trainer._write_status("u1", "sergio", "sdxl", {"status": "training", "pid": 123})
    with patch.object(persona_trainer, "_pid_alive", return_value=True):
        status = persona_trainer.get_training_status("u1", "sergio")
    assert status["status"] == "training"


def test_start_training_refuses_when_already_training(tmp_path, monkeypatch):
    _isolate_dirs(tmp_path, monkeypatch)
    _make_sdxl_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(persona_trainer, "SD_SCRIPTS_PYTHON", tmp_path)  # solo necesita .exists()
    monkeypatch.setattr(persona_trainer, "ACCELERATE_EXE", tmp_path)
    persona_trainer._write_status("u1", "sergio", "sdxl", {"status": "training", "pid": 123})

    with patch.object(persona_trainer, "_pid_alive", return_value=True):
        with pytest.raises(persona_trainer.TrainingAlreadyRunningError):
            persona_trainer.start_training("u1", "sergio", [])


def test_start_training_launches_the_subprocess_and_writes_status(tmp_path, monkeypatch):
    _isolate_dirs(tmp_path, monkeypatch)
    _make_sdxl_tree(tmp_path, monkeypatch)
    monkeypatch.setattr(persona_trainer, "SD_SCRIPTS_PYTHON", tmp_path / "venv" / "Scripts" / "python.exe")
    monkeypatch.setattr(persona_trainer, "ACCELERATE_EXE", tmp_path / "venv" / "Scripts" / "accelerate.exe")
    persona_trainer.SD_SCRIPTS_PYTHON.parent.mkdir(parents=True)
    persona_trainer.SD_SCRIPTS_PYTHON.write_bytes(b"x")
    persona_trainer.ACCELERATE_EXE.write_bytes(b"x")

    photo = tmp_path / "foto.jpg"
    photo.write_bytes(b"fake-jpg-bytes")

    fake_proc = MagicMock()
    fake_proc.pid = 4242
    with patch.object(persona_trainer.subprocess, "Popen", return_value=fake_proc) as mock_popen:
        result = persona_trainer.start_training("u1", "Sergio", [photo, photo, photo], epochs=3)

    assert result == {"status": "training", "persona": "Sergio", "architecture": "sdxl"}
    mock_popen.assert_called_once()
    cmd = mock_popen.call_args.args[0]
    assert "--max_train_epochs=3" in cmd
    assert "--network_train_unet_only" in cmd

    dataset_images = persona_trainer.DATASETS_DIR / "u1" / "Sergio" / "images"
    assert len(list(dataset_images.iterdir())) == 3

    status = persona_trainer._read_status("u1", "Sergio", "sdxl")
    assert status["status"] == "training"
    assert status["pid"] == 4242
    assert status["photos"] == 3


def test_list_personas_includes_status_per_persona(tmp_path, monkeypatch):
    _isolate_dirs(tmp_path, monkeypatch)
    out_dir = persona_trainer.persona_dir("u1", "ana") / "sdxl"
    out_dir.mkdir(parents=True)
    (out_dir / "ana_sdxl.safetensors").write_bytes(b"x")

    personas = persona_trainer.list_personas("u1")

    assert len(personas) == 1
    assert personas[0]["name"] == "ana"
    assert personas[0]["sdxl"]["status"] == "listo"


def test_training_progress_reads_the_last_tqdm_step(tmp_path):
    log = tmp_path / "train.log"
    log.write_text("caching latents...\nsteps:  10%|#         | 3/30 [00:12<01:48,  4.01s/it]\r"
                   "steps:  45%|####5     | 27/60 [01:10<12:25,  2.6s/it, avr_loss=0.1]\r", encoding="utf-8")
    assert persona_trainer.training_progress(log) == {"percent": 45, "step": 27, "steps": 60, "eta": "unos 12 min"}


def test_training_progress_before_the_first_step_is_preparing(tmp_path):
    log = tmp_path / "train.log"
    log.write_text("loading model...\n", encoding="utf-8")
    assert persona_trainer.training_progress(log) == {"phase": "preparando"}
    assert persona_trainer.training_progress(tmp_path / "no-existe.log") == {"phase": "preparando"}


def test_lora_for_gives_the_comfyui_name_and_trigger_only_when_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(persona_trainer, "PERSONAS_DIR", tmp_path / "loras" / "personas")
    assert persona_trainer.lora_for("u1", "Ana Lopez") is None
    out = tmp_path / "loras" / "personas" / "u1" / "Ana-Lopez" / "sdxl"
    out.mkdir(parents=True)
    (out / "Ana-Lopez_sdxl.safetensors").write_bytes(b"x")
    name, trigger = persona_trainer.lora_for("u1", "Ana Lopez")
    assert name.replace("\\", "/") == "personas/u1/Ana-Lopez/sdxl/Ana-Lopez_sdxl.safetensors"
    assert trigger == "ohwx-Ana-Lopez person"


def test_personas_are_private_to_their_owner(tmp_path, monkeypatch):
    """Auditoria 2026-09-29: las personas eran comunes a todos los usuarios."""
    _isolate_dirs(tmp_path, monkeypatch)
    out_dir = persona_trainer.persona_dir("u1", "ana") / "sdxl"
    out_dir.mkdir(parents=True)
    (out_dir / "ana_sdxl.safetensors").write_bytes(b"x")

    assert [p["name"] for p in persona_trainer.list_personas("u1")] == ["ana"]
    assert persona_trainer.list_personas("u2") == []
    assert persona_trainer.lora_for("u2", "ana") is None
    assert persona_trainer.lora_for("u1", "ana") is not None
    with pytest.raises(ValueError):
        persona_trainer.persona_dir("../..", "ana")


def test_training_photos_are_deleted_when_it_finishes_and_persona_can_be_deleted(tmp_path, monkeypatch):
    _isolate_dirs(tmp_path, monkeypatch)
    photos = persona_trainer._dataset_dir("u1", "ana") / "images"
    photos.mkdir(parents=True)
    (photos / "000.jpg").write_bytes(b"cara")
    out_dir = persona_trainer.persona_dir("u1", "ana") / "sdxl"
    out_dir.mkdir(parents=True)
    (out_dir / "ana_sdxl.safetensors").write_bytes(b"x")

    assert persona_trainer.get_training_status("u1", "ana")["status"] == "listo"
    assert not photos.exists()

    assert persona_trainer.delete_persona("u1", "ana") is True
    assert not persona_trainer.persona_dir("u1", "ana").exists()


def test_only_one_persona_trains_at_a_time_even_for_other_users(tmp_path, monkeypatch):
    _isolate_dirs(tmp_path, monkeypatch)
    persona_trainer._write_status("otro-usuario", "Ana", "sdxl", {"status": "training", "pid": 1234})
    monkeypatch.setattr(persona_trainer, "_pid_alive", lambda pid: True)
    assert persona_trainer._any_training_running()
    monkeypatch.setattr(persona_trainer, "_pid_alive", lambda pid: False)
    assert not persona_trainer._any_training_running()
