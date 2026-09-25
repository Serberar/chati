"""Test unitario (sin Ollama real) de la politica de "un solo modelo de
texto pesado en RAM a la vez" (ver ROADMAP.md, punto 14): varios modelos
CPU grandes cargados a la vez agotaban los 32GB de RAM reales de la
maquina y causaban lentitud severa por intercambio a disco - pedido
explicito de Sergio: solo un modelo pesado cargado en cada momento,
descargando el anterior antes de usar uno distinto."""

from unittest.mock import patch

import main


def _reset():
    main._active_heavy_model = None


def test_first_heavy_model_loads_without_unloading_anything():
    _reset()
    with patch.object(main.ollama, "unload") as mock_unload:
        main._ensure_active_model("qwen3-coder:30b-cpu")

    mock_unload.assert_not_called()
    assert main._active_heavy_model == "qwen3-coder:30b-cpu"


def test_switching_to_a_different_heavy_model_unloads_the_old_one():
    _reset()
    main._active_heavy_model = "qwen3-coder:30b-cpu"
    with patch.object(main.ollama, "unload") as mock_unload:
        main._ensure_active_model("qwen3-abliterated:14b-cpu")

    mock_unload.assert_called_once_with("qwen3-coder:30b-cpu")
    assert main._active_heavy_model == "qwen3-abliterated:14b-cpu"


def test_repeating_the_same_heavy_model_does_not_unload():
    _reset()
    main._active_heavy_model = "qwen3-coder:30b-cpu"
    with patch.object(main.ollama, "unload") as mock_unload:
        main._ensure_active_model("qwen3-coder:30b-cpu")

    mock_unload.assert_not_called()
    assert main._active_heavy_model == "qwen3-coder:30b-cpu"


def test_switching_to_the_light_shared_model_frees_the_heavy_one():
    """El modelo ligero (router/verificador/perfil 'rapido') vive en VRAM,
    no compite por la RAM del sistema - pero si habia un modelo pesado
    cargado de antes, ya no hace falta y debe liberarse."""
    _reset()
    main._active_heavy_model = "qwen3-coder:30b-cpu"
    with patch.object(main.ollama, "unload") as mock_unload:
        main._ensure_active_model(main._LIGHT_MODEL)

    mock_unload.assert_called_once_with("qwen3-coder:30b-cpu")
    assert main._active_heavy_model is None


def test_light_model_calls_stay_cheap_when_nothing_heavy_was_loaded():
    _reset()
    with patch.object(main.ollama, "unload") as mock_unload:
        main._ensure_active_model(main._LIGHT_MODEL)

    mock_unload.assert_not_called()
    assert main._active_heavy_model is None
