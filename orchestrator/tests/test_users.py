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
