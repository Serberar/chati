import io
import json
import shutil
from pathlib import Path

from PIL import Image, ImageOps, UnidentifiedImageError

import crypto_utils
from paths import DATA_DIR
import atomic

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


AVATAR_SIZE = 256


def save_avatar(image_bytes: bytes, user_id: str, dek: bytes, key_generation: int) -> None:
    """Recorta al centro en cuadrado y reduce a 256px antes de cifrar - una
    foto del movil de varios MB no hace falta entera para un circulo de 30px.
    Lanza ValueError si no es una imagen."""
    try:
        img = Image.open(io.BytesIO(image_bytes))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("El archivo no es una imagen valida.") from exc
    img = ImageOps.fit(img, (AVATAR_SIZE, AVATAR_SIZE))
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)

    d = _user_dir(user_id)
    (d / "avatar.jpg.enc").write_bytes(crypto_utils.encrypt_bytes(dek, out.getvalue()))
    (d / "avatar_meta.json").write_text(json.dumps({"key_generation": key_generation}), encoding="utf-8")


def get_avatar_bytes(user_id: str, dek: bytes, key_generation: int) -> bytes | None:
    d = _user_dir(user_id)
    path, meta_path = d / "avatar.jpg.enc", d / "avatar_meta.json"
    if not path.exists() or not meta_path.exists():
        return None
    # tras restablecer la contraseña con la pregunta de seguridad la DEK es
    # otra - el avatar viejo ya no se puede descifrar, se trata como "sin avatar"
    if json.loads(meta_path.read_text(encoding="utf-8")).get("key_generation") != key_generation:
        return None
    return crypto_utils.decrypt_bytes(dek, path.read_bytes())


def delete_avatar(user_id: str) -> None:
    d = _user_dir(user_id)
    for name in ("avatar.jpg.enc", "avatar_meta.json"):
        (d / name).unlink(missing_ok=True)


# Atajos del modo Agente: tareas que se repiten, a un clic. Los de ejemplo
# solo se muestran mientras el usuario no haya guardado los suyos.
DEFAULT_AGENT_SHORTCUTS = [
    {"name": "Ordenar Descargas", "potente": False,
     "task": "Ordena mi carpeta de Descargas en subcarpetas por tipo de archivo "
             "(Documentos, Imagenes, Videos, Comprimidos y Otros). No borres nada."},
    {"name": "Archivos mas grandes", "potente": False,
     "task": "Dime cuales son los 10 archivos mas grandes de mi carpeta de usuario y cuanto ocupa cada uno. "
             "No cambies nada."},
    {"name": "Limpiar temporales", "potente": False,
     "task": "Dime cuanto espacio ocupan los archivos de mi carpeta temporal (%TEMP%) con mas de 7 dias y, "
             "si te doy permiso, borralos."},
]
MAX_SHORTCUTS = 30


def get_agent_shortcuts(user_id: str, dek: bytes, key_generation: int) -> list[dict]:
    d = _user_dir(user_id)
    path, meta_path = d / "agent_shortcuts.json.enc", d / "agent_shortcuts_meta.json"
    if not path.exists() or not meta_path.exists():
        return [dict(s) for s in DEFAULT_AGENT_SHORTCUTS]
    if json.loads(meta_path.read_text(encoding="utf-8")).get("key_generation") != key_generation:
        # tras restablecer la contraseña (DEK nueva) los viejos no se pueden leer
        return [dict(s) for s in DEFAULT_AGENT_SHORTCUTS]
    raw = crypto_utils.decrypt_bytes(dek, path.read_bytes())
    return json.loads(raw.decode("utf-8")) if raw else [dict(s) for s in DEFAULT_AGENT_SHORTCUTS]


def save_agent_shortcuts(shortcuts: list[dict], user_id: str, dek: bytes, key_generation: int) -> list[dict]:
    """Sustituye la lista entera. Lanza ValueError si algo no vale."""
    clean = []
    for s in shortcuts[:MAX_SHORTCUTS]:
        name, task = str(s.get("name", "")).strip()[:40], str(s.get("task", "")).strip()[:2000]
        if not name or not task:
            raise ValueError("Cada atajo necesita un nombre y la tarea a hacer.")
        clean.append({"name": name, "task": task, "potente": bool(s.get("potente", False))})
    d = _user_dir(user_id)
    atomic.write_bytes(d / "agent_shortcuts.json.enc",
        crypto_utils.encrypt_bytes(dek, json.dumps(clean, ensure_ascii=False).encode("utf-8")))
    (d / "agent_shortcuts_meta.json").write_text(json.dumps({"key_generation": key_generation}), encoding="utf-8")
    return clean


def delete_all_for_user(user_id: str) -> None:
    """Borra el CV de un usuario - se usa cuando un admin borra la cuenta
    (ver ROADMAP.md, punto 0)."""
    d = PROFILE_DIR / user_id
    if d.exists():
        shutil.rmtree(d)
