"""Actualizaciones de Chati desde las Releases de GitHub (Serberar/chati).

Comprobar solo lee los datos de la ultima release. Instalar lo pide el
administrador con un boton: se baja el instalador, se compara su huella
SHA-256 con la que publica GitHub para ese archivo y se lanza (pide permiso
de administrador: el codigo vive en Archivos de Programa). El instalador
cierra Chati, cambia el codigo y la vuelve a abrir (setup/chati_installer.iss)."""

import hashlib
import json
import os
import re
import time

import requests

from paths import DATA_DIR
from version import VERSION

LATEST_URL = "https://api.github.com/repos/Serberar/chati/releases/latest"
ASSET_NAME = "ChatiIA-Instalador.exe"
CACHE_PATH = DATA_DIR / "app_update.json"
# GitHub deja 60 consultas por hora sin cuenta: se pregunta cada 6 h salvo
# que el administrador pulse "Buscar actualizaciones"
CACHE_TTL_SECONDS = 6 * 60 * 60
DOWNLOAD_DIR = DATA_DIR / "updates"


class UpdateError(Exception):
    pass


def _parse(version: str) -> tuple[int, ...]:
    return tuple(int(n) for n in re.findall(r"\d+", version)[:3])


def is_newer(latest: str, current: str) -> bool:
    return _parse(latest) > _parse(current)


def _fetch_latest() -> dict:
    resp = requests.get(LATEST_URL, timeout=10, headers={"Accept": "application/vnd.github+json"})
    resp.raise_for_status()
    release = resp.json()
    asset = next((a for a in release.get("assets", []) if a.get("name") == ASSET_NAME), {})
    return {
        "version": str(release.get("tag_name", "")).lstrip("v"),
        "notes": release.get("body") or "",
        "url": asset.get("browser_download_url"),
        # sin huella no se instala: es lo que garantiza que el .exe es el publicado
        "sha256": (asset.get("digest") or "").removeprefix("sha256:").lower(),
    }


def _installable(latest: dict) -> bool:
    return bool(latest["url"] and latest["sha256"] and is_newer(latest["version"], VERSION))


def check(force: bool = False) -> dict:
    latest = None
    if not force and CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if time.time() - cached["checked_at"] < CACHE_TTL_SECONDS:
                latest = cached["latest"]
        except (ValueError, KeyError):
            pass
    if latest is None:
        try:
            latest = _fetch_latest()
        except (requests.RequestException, ValueError) as exc:
            return {"current": VERSION, "latest": None, "available": False, "notes": "", "error": str(exc)}
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps({"checked_at": time.time(), "latest": latest}, ensure_ascii=False),
                              encoding="utf-8")
    return {"current": VERSION, "latest": latest["version"], "available": _installable(latest),
            "notes": latest["notes"], "error": None}


def _download(url: str, sha256: str, target) -> None:
    partial = target.with_suffix(".part")
    digest = hashlib.sha256()
    try:
        with requests.get(url, stream=True, timeout=60) as resp:
            resp.raise_for_status()
            with open(partial, "wb") as out:
                for chunk in resp.iter_content(1 << 20):
                    out.write(chunk)
                    digest.update(chunk)
    except requests.RequestException as exc:
        partial.unlink(missing_ok=True)
        raise UpdateError(f"No se pudo descargar la version nueva: {exc}") from exc
    if digest.hexdigest() != sha256:
        partial.unlink(missing_ok=True)
        raise UpdateError("El instalador descargado no coincide con el publicado en GitHub. No se instala.")
    os.replace(partial, target)


def install() -> str:
    """Descarga la ultima version y lanza su instalador. Devuelve la version."""
    try:
        latest = _fetch_latest()  # no la cache: la direccion y la huella tienen que ser las de ahora
    except (requests.RequestException, ValueError) as exc:
        raise UpdateError(f"No se pudo consultar GitHub: {exc}") from exc
    if not _installable(latest):
        raise UpdateError(f"No hay ninguna version mas nueva que la {VERSION}.")
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    for old in DOWNLOAD_DIR.iterdir():  # instaladores de actualizaciones anteriores
        old.unlink(missing_ok=True)
    target = DOWNLOAD_DIR / f"ChatiIA-Instalador-{latest['version']}.exe"
    _download(latest["url"], latest["sha256"], target)
    try:
        # Sin "runas": el instalador pide el permiso de administrador el mismo
        # y recuerda al usuario normal para volver a abrir Chati sin permisos
        # (runasoriginaluser). Lanzado ya elevado, Chati volvia a abrirse como
        # administrador (2026-10-21). /UPDATE=1: sin preguntas ni modelos.
        os.startfile(str(target), "open", "/SILENT /SUPPRESSMSGBOXES /NORESTART /UPDATE=1")
    except OSError as exc:
        raise UpdateError(f"No se pudo abrir el instalador: {exc}") from exc
    return latest["version"]
