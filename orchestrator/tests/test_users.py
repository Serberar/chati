import pytest

import users


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "DB_PATH", tmp_path / "users_test.db")
    monkeypatch.setattr(users, "REGISTRATION_KEY_FILE", tmp_path / "registration_key_test.txt")
    monkeypatch.setattr(users, "SECURITY_LOG_FILE", tmp_path / "security_events_test.jsonl")
    users.init_db()


def test_create_and_login_roundtrip():
    users.create_user("sergio", "contraseña-larga-123", "admin")

    session = users.login("sergio", "contraseña-larga-123")

    assert session["username"] == "sergio"
    assert session["role"] == "admin"
    assert session["key_generation"] == 1
    assert isinstance(session["dek"], bytes)


def test_login_with_wrong_password_fails():
    users.create_user("sergio", "contraseña-larga-123", "admin")

    with pytest.raises(users.UserError):
        users.login("sergio", "contraseña-incorrecta")


def test_login_with_unknown_username_fails():
    with pytest.raises(users.UserError):
        users.login("no-existe", "cualquier-cosa")


def test_cannot_create_duplicate_username():
    users.create_user("sergio", "contraseña-larga-123", "admin")

    with pytest.raises(users.UserError):
        users.create_user("sergio", "otra-contraseña-123", "user")
    with pytest.raises(users.UserError):  # "Sergio" se hacia pasar por "sergio"
        users.create_user("Sergio", "otra-contraseña-123", "user")


def test_short_password_rejected():
    with pytest.raises(users.UserError):
        users.create_user("sergio", "corta", "admin")


def test_two_users_get_different_deks():
    users.create_user("uno", "contraseña-larga-123", "user")
    users.create_user("dos", "contraseña-larga-456", "user")

    dek1 = users.login("uno", "contraseña-larga-123")["dek"]
    dek2 = users.login("dos", "contraseña-larga-456")["dek"]

    assert dek1 != dek2


def test_change_password_keeps_same_dek_accessible():
    users.create_user("sergio", "contraseña-vieja-123", "user")
    dek_before = users.login("sergio", "contraseña-vieja-123")["dek"]

    users.change_password("sergio", "contraseña-vieja-123", "contraseña-nueva-456")

    dek_after = users.login("sergio", "contraseña-nueva-456")["dek"]
    assert dek_after == dek_before  # misma clave de datos, solo cambio como se desbloquea


def test_change_password_invalidates_old_password():
    users.create_user("sergio", "contraseña-vieja-123", "user")
    users.change_password("sergio", "contraseña-vieja-123", "contraseña-nueva-456")

    with pytest.raises(users.UserError):
        users.login("sergio", "contraseña-vieja-123")


def test_change_password_requires_correct_old_password():
    users.create_user("sergio", "contraseña-vieja-123", "user")

    with pytest.raises(users.UserError):
        users.change_password("sergio", "contraseña-equivocada", "contraseña-nueva-456")


def test_security_question_reset_generates_new_dek_not_the_old_one():
    users.create_user(
        "sergio", "contraseña-vieja-123", "user",
        security_question="¿ciudad natal?", security_answer="Madrid",
    )
    dek_before = users.login("sergio", "contraseña-vieja-123")["dek"]

    users.reset_via_security_question("sergio", "madrid", "contraseña-nueva-789")

    session_after = users.login("sergio", "contraseña-nueva-789")
    assert session_after["dek"] != dek_before  # zero-knowledge real: datos viejos quedan huerfanos
    assert session_after["key_generation"] == 2


def test_security_question_reset_is_case_insensitive_on_answer():
    users.create_user(
        "sergio", "contraseña-vieja-123", "user",
        security_question="¿ciudad natal?", security_answer="Madrid",
    )
    users.reset_via_security_question("sergio", "  MADRID  ", "contraseña-nueva-789")
    users.login("sergio", "contraseña-nueva-789")  # no lanza


def test_security_question_reset_rejects_wrong_answer():
    users.create_user(
        "sergio", "contraseña-vieja-123", "user",
        security_question="¿ciudad natal?", security_answer="Madrid",
    )
    with pytest.raises(users.UserError):
        users.reset_via_security_question("sergio", "Barcelona", "contraseña-nueva-789")


def test_security_question_reset_fails_without_question_configured():
    users.create_user("sergio", "contraseña-vieja-123", "user")  # sin pregunta de seguridad

    with pytest.raises(users.UserError):
        users.reset_via_security_question("sergio", "lo que sea", "contraseña-nueva-789")


