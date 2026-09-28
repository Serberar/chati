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


def _png(width, height):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_avatar_is_cropped_square_encrypted_and_readable():
    import io
    from PIL import Image
    dek = Fernet.generate_key()

    profile_store.save_avatar(_png(1200, 800), "u1", dek, key_generation=1)

    raw_on_disk = (profile_store.PROFILE_DIR / "u1" / "avatar.jpg.enc").read_bytes()
    assert not raw_on_disk.startswith(b"\xff\xd8")  # no es un JPEG en claro
    img = Image.open(io.BytesIO(profile_store.get_avatar_bytes("u1", dek, key_generation=1)))
    assert img.size == (profile_store.AVATAR_SIZE, profile_store.AVATAR_SIZE)


def test_avatar_unreadable_after_key_generation_changes():
    dek = Fernet.generate_key()
    profile_store.save_avatar(_png(300, 300), "u1", dek, key_generation=1)
    assert profile_store.get_avatar_bytes("u1", Fernet.generate_key(), key_generation=2) is None


def test_avatar_rejects_non_images_and_can_be_removed():
    dek = Fernet.generate_key()
    with pytest.raises(ValueError):
        profile_store.save_avatar(b"esto no es una imagen", "u1", dek, key_generation=1)

    profile_store.save_avatar(_png(300, 300), "u1", dek, key_generation=1)
    profile_store.delete_avatar("u1")
    assert profile_store.get_avatar_bytes("u1", dek, key_generation=1) is None


def test_agent_shortcuts_defaults_until_saved_then_encrypted_roundtrip():
    dek = Fernet.generate_key()
    assert [s["name"] for s in profile_store.get_agent_shortcuts("u1", dek, 1)][0] == "Ordenar Descargas"

    saved = profile_store.save_agent_shortcuts(
        [{"name": "  Fotos  ", "task": "ordena mis fotos por año", "potente": True}], "u1", dek, 1)

    assert saved == [{"name": "Fotos", "task": "ordena mis fotos por año", "potente": True}]
    assert profile_store.get_agent_shortcuts("u1", dek, 1) == saved
    on_disk = (profile_store.PROFILE_DIR / "u1" / "agent_shortcuts.json.enc").read_bytes()
    assert b"fotos" not in on_disk.lower()
    # otra DEK (contraseña restablecida): no se pueden leer, vuelven los de ejemplo
    assert profile_store.get_agent_shortcuts("u1", Fernet.generate_key(), 2)[0]["name"] == "Ordenar Descargas"


def test_agent_shortcuts_need_name_and_task():
    with pytest.raises(ValueError):
        profile_store.save_agent_shortcuts([{"name": "x", "task": " "}], "u1", Fernet.generate_key(), 1)
