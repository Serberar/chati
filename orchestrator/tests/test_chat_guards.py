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
    session = {"user_id": None}  # invitado: la foto de la conversacion vive en la sesion
    main.photo_session.start(session, "conv-1", "a" * 32 + ".jpg", "subida")

    def intent(msg, override=None, chat="conv-1"):
        found = main._photo_followup(session, msg, override, chat)
        return found[0] if found else None

    with patch.object(main.model_registry, "get_edit_model", return_value=object()), \
            patch.object(main.ollama, "chat", return_value="otra"):
        for msg in ("corrige la cara", "que no salga nadie mas", "quita a la otra persona",
                    "ponme unas gafas de sol", "vuelve a hacerla pero sin la toalla", "ahora haz que este sentado"):
            assert intent(msg) == "editar", msg
        assert intent("corrige la cara", "image") == "editar"
        assert intent("deshaz eso") == "deshacer" and intent("vuelve a la original") == "original"
        assert intent("otra vez") == "repetir"
        assert intent("hazme una imagen de un perro en la luna") == "nueva"
        for msg in ("que tiempo hace hoy?", "explicame este codigo", "agente: arregla el bug del login",
                    "arregla el error del archivo", "gracias!"):
            assert intent(msg) is None, msg
        # auditoria 2026-10-05: en OTRA conversacion "arregla..." no es la foto
        assert intent("corrige la cara", chat="conv-2") is None
        assert intent("corrige la cara", "code") is None


def test_undo_and_back_to_the_original_move_between_versions():
    session = {"user_id": None}
    ps = main.photo_session
    ps.start(session, "c", "v0", "subida")
    ps.push(session, "c", "v1", "ponme en la playa")
    ps.push(session, "c", "v2", "con un sombrero")
    state = ps.load(session, "c")
    assert ps.requests_so_far(state) == ["ponme en la playa", "con un sombrero"]
    assert ps.move(session, "c", "anterior") == "v1"
    ps.push(session, "c", "v3", "con gafas")  # lo deshecho (v2) se descarta
    assert ps.load(session, "c")["versions"] == ["v0", "v1", "v3"]
    assert ps.move(session, "c", "original") == "v0"
    assert ps.move(session, "c", "original") is None
