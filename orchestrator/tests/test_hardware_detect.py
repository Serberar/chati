"""Pruebas deterministas de hardware_detect.py - nvidia-smi siempre
mockeado, nunca depende de tener (o no tener) una GPU de verdad en la
maquina donde corran los tests."""

from unittest.mock import MagicMock, patch

import hardware_detect


def _fake_result(stdout: str, returncode: int = 0) -> MagicMock:
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    return result


def test_detect_gpu_reads_name_and_vram():
    with patch.object(hardware_detect.subprocess, "run",
                       return_value=_fake_result("NVIDIA GeForce RTX 5060 Laptop GPU, 8151\n")):
        info = hardware_detect.detect_gpu()

    assert info.available is True
    assert info.name == "NVIDIA GeForce RTX 5060 Laptop GPU"
    assert info.vram_mib == 8151
    assert info.tier == "8gb"


def test_detect_gpu_falls_back_to_cpu_only_when_nvidia_smi_missing():
    with patch.object(hardware_detect.subprocess, "run", side_effect=FileNotFoundError):
        info = hardware_detect.detect_gpu()

    assert info.available is False
    assert info.tier == "cpu_only"


def test_detect_gpu_falls_back_when_nvidia_smi_returns_error():
    with patch.object(hardware_detect.subprocess, "run", return_value=_fake_result("", returncode=1)):
        info = hardware_detect.detect_gpu()

    assert info.available is False
    assert info.tier == "cpu_only"


def test_detect_gpu_falls_back_on_unparseable_output():
    with patch.object(hardware_detect.subprocess, "run", return_value=_fake_result("cosas raras sin coma\n")):
        info = hardware_detect.detect_gpu()

    assert info.available is False
    assert info.tier == "cpu_only"


def test_tier_boundaries():
    assert hardware_detect._tier_for_vram(0) == "cpu_only"
    assert hardware_detect._tier_for_vram(4000) == "minimo"
    assert hardware_detect._tier_for_vram(8151) == "8gb"
    assert hardware_detect._tier_for_vram(12000) == "12gb"
    assert hardware_detect._tier_for_vram(24000) == "16gb_plus"


def test_detect_gpu_with_low_vram_card():
    with patch.object(hardware_detect.subprocess, "run",
                       return_value=_fake_result("NVIDIA GeForce GTX 1650, 4096\n")):
        info = hardware_detect.detect_gpu()

    assert info.tier == "minimo"
