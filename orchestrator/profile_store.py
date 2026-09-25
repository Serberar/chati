import json
import shutil
from pathlib import Path

import crypto_utils
from paths import DATA_DIR

PROFILE_DIR = DATA_DIR / "profile"
PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def _user_dir(user_id: str | None) -> Path:
    """Sin user_id: carpeta compartida de siempre (retrocompatible). Con
    user_id: carpeta privada de ese usuario - ver ROADMAP.md, punto 0, fase 3."""
    d = PROFILE_DIR / user_id if user_id else PROFILE_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_cv(filename: str, content: bytes, user_id: str | None = None,
            dek: bytes | None = None, key_generation: int | None = None) -> Path:
    d = _user_dir(user_id)
    ext = Path(filename).suffix or ".pdf"
    # borra cualquier CV anterior (cifrado o no, otra extension incluida) para que solo haya uno activo
    for old in list(d.glob("cv.*")) + list(d.glob("meta.json")):
        old.unlink()

    if dek is not None:
        cv_path = d / f"cv{ext}.enc"
        cv_path.write_bytes(crypto_utils.encrypt_bytes(dek, content))
        (d / "meta.json").write_text(json.dumps({"key_generation": key_generation, "ext": ext}), encoding="utf-8")
    else:
        cv_path = d / f"cv{ext}"
        cv_path.write_bytes(content)
    return cv_path


def get_cv(user_id: str | None = None) -> Path | None:
    """Solo para CVs SIN cifrar (legado o guardados sin sesion) - para el
    caso general usa get_cv_bytes(), que si sabe descifrar."""
    d = _user_dir(user_id)
    matches = [m for m in d.glob("cv.*") if not m.name.endswith(".enc")]
    return matches[0] if matches else None


def cv_filename(user_id: str | None = None) -> str | None:
    """Nombre del CV guardado (cifrado o no), sin necesitar descifrarlo -
    para mostrar el estado ('tienes un CV subido: x.pdf') sin exponer nada."""
    d = _user_dir(user_id)
    encrypted = list(d.glob("cv.*.enc"))
    if encrypted:
        return encrypted[0].name.removesuffix(".enc")
    plain = get_cv(user_id)
    return plain.name if plain else None


def get_cv_bytes(user_id: str | None = None,
                  dek: bytes | None = None, key_generation: int | None = None) -> bytes | None:
    d = _user_dir(user_id)
    encrypted = list(d.glob("cv.*.enc"))
    if encrypted:
        meta_path = d / "meta.json"
        if not meta_path.exists():
            return None
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        if dek is None or meta.get("key_generation") != key_generation:
            return None
        return crypto_utils.decrypt_bytes(dek, encrypted[0].read_bytes())

    plain = get_cv(user_id)
    return plain.read_bytes() if plain else None


def delete_all_for_user(user_id: str) -> None:
    """Borra el CV de un usuario - se usa cuando un admin borra la cuenta
    (ver ROADMAP.md, punto 0)."""
    d = PROFILE_DIR / user_id
    if d.exists():
        shutil.rmtree(d)
