import os
import time

from cryptography.fernet import Fernet

import media_store
import paths


def test_saved_media_is_encrypted_and_only_its_owner_can_open_it(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "OUTPUT_DIR", tmp_path)
    mine, other = Fernet.generate_key(), Fernet.generate_key()
    name = media_store.save(b"\x89PNG secreto", ".png", mine)
    assert media_store.valid_name(name)
    assert b"secreto" not in (tmp_path / "media" / (name + ".enc")).read_bytes()
    assert media_store.load(name, mine) == b"\x89PNG secreto"
    assert media_store.load(name, other) is None
    assert media_store.load("../../users.db", mine) is None


def test_plain_copies_never_stay_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "OUTPUT_DIR", tmp_path)
    with media_store.plain_copy(b"foto", ".png") as p:
        assert p.read_bytes() == b"foto"
    assert not p.exists()


def test_old_media_is_cleaned_up(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "OUTPUT_DIR", tmp_path)
    key = Fernet.generate_key()
    old, new = media_store.save(b"a", ".png", key), media_store.save(b"b", ".png", key)
    old_file = tmp_path / "media" / (old + ".enc")
    past = time.time() - (media_store.MEDIA_MAX_AGE_DAYS + 1) * 86400
    os.utime(old_file, (past, past))
    assert media_store.cleanup_old_media() == 1
    assert not old_file.exists() and media_store.load(new, key) == b"b"
