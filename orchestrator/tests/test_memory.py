import contextlib

from cryptography.fernet import Fernet

import memory


def test_add_and_get_history(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_test.db")
    memory.init_db()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "hola")
    memory.add_message(sid, "assistant", "hola, como estas?", agent="text")

    history = memory.get_history(sid)
    assert len(history) == 2
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "hola"
    assert history[1]["role"] == "assistant"
    assert history[1]["agent"] == "text"


def test_history_respects_limit_and_order(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_test2.db")
    memory.init_db()

    sid = memory.new_session_id()
    for i in range(5):
        memory.add_message(sid, "user", f"mensaje {i}")

    history = memory.get_history(sid, limit=3)
    assert len(history) == 3
    # deben ser los 3 mas recientes, en orden cronologico
    assert [h["content"] for h in history] == ["mensaje 2", "mensaje 3", "mensaje 4"]


def test_update_and_delete_message(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_test3.db")
    memory.init_db()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "texto original")
    msg_id = memory.get_history(sid)[0]["id"]

    assert memory.update_message(msg_id, "texto editado") is True
    assert memory.get_history(sid)[0]["content"] == "texto editado"

    assert memory.delete_message(msg_id) is True
    assert memory.get_history(sid) == []


def test_clear_session_only_affects_that_session(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_test4.db")
    memory.init_db()

    sid1 = memory.new_session_id()
    sid2 = memory.new_session_id()
    memory.add_message(sid1, "user", "sesion 1")
    memory.add_message(sid2, "user", "sesion 2")

    memory.clear_session(sid1)

    assert memory.get_history(sid1) == []
    assert len(memory.get_history(sid2)) == 1


def test_list_sessions_filters_by_user_id(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_users.db")
    memory.init_db()

    sid1 = memory.new_session_id()
    sid2 = memory.new_session_id()
    memory.add_message(sid1, "user", "mensaje de usuario uno", user_id="user-1")
    memory.add_message(sid2, "user", "mensaje de usuario dos", user_id="user-2")

    sessions_user1 = memory.list_sessions(user_id="user-1")
    assert {s["session_id"] for s in sessions_user1} == {sid1}

    sessions_all = memory.list_sessions()
    assert {s["session_id"] for s in sessions_all} == {sid1, sid2}


def test_delete_user_messages_removes_only_that_user(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_deluser.db")
    memory.init_db()

    sid1 = memory.new_session_id()
    sid2 = memory.new_session_id()
    memory.add_message(sid1, "user", "de usuario uno", user_id="user-1")
    memory.add_message(sid2, "user", "de usuario dos", user_id="user-2")

    deleted = memory.delete_user_messages("user-1")

    assert deleted == 1
    assert memory.get_history(sid1) == []
    assert len(memory.get_history(sid2)) == 1


def test_encrypted_message_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_enc1.db")
    memory.init_db()
    dek = Fernet.generate_key()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "esto es privado de verdad", dek=dek, key_generation=1)

    history = memory.get_history(sid, dek=dek, key_generation=1)
    assert history[0]["content"] == "esto es privado de verdad"


def test_encrypted_message_stored_as_ciphertext_not_plaintext(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_enc2.db")
    memory.init_db()
    dek = Fernet.generate_key()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "secreto de verdad", dek=dek, key_generation=1)

    with contextlib.closing(memory._connect()) as conn:
        row = conn.execute("SELECT content FROM messages WHERE session_id = ?", (sid,)).fetchone()
    assert "secreto de verdad" not in row[0]


def test_message_from_orphaned_key_generation_shows_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_enc3.db")
    memory.init_db()
    old_dek = Fernet.generate_key()
    new_dek = Fernet.generate_key()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "mensaje de antes del reset", dek=old_dek, key_generation=1)

    # tras un reset de contraseña: nueva dek, key_generation sube a 2
    history = memory.get_history(sid, dek=new_dek, key_generation=2)
    assert history[0]["content"] == memory.ORPHANED_PLACEHOLDER


def test_reading_without_dek_returns_ciphertext_gibberish_not_plaintext(tmp_path, monkeypatch):
    # si alguien lee memory.db sin sesion de usuario (dek=None), nunca debe
    # ver el texto real de un mensaje cifrado
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_enc4.db")
    memory.init_db()
    dek = Fernet.generate_key()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "informacion privada", dek=dek, key_generation=1)

    history = memory.get_history(sid)  # sin dek
    assert history[0]["content"] != "informacion privada"


def test_legacy_plaintext_messages_still_readable_without_dek(tmp_path, monkeypatch):
    # mensajes guardados antes del sistema de usuarios (sin cifrar) deben
    # seguir siendo legibles tal cual - retrocompatibilidad
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_enc5.db")
    memory.init_db()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "mensaje antiguo sin cifrar")  # sin dek

    history = memory.get_history(sid, dek=Fernet.generate_key(), key_generation=1)
    assert history[0]["content"] == "mensaje antiguo sin cifrar"


