import pytest
from cryptography.fernet import Fernet

import people


@pytest.fixture(autouse=True)
def isolate_faces_dir(tmp_path, monkeypatch):
    d = tmp_path / "faces"
    d.mkdir()
    monkeypatch.setattr(people, "FACES_DIR", d)
    yield d


def test_save_and_get_reference_image(tmp_path):
    path = people.save_person("Blanca", b"fake image bytes", ".jpg")
    assert path.exists()
    assert path.read_bytes() == b"fake image bytes"

    ref = people.get_reference_image("Blanca")
    assert ref == path


def test_save_person_replaces_previous_extension():
    people.save_person("Maria de la O", b"v1", ".jpg")
    people.save_person("Maria de la O", b"v2", ".png")

    d = people.person_dir("Maria de la O")
    refs = list(d.glob("reference.*"))
    assert len(refs) == 1
    assert refs[0].suffix == ".png"
    assert refs[0].read_bytes() == b"v2"


def test_list_people_only_lists_dirs_with_reference():
    people.save_person("Sergio", b"data", ".jpg")
    people.person_dir("VacioSinFoto").mkdir()  # carpeta sin referencia, no deberia listarse

    listed = {p["name"] for p in people.list_people()}
    assert listed == {"Sergio"}


def test_delete_person():
    people.save_person("Temporal", b"data", ".jpg")
    assert people.delete_person("Temporal") is True
    assert people.get_reference_image("Temporal") is None
    assert people.delete_person("Temporal") is False  # ya no existe


def test_sanitize_name_strips_path_traversal_characters():
    # los caracteres de ruta se eliminan, no se permite que compongan una carpeta fuera de FACES_DIR
    cleaned = people.sanitize_name("Sergio/Bernabe")
    assert "/" not in cleaned and "\\" not in cleaned

    cleaned2 = people.sanitize_name("../../etc/passwd")
    assert "/" not in cleaned2 and ".." not in cleaned2


def test_sanitize_name_rejects_name_that_becomes_empty():
    # un nombre compuesto solo por caracteres no permitidos queda vacio tras limpiar
    with pytest.raises(ValueError):
        people.sanitize_name("../..")


def test_person_dir_scoped_by_user_id_when_given():
    d_shared = people.person_dir("Ana")
    d_user = people.person_dir("Ana", user_id="user-123")
    assert d_shared != d_user
    assert "user-123" in str(d_user)


def test_two_users_can_have_a_person_with_the_same_name_isolated():
    people.save_person("Ana", b"foto de usuario uno", ".jpg", user_id="user-1")
    people.save_person("Ana", b"foto de usuario dos", ".jpg", user_id="user-2")

    assert people.get_reference_image("Ana", user_id="user-1").read_bytes() == b"foto de usuario uno"
    assert people.get_reference_image("Ana", user_id="user-2").read_bytes() == b"foto de usuario dos"


def test_list_people_scoped_by_user_id():
    people.save_person("Solo de uno", b"x", ".jpg", user_id="user-1")
    people.save_person("Compartida", b"x", ".jpg")  # sin user_id, carpeta de siempre

    assert {p["name"] for p in people.list_people(user_id="user-1")} == {"Solo de uno"}
    assert {p["name"] for p in people.list_people()} == {"Compartida"}


def test_save_person_with_dek_encrypts_on_disk():
    dek = Fernet.generate_key()
    path = people.save_person("Cifrada", b"contenido real de la foto", ".jpg", dek=dek, key_generation=1)

    assert path.name.endswith(".enc")
    assert b"contenido real de la foto" not in path.read_bytes()


def test_get_reference_image_returns_none_for_encrypted_entry():
    # el getter basado en Path es solo para legado sin cifrar, a proposito
    dek = Fernet.generate_key()
    people.save_person("Cifrada", b"contenido", ".jpg", dek=dek, key_generation=1)

    assert people.get_reference_image("Cifrada") is None


def test_get_reference_bytes_decrypts_with_correct_dek():
    dek = Fernet.generate_key()
    people.save_person("Cifrada", b"contenido real de la foto", ".jpg", dek=dek, key_generation=1)

    result = people.get_reference_bytes("Cifrada", dek=dek, key_generation=1)

    assert result == b"contenido real de la foto"


def test_get_reference_bytes_returns_none_for_orphaned_key_generation():
    old_dek = Fernet.generate_key()
    new_dek = Fernet.generate_key()
    people.save_person("Cifrada", b"contenido", ".jpg", dek=old_dek, key_generation=1)

    result = people.get_reference_bytes("Cifrada", dek=new_dek, key_generation=2)

    assert result is None


def test_get_reference_bytes_works_for_legacy_unencrypted_entries():
    people.save_person("SinCifrar", b"foto de siempre", ".jpg")  # sin dek

    result = people.get_reference_bytes("SinCifrar")

    assert result == b"foto de siempre"


def test_delete_all_for_user_removes_only_that_users_folder():
    people.save_person("Ana", b"foto de uno", ".jpg", user_id="user-1")
    people.save_person("Ana", b"foto de dos", ".jpg", user_id="user-2")

    people.delete_all_for_user("user-1")

    assert people.list_people(user_id="user-1") == []
    assert {p["name"] for p in people.list_people(user_id="user-2")} == {"Ana"}


def test_delete_all_for_user_does_not_raise_when_nothing_to_delete():
    people.delete_all_for_user("usuario-sin-datos")  # no lanza


def test_save_person_replaces_encrypted_reference_cleanly():
    dek = Fernet.generate_key()
    people.save_person("Cifrada", b"v1", ".jpg", dek=dek, key_generation=1)
    people.save_person("Cifrada", b"v2", ".png", dek=dek, key_generation=1)

    d = people.person_dir("Cifrada")
    refs = list(d.glob("reference.*"))
    assert len(refs) == 1
    assert people.get_reference_bytes("Cifrada", dek=dek, key_generation=1) == b"v2"
