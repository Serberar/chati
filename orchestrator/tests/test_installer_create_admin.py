import pytest

import installer_create_admin
import users


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(users, "DB_PATH", tmp_path / "users_test.db")
    monkeypatch.setattr(users, "SECURITY_LOG_FILE", tmp_path / "security_events_test.jsonl")
    users.init_db()


def _write(tmp_path, content):
    path = tmp_path / "chati_admin.txt"
    path.write_text(content, encoding="utf-8")
    return path


def test_creates_admin_and_deletes_the_file(tmp_path):
    path = _write(tmp_path, "sergio\n contraseña con espacios \n¿Mascota?\nToby\n")

    installer_create_admin.create_admin_from_file(path)

    assert not path.exists()
    session = users.login("sergio", " contraseña con espacios ")
    assert session["role"] == "admin"
    assert users.get_user("sergio")["security_question"] == "¿Mascota?"


def test_security_question_is_optional(tmp_path):
    path = _write(tmp_path, "sergio\ncontraseña-larga-123\n")

    installer_create_admin.create_admin_from_file(path)

    assert users.login("sergio", "contraseña-larga-123")["role"] == "admin"


def test_does_nothing_if_users_already_exist(tmp_path):
    users.create_user("existente", "contraseña-larga-123", "admin")
    path = _write(tmp_path, "otro\ncontraseña-larga-123\n\n\n")

    result = installer_create_admin.create_admin_from_file(path)

    assert "Ya existen" in result
    assert users.get_user("otro") is None
    assert not path.exists()