def test_list_sessions_includes_title_when_set(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_titles1.db")
    memory.init_db()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "hola")
    memory.set_session_title(sid, "Ideas para el cumpleanos")

    sessions = memory.list_sessions()
    assert sessions[0]["title"] == "Ideas para el cumpleanos"


def test_list_sessions_title_is_none_when_never_set(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_titles2.db")
    memory.init_db()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "hola")

    sessions = memory.list_sessions()
    assert sessions[0]["title"] is None


def test_set_session_title_can_be_renamed(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_titles3.db")
    memory.init_db()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "hola")
    memory.set_session_title(sid, "Nombre viejo")
    memory.set_session_title(sid, "Nombre nuevo")

    assert memory.list_sessions()[0]["title"] == "Nombre nuevo"


def test_session_title_encrypted_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_titles4.db")
    memory.init_db()
    dek = Fernet.generate_key()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "hola", dek=dek, key_generation=1, user_id="u1")
    memory.set_session_title(sid, "Titulo privado de verdad", user_id="u1", dek=dek, key_generation=1)

    sessions = memory.list_sessions(user_id="u1", dek=dek, key_generation=1)
    assert sessions[0]["title"] == "Titulo privado de verdad"

    with contextlib.closing(memory._connect()) as conn:
        row = conn.execute("SELECT title FROM session_titles WHERE session_id = ?", (sid,)).fetchone()
    assert "Titulo privado de verdad" not in row[0]  # cifrado en disco, no en texto plano


def test_session_title_orphaned_after_password_reset_shows_placeholder(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_titles5.db")
    memory.init_db()
    old_dek = Fernet.generate_key()
    new_dek = Fernet.generate_key()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "hola", dek=old_dek, key_generation=1, user_id="u1")
    memory.set_session_title(sid, "Titulo de antes del reset", user_id="u1", dek=old_dek, key_generation=1)

    sessions = memory.list_sessions(user_id="u1", dek=new_dek, key_generation=2)
    assert sessions[0]["title"] == memory.ORPHANED_PLACEHOLDER


def test_clear_session_also_removes_its_title(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_titles6.db")
    memory.init_db()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "hola")
    memory.set_session_title(sid, "Se va a borrar")

    memory.clear_session(sid)

    with contextlib.closing(memory._connect()) as conn:
        row = conn.execute("SELECT * FROM session_titles WHERE session_id = ?", (sid,)).fetchone()
    assert row is None


def test_delete_user_messages_also_removes_their_titles(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_titles7.db")
    memory.init_db()

    sid1 = memory.new_session_id()
    sid2 = memory.new_session_id()
    memory.add_message(sid1, "user", "de usuario uno", user_id="user-1")
    memory.add_message(sid2, "user", "de usuario dos", user_id="user-2")
    memory.set_session_title(sid1, "Titulo de uno", user_id="user-1")
    memory.set_session_title(sid2, "Titulo de dos", user_id="user-2")

    memory.delete_user_messages("user-1")

    with contextlib.closing(memory._connect()) as conn:
        row1 = conn.execute("SELECT * FROM session_titles WHERE session_id = ?", (sid1,)).fetchone()
        row2 = conn.execute("SELECT * FROM session_titles WHERE session_id = ?", (sid2,)).fetchone()
    assert row1 is None
    assert row2 is not None


def test_update_message_re_encrypts_with_dek(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "memory_enc6.db")
    memory.init_db()
    dek = Fernet.generate_key()

    sid = memory.new_session_id()
    memory.add_message(sid, "user", "version original", dek=dek, key_generation=1)
    msg_id = memory.get_history(sid, dek=dek, key_generation=1)[0]["id"]

    memory.update_message(msg_id, "version editada", dek=dek, key_generation=1)

    history = memory.get_history(sid, dek=dek, key_generation=1)
    assert history[0]["content"] == "version editada"
