import pytest
from cryptography.fernet import Fernet

import profile_store


@pytest.fixture(autouse=True)
def isolate_profile_dir(tmp_path, monkeypatch):
    d = tmp_path / "profile"
    d.mkdir()
    monkeypatch.setattr(profile_store, "PROFILE_DIR", d)
    yield d


def test_save_and_get_cv_roundtrip():
    path = profile_store.save_cv("mi_cv.pdf", b"contenido del cv")
    assert path.exists()
    assert path.read_bytes() == b"contenido del cv"
    assert profile_store.get_cv() == path


def test_save_cv_replaces_previous_extension():
    profile_store.save_cv("v1.pdf", b"version 1")
    profile_store.save_cv("v2.docx", b"version 2")

    matches = list(profile_store.PROFILE_DIR.glob("cv.*"))
    assert len(matches) == 1
    assert matches[0].suffix == ".docx"
    assert matches[0].read_bytes() == b"version 2"


def test_get_cv_none_when_nothing_saved():
    assert profile_store.get_cv() is None


def test_cv_scoped_by_user_id():
    profile_store.save_cv("cv1.pdf", b"cv de usuario uno", user_id="user-1")
    profile_store.save_cv("cv2.pdf", b"cv de usuario dos", user_id="user-2")

    assert profile_store.get_cv(user_id="user-1").read_bytes() == b"cv de usuario uno"
    assert profile_store.get_cv(user_id="user-2").read_bytes() == b"cv de usuario dos"
    assert profile_store.get_cv() is None  # el compartido de siempre sigue vacio


def test_save_cv_with_dek_encrypts_on_disk():
    dek = Fernet.generate_key()
    path = profile_store.save_cv("cv.pdf", b"contenido sensible del cv", dek=dek, key_generation=1)

    assert path.name.endswith(".enc")
    assert b"contenido sensible del cv" not in path.read_bytes()


def test_get_cv_returns_none_for_encrypted_entry():
    dek = Fernet.generate_key()
    profile_store.save_cv("cv.pdf", b"contenido", dek=dek, key_generation=1)

    assert profile_store.get_cv() is None


def test_get_cv_bytes_decrypts_with_correct_dek():
    dek = Fernet.generate_key()
    profile_store.save_cv("cv.pdf", b"contenido sensible del cv", dek=dek, key_generation=1)

    assert profile_store.get_cv_bytes(dek=dek, key_generation=1) == b"contenido sensible del cv"


def test_get_cv_bytes_returns_none_for_orphaned_key_generation():
    old_dek = Fernet.generate_key()
    new_dek = Fernet.generate_key()
    profile_store.save_cv("cv.pdf", b"contenido", dek=old_dek, key_generation=1)

    assert profile_store.get_cv_bytes(dek=new_dek, key_generation=2) is None


def test_get_cv_bytes_works_for_legacy_unencrypted_entries():
    profile_store.save_cv("cv.pdf", b"cv de siempre")  # sin dek

    assert profile_store.get_cv_bytes() == b"cv de siempre"


def test_delete_all_for_user_removes_only_that_users_cv():
    profile_store.save_cv("cv1.pdf", b"cv de uno", user_id="user-1")
    profile_store.save_cv("cv2.pdf", b"cv de dos", user_id="user-2")

    profile_store.delete_all_for_user("user-1")

    assert profile_store.get_cv(user_id="user-1") is None
    assert profile_store.get_cv(user_id="user-2").read_bytes() == b"cv de dos"


def test_delete_all_for_user_does_not_raise_when_nothing_to_delete():
    profile_store.delete_all_for_user("usuario-sin-datos")  # no lanza
