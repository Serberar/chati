"""Mejoras de mejoras.md (2026-09-28): el chat no delega en el agente de
codigo y no se pasa al chino a mitad de respuesta."""
from unittest.mock import patch

import main


def _names(defs):
    return {t["function"]["name"] for t in defs}


def test_text_chat_cannot_delegate_to_the_code_agent():
    assert "delegar_a_agente_de_codigo" not in _names(main._tools_for("text"))
    assert "delegar_a_agente_de_codigo" in _names(main._tools_for("code"))


def test_answer_that_drifts_into_chinese_is_rewritten_in_spanish():
    mixed = "NTFS ofrece permisos y journaling. 这些特性提高了安全性。"
    with patch.object(main.ollama, "chat", return_value="NTFS ofrece permisos y journaling, que mejoran la seguridad.") as chat:
        fixed = main._fix_language(mixed, "¿Que ofrece NTFS?", "qwen2.5:7b")
    assert fixed == "NTFS ofrece permisos y journaling, que mejoran la seguridad."
    assert chat.call_args.kwargs["think"] is False


def test_spanish_answers_and_chinese_questions_are_left_alone():
    with patch.object(main.ollama, "chat") as chat:
        assert main._fix_language("Todo en español.", "hola", "m") == "Todo en español."
        assert main._fix_language("你好 es hola", "¿como se dice 你好?", "m") == "你好 es hola"
    chat.assert_not_called()


def test_if_the_rewrite_fails_the_original_answer_is_kept():
    mixed = "Hola 你好"
    with patch.object(main.ollama, "chat", side_effect=RuntimeError("ollama caido")):
        assert main._fix_language(mixed, "hola", "m") == mixed
    with patch.object(main.ollama, "chat", return_value="sigue 中文"):
        assert main._fix_language(mixed, "hola", "m") == mixed


def test_a_correction_after_a_photo_edit_edits_that_photo_again():
    # Sergio, 2026-10-05: "corrige..." iba al chat normal y generaba otra imagen
    session = {"last_photo": {"last": "a.jpg", "original": "b.jpg", "time": __import__("time").time()}}
    with patch.object(main.model_registry, "get_edit_model", return_value=object()), \
            patch.object(main.photo_edit, "wants_edit", return_value=False):
        for msg in ("corrige la cara", "que no salga nadie mas", "quita a la otra persona",
                    "ponme unas gafas de sol", "vuelve a hacerla pero sin la toalla"):
            assert main._wants_followup_edit(session, msg, None), msg
        assert main._wants_followup_edit(session, "corrige la cara", "image")
        for msg in ("que tiempo hace hoy", "hazme una imagen de un perro", "explicame este codigo"):
            assert not main._wants_followup_edit(session, msg, None), msg
        assert not main._wants_followup_edit(session, "corrige la cara", "code")
        assert not main._wants_followup_edit({}, "corrige la cara", None)
        old = {"last_photo": {**session["last_photo"], "time": 0}}
        assert not main._wants_followup_edit(old, "corrige la cara", None)
