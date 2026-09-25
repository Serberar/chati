"""Comprueba si hay una version mas nueva de los modelos de Ollama ya
instalados, sin descargar nada - compara el digest del manifiesto local
(lo que ya tienes) contra el que ofrece el registro de Ollama (lo que hay
publicado), igual que 'docker pull' antes de bajar ninguna capa. Descargar
de verdad es una accion aparte y explicita (ver /models/update en main.py):
este modulo nunca actualiza nada por su cuenta."""

import json
import os
import time
from pathlib import Path

import requests

from paths import DATA_DIR

REGISTRY_BASE = "https://registry.ollama.ai/v2/library"
CACHE_PATH = DATA_DIR / "model_updates.json"
CACHE_TTL_SECONDS = 24 * 60 * 60


def _models_dir() -> Path:
    return Path(os.environ.get("OLLAMA_MODELS", str(Path.home() / ".ollama" / "models")))


def _library_dir() -> Path:
    return _models_dir() / "manifests" / "registry.ollama.ai" / "library"


def installed_official_models() -> list[tuple[str, str]]:
    """Lista (nombre, tag) de los modelos de la biblioteca oficial de Ollama
    instalados localmente (no de terceros - esos no tienen 'library/' como
    namespace y no seria seguro asumir que se pueden re-pullear igual)."""
    library_dir = _library_dir()
    if not library_dir.exists():
        return []
    found = []
    for name_dir in sorted(library_dir.iterdir()):
        if not name_dir.is_dir():
            continue
        for tag_file in sorted(name_dir.iterdir()):
            if tag_file.is_file():
                found.append((name_dir.name, tag_file.name))
    return found


def _model_layer_digest(manifest: dict) -> str | None:
    for layer in manifest.get("layers", []):
        if layer.get("mediaType") == "application/vnd.ollama.image.model":
            return layer.get("digest")
    return None


def local_digest(name: str, tag: str) -> str | None:
    path = _library_dir() / name / tag
    if not path.exists():
        return None
    manifest = json.loads(path.read_text(encoding="utf-8"))
    return _model_layer_digest(manifest)


def remote_digest(name: str, tag: str, timeout: int = 10) -> str | None:
    resp = requests.get(f"{REGISTRY_BASE}/{name}/manifests/{tag}", timeout=timeout)
    resp.raise_for_status()
    return _model_layer_digest(resp.json())


def _check_one(name: str, tag: str) -> dict:
    model_id = f"{name}:{tag}"
    # las variantes "-cpu" (num_gpu 0, fix del crash de flash-attention en
    # Blackwell, ver AGENTS.md) se crean localmente con `ollama create` a
    # partir de un modelo ya descargado - Ollama las guarda en la misma
    # carpeta de manifiestos que los modelos de verdad venidos del registro,
    # pero nunca han existido ahi y jamas lo estaran, comprobarlas contra el
    # registro solo produce un 404 seguro. Se detectan por convencion propia
    # (es el sufijo que usa este proyecto, ver ROADMAP.md) y se saltan.
    if tag.endswith("-cpu"):
        return {"model": model_id, "update_available": False, "error": None, "local_only": True}
    local = local_digest(name, tag)
    try:
        remote = remote_digest(name, tag)
        return {
            "model": model_id,
            "update_available": bool(local and remote and local != remote),
            "error": None,
        }
    except Exception as exc:
        return {"model": model_id, "update_available": False, "error": str(exc)}


def check_updates(force: bool = False) -> list[dict]:
    """Devuelve el estado de cada modelo instalado (¿hay version nueva o no?).
    No descarga nada, solo metadatos pequeños. Usa una cache de 24h para no
    golpear el registro en cada carga de la pagina, salvo que force=True
    (el usuario pulsa 'comprobar ahora')."""
    if not force and CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if time.time() - cached.get("checked_at_epoch", 0) < CACHE_TTL_SECONDS:
                return cached["results"]
        except (json.JSONDecodeError, KeyError):
            pass  # cache corrupta o con formato viejo: se recalcula

    results = [_check_one(name, tag) for name, tag in installed_official_models()]

    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(
        json.dumps({"checked_at_epoch": time.time(), "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return results
