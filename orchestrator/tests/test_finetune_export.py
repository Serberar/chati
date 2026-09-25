import json

from cryptography.fernet import Fernet

import memory
from finetune_export import export_user_conversations_chatml


def _fresh_db(tmp_path, monkeypatch, name="finetune_export_test.db"):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / name)
    memory.init_db()


def test_export_writes_one_chatml_line_per_session_with_enough_turns(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    dek = Fernet.generate_key()

    sid1 = memory.new_session_id()
    memory.add_message(sid1, "user", "hola, que tal?", dek=dek, key_generation=1, user_id="u1")
    memory.add_message(sid1, "assistant", "muy bien, gracias", dek=dek, key_generation=1, user_id="u1")

    sid2 = memory.new_session_id()
    memory.add_message(sid2, "user", "solo un mensaje suelto", dek=dek, key_generation=1, user_id="u1")

    out_path = tmp_path / "export.jsonl"
    count = export_user_conversations_chatml("u1", dek, 1, out_path, min_turns=2)

    assert count == 1  # sid2 descartado por no llegar a min_turns
    lines = out_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    doc = json.loads(lines[0])
    assert doc == {"messages": [
        {"role": "user", "content": "hola, que tal?"},
        {"role": "assistant", "content": "muy bien, gracias"},
    ]}


def test_export_only_includes_the_given_users_sessions(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    dek = Fernet.generate_key()

    sid_mine = memory.new_session_id()
    memory.add_message(sid_mine, "user", "mi pregunta", dek=dek, key_generation=1, user_id="u1")
    memory.add_message(sid_mine, "assistant", "mi respuesta", dek=dek, key_generation=1, user_id="u1")

    sid_other = memory.new_session_id()
    memory.add_message(sid_other, "user", "pregunta de otro usuario", dek=dek, key_generation=1, user_id="u2")
    memory.add_message(sid_other, "assistant", "respuesta de otro usuario", dek=dek, key_generation=1, user_id="u2")

    out_path = tmp_path / "export.jsonl"
    count = export_user_conversations_chatml("u1", dek, 1, out_path, min_turns=2)

    assert count == 1
    doc = json.loads(out_path.read_text(encoding="utf-8").strip())
    assert "otro usuario" not in json.dumps(doc)


def test_export_skips_content_from_a_stale_key_generation(tmp_path, monkeypatch):
    """Mensajes cifrados con una DEK anterior a un reset de contraseña (ver
    ROADMAP.md punto 0) no se pueden descifrar con la DEK actual - no deben
    colarse en el export como texto ilegible."""
    _fresh_db(tmp_path, monkeypatch)
    old_dek = Fernet.generate_key()
    new_dek = Fernet.generate_key()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "mensaje de antes del reset", dek=old_dek, key_generation=1, user_id="u1")
    memory.add_message(sid, "assistant", "respuesta de antes del reset", dek=old_dek, key_generation=1, user_id="u1")

    out_path = tmp_path / "export.jsonl"
    # se exporta con la DEK nueva (generation 2), como pasaria de verdad tras un reset
    count = export_user_conversations_chatml("u1", new_dek, 2, out_path, min_turns=2)

    assert count == 0  # el unico hilo no se pudo descifrar del todo, se descarta


def test_export_trims_a_trailing_user_message_with_no_reply(tmp_path, monkeypatch):
    """Reproduce un caso real visto en pruebas en vivo: la generacion de la
    respuesta se interrumpe/falla a mitad, el mensaje del usuario ya se
    guardo pero el de assistant nunca llega - ese turno colgado no es un
    ejemplo de entrenamiento valido y debe descartarse."""
    _fresh_db(tmp_path, monkeypatch)
    dek = Fernet.generate_key()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "primera pregunta", dek=dek, key_generation=1, user_id="u1")
    memory.add_message(sid, "assistant", "primera respuesta", dek=dek, key_generation=1, user_id="u1")
    memory.add_message(sid, "user", "segunda pregunta sin respuesta", dek=dek, key_generation=1, user_id="u1")

    out_path = tmp_path / "export.jsonl"
    count = export_user_conversations_chatml("u1", dek, 1, out_path, min_turns=2)

    assert count == 1
    doc = json.loads(out_path.read_text(encoding="utf-8").strip())
    assert doc["messages"] == [
        {"role": "user", "content": "primera pregunta"},
        {"role": "assistant", "content": "primera respuesta"},
    ]


def test_export_returns_zero_for_user_with_no_sessions(tmp_path, monkeypatch):
    _fresh_db(tmp_path, monkeypatch)
    dek = Fernet.generate_key()

    out_path = tmp_path / "export.jsonl"
    count = export_user_conversations_chatml("nadie", dek, 1, out_path, min_turns=2)

    assert count == 0
    assert out_path.read_text(encoding="utf-8") == ""
