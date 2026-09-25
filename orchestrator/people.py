import json
import re
import shutil
from pathlib import Path

import crypto_utils
from paths import DATA_DIR

FACES_DIR = DATA_DIR / "faces"
FACES_DIR.mkdir(parents=True, exist_ok=True)

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9 _\-ÁÉÍÓÚÑáéíóúñÜü]")


def sanitize_name(name: str) -> str:
    name = name.strip()
    name = _SAFE_NAME.sub("", name)
    if not name:
        raise ValueError("Nombre invalido")
    return name


def person_dir(name: str, user_id: str | None = None) -> Path:
    """Sin user_id: carpeta compartida de siempre (retrocompatible con la
    galeria ya existente). Con user_id: carpeta privada de ese usuario - ver
    ROADMAP.md, punto 0, fase 3."""
    base = FACES_DIR / user_id if user_id else FACES_DIR
    return base / sanitize_name(name)


def save_person(name: str, image_bytes: bytes, ext: str, user_id: str | None = None,
                 dek: bytes | None = None, key_generation: int | None = None) -> Path:
    d = person_dir(name, user_id)
    d.mkdir(parents=True, exist_ok=True)
    # limpia referencias anteriores (cifradas o no, cualquier extension) para no acumular basura
    for old in list(d.glob("reference.*")) + list(d.glob("meta.json")):
        old.unlink()

    if dek is not None:
        ref_path = d / f"reference{ext}.enc"
        ref_path.write_bytes(crypto_utils.encrypt_bytes(dek, image_bytes))
        (d / "meta.json").write_text(json.dumps({"key_generation": key_generation, "ext": ext}), encoding="utf-8")
    else:
        ref_path = d / f"reference{ext}"
        ref_path.write_bytes(image_bytes)
    return ref_path


def get_reference_image(name: str, user_id: str | None = None) -> Path | None:
    """Solo para referencias SIN cifrar (legado o guardadas sin sesion de
    usuario) - si la referencia esta cifrada, devuelve None a proposito (no
    hay forma de servir un archivo cifrado como si fuera la imagen real).
    Usa get_reference_bytes() para el caso general, que si sabe descifrar."""
    d = person_dir(name, user_id)
    matches = list(d.glob("reference.*"))
    matches = [m for m in matches if not m.name.endswith(".enc")]
    return matches[0] if matches else None


def get_reference_bytes(name: str, user_id: str | None = None,
                         dek: bytes | None = None, key_generation: int | None = None) -> bytes | None:
    """Devuelve los bytes de la imagen de referencia, descifrando si hace
    falta. None si no existe, o si esta cifrada con una key_generation
    distinta a la actual (huerfana tras un reset de contraseña - zero-
    knowledge real, no se puede recuperar)."""
    d = person_dir(name, user_id)
    encrypted = list(d.glob("reference.*.enc"))
    if encrypted:
        meta_path = d / "meta.json"
        if not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if dek is None or meta.get("key_generation") != key_generation:
            return None
        return crypto_utils.decrypt_bytes(dek, encrypted[0].read_bytes())

    plain = get_reference_image(name, user_id)
    return plain.read_bytes() if plain else None


def list_people(user_id: str | None = None) -> list[dict]:
    base = FACES_DIR / user_id if user_id else FACES_DIR
    if not base.exists():
        return []
    result = []
    for d in sorted(base.iterdir()):
        if not d.is_dir():
            continue
        has_ref = list(d.glob("reference.*"))
        if has_ref:
            result.append({"name": d.name, "thumbnail_url": f"/people_photo/{d.name}"})
    return result


def delete_person(name: str, user_id: str | None = None) -> bool:
    d = person_dir(name, user_id)
    if not d.exists():
        return False
    for f in d.iterdir():
        f.unlink()
    d.rmdir()
    return True


def delete_all_for_user(user_id: str) -> None:
    """Borra toda la galeria de caras de un usuario - se usa cuando un admin
    borra la cuenta (ver ROADMAP.md, punto 0)."""
    d = FACES_DIR / user_id
    if d.exists():
        shutil.rmtree(d)