def test_old_password_stops_working_after_security_question_reset():
    users.create_user(
        "sergio", "contraseña-vieja-123", "user",
        security_question="¿ciudad natal?", security_answer="Madrid",
    )
    users.reset_via_security_question("sergio", "Madrid", "contraseña-nueva-789")

    with pytest.raises(users.UserError):
        users.login("sergio", "contraseña-vieja-123")


def test_delete_user_removes_account():
    users.create_user("sergio", "contraseña-larga-123", "admin")

    deleted = users.delete_user("sergio")

    assert deleted is True
    with pytest.raises(users.UserError):
        users.login("sergio", "contraseña-larga-123")


def test_delete_unknown_user_returns_false():
    assert users.delete_user("no-existe") is False


def test_list_users_never_exposes_secrets():
    users.create_user("sergio", "contraseña-larga-123", "admin")
    users.create_user("invitado-real", "otra-contraseña-123", "user")

    listing = users.list_users()

    usernames = {u["username"] for u in listing}
    assert usernames == {"sergio", "invitado-real"}
    for u in listing:
        assert "password_hash" not in u
        assert "wrapped_dek" not in u
        assert "salt" not in u


def test_any_users_exist_reflects_state():
    assert users.any_users_exist() is False
    users.create_user("sergio", "contraseña-larga-123", "admin")
    assert users.any_users_exist() is True


def test_registration_key_is_stable_across_calls():
    key1 = users.get_or_create_registration_key()
    key2 = users.get_or_create_registration_key()
    assert key1 == key2
    assert len(key1) > 10


def test_security_question_reset_logs_an_event():
    users.create_user(
        "sergio", "contraseña-vieja-123", "user",
        security_question="¿ciudad natal?", security_answer="Madrid",
    )
    users.reset_via_security_question("sergio", "Madrid", "contraseña-nueva-789")

    events = users.list_security_events()

    assert len(events) == 1
    assert events[0]["event"] == "password_reset_via_security_question"
    assert events[0]["username"] == "sergio"


def test_list_security_events_empty_when_nothing_logged():
    assert users.list_security_events() == []


def test_display_name_roundtrip_and_clear():
    users.create_user("sergio", "contraseña-larga-123", "admin")

    users.set_display_name("sergio", "  Sergio Bernabé ")
    assert users.get_user("sergio")["display_name"] == "Sergio Bernabé"

    users.set_display_name("sergio", "")
    assert users.get_user("sergio")["display_name"] is None


def test_display_name_too_long_is_rejected():
    users.create_user("sergio", "contraseña-larga-123", "admin")
    with pytest.raises(users.UserError):
        users.set_display_name("sergio", "x" * 41)


def test_set_security_question_requires_password_and_enables_reset():
    users.create_user("sergio", "contraseña-larga-123", "admin")

    with pytest.raises(users.UserError):
        users.set_security_question("sergio", "contraseña-mala", "¿Mascota?", "Toby")

    users.set_security_question("sergio", "contraseña-larga-123", "¿Mascota?", "Toby")
    assert users.get_user("sergio")["security_question"] == "¿Mascota?"
    users.reset_via_security_question("sergio", "toby", "otra-contraseña-456")
    assert users.login("sergio", "otra-contraseña-456")["username"] == "sergio"


def test_init_db_adds_display_name_to_old_databases(tmp_path, monkeypatch):
    import sqlite3
    old_db = tmp_path / "vieja.db"
    with sqlite3.connect(old_db) as conn:
        conn.execute("CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, role TEXT NOT NULL, "
                     "password_hash TEXT NOT NULL, salt BLOB NOT NULL, wrapped_dek BLOB NOT NULL, "
                     "key_generation INTEGER NOT NULL DEFAULT 1, security_question TEXT, "
                     "security_answer_hash TEXT, created_at TEXT NOT NULL)")
    monkeypatch.setattr(users, "DB_PATH", old_db)

    users.init_db()
    users.create_user("sergio", "contraseña-larga-123", "admin")

    assert users.get_user("sergio")["display_name"] is None


def test_guest_sessions_are_capped():
    import auth_sessions
    tokens = [auth_sessions.create_guest_session() for _ in range(auth_sessions.MAX_GUEST_SESSIONS + 10)]
    alive = [t for t in tokens if auth_sessions.get_session(t)]
    assert len(alive) == auth_sessions.MAX_GUEST_SESSIONS
    assert auth_sessions.get_session(tokens[-1])  # la recien creada siempre vale
    for t in tokens:
        auth_sessions.destroy_session(t)
