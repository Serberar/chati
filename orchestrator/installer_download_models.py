"""Paso del instalador que descarga los modelos que el usuario eligio en el
asistente (ver setup/chati_installer.iss) - reutiliza model_catalog.py como
unica fuente de verdad de que hay y de donde se baja, asi que nunca hay dos
copias de una URL que puedan desincronizarse.

Se puede llamar de tres formas:
  installer_download_models.py --selected-file RUTA   (lo que usa el instalador)
  installer_download_models.py --new-base              (al actualizar, ver new_base_models)
  installer_download_models.py --selected id1 id2 ...  (para probarlo a mano)

Imprime una linea por paso a stdout - el instalador (o quien lo llame) puede
mostrar eso tal cual como progreso, no hace falta parsear nada mas fino."""

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

import model_catalog
import model_registry
from paths import DATA_DIR, MODELS_DIR

OLLAMA_URL = "http://127.0.0.1:11434"
LOG_FILE = DATA_DIR / "logs" / "instalacion_modelos.log"
# ids del catalogo que ya se ofrecieron (en el asistente o en una
# actualizacion): lo que se desmarco al instalar no se vuelve a bajar
OFFERED_FILE = DATA_DIR / "modelos_ofrecidos.txt"


def _offered() -> set[str]:
    return set(OFFERED_FILE.read_text(encoding="utf-8").split()) if OFFERED_FILE.exists() else set()


def _all_entries() -> dict[str, model_catalog.CatalogEntry]:
    return {e.id: e for tier in ["cpu_only", "minimo", "8gb", "12gb", "16gb_plus"]
            for e in model_catalog.catalog_for_tier(tier)}


def new_base_models() -> list[str]:
    """Modelos base (recommended_by_default) que una version nueva de Chati
    anade al catalogo: se bajan solos al actualizar. Sin registro (instalado
    antes de que existiera) se repasan todos: los que ya estan no se bajan."""
    offered = _offered()
    return [i for i, e in _all_entries().items() if e.recommended_by_default and i not in offered]


def _destination_for(entry: model_catalog.CatalogEntry) -> Path:
    if entry.modality == "imagen":
        return model_registry.image_model_folder(entry.architecture) / entry.filename
    if entry.modality == "video":
        return model_registry.video_model_folder(entry.architecture) / entry.filename
    if entry.modality == "voz":
        return MODELS_DIR / "voice" / "piper" / entry.filename
    raise ValueError(f"Modalidad sin carpeta de destino conocida: {entry.modality}")


def _download_file(url: str, dest: Path, sha256: str | None = None) -> None:
    """Descarga a un .part y solo lo deja como modelo si llega entero y con la
    huella esperada (sha256, ver model_catalog.CatalogEntry.sha256)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        print(f"  ya estaba descargado: {dest.name}")
        return
    tmp_path = dest.with_suffix(dest.suffix + ".part")
    with requests.get(url, stream=True, timeout=30) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        written = 0
        last_pct_shown = -1
        digest = hashlib.sha256()
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                f.write(chunk)
                digest.update(chunk)
                written += len(chunk)
                if total:
                    pct = int(written * 100 / total)
                    if pct != last_pct_shown and pct % 10 == 0:
                        print(f"  {dest.name}: {pct}%")
                        last_pct_shown = pct
    if total and written != total:  # cortada a medias: no dejar un modelo roto como si estuviera bien
        tmp_path.unlink(missing_ok=True)
        raise OSError(f"descarga incompleta de {dest.name}: {written} de {total} bytes")
    if sha256 and digest.hexdigest() != sha256.lower():
        tmp_path.unlink(missing_ok=True)
        raise OSError(f"{dest.name} no es el archivo esperado (huella distinta): no se instala")
    tmp_path.replace(dest)


CPU_SUFFIX = "-cpu"
GPU_SUFFIX = "-gpu"
# modelos que no estan en la biblioteca oficial de Ollama: de donde se bajan
_PULL_FROM = {"qwen3-abliterated:14b": "huihui_ai/qwen3-abliterated:14b"}
# Los dos son tambien modelos del agente (OpenCode, ver opencode_client.py): su
# prompt con las herramientas no cabe en el contexto por defecto - con menos,
# Ollama lo recorta en silencio y el agente pierde sus instrucciones.
_EXTRA_PARAMS = {"qwen3-coder:30b-cpu": ["num_ctx 32768"], "qwen2.5:7b": ["num_ctx 16384"],
                 "qwen3:8b": ["num_ctx 16384"],
                 # vision en GPU: contexto corto para que quepa entera en 8 GB de VRAM
                 "qwen2.5vl:7b-gpu": ["num_ctx 4096"]}


def _ollama_exe() -> str:
    """El instalador lanza este script con el PATH de ANTES de instalar Ollama:
    "ollama" a secas no se encontraba y la descarga fallaba en silencio (Windows
    Sandbox, 2026-09-30: el chat decia "model 'qwen3:8b' not found")."""
    found = shutil.which("ollama")
    if found:
        return found
    candidates = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
                  Path(os.environ.get("ProgramFiles", "")) / "Ollama" / "ollama.exe"]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    raise RuntimeError("No se encuentra Ollama instalado.")


def _ensure_ollama_server() -> None:
    """Que haya un servidor de Ollama escuchando, y guardando en la carpeta de
    modelos de Chati (si no, "ollama pull" no tiene a quien pedirselo)."""
    try:
        requests.get(f"{OLLAMA_URL}/api/tags", timeout=3).raise_for_status()
        return
    except requests.RequestException:
        pass
    env = {**os.environ, "OLLAMA_MODELS": str(MODELS_DIR / "text"), "OLLAMA_FLASH_ATTENTION": "0"}
    subprocess.Popen([_ollama_exe(), "serve"], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    for _ in range(30):
        time.sleep(1)
        try:
            requests.get(f"{OLLAMA_URL}/api/tags", timeout=3).raise_for_status()
            return
        except requests.RequestException:
            continue
    raise RuntimeError("Ollama no arranca: no se pueden descargar los modelos de texto.")


def _pull_ollama_model(model: str) -> None:
    # las variantes "-cpu"/"-gpu" (ver AGENTS.md) no existen en el registro de
    # Ollama - se crean en local a partir del modelo base
    base = model.removesuffix(CPU_SUFFIX).removesuffix(GPU_SUFFIX)
    base = _PULL_FROM.get(base, base)
    _ensure_ollama_server()
    print(f"  ollama pull {base}")
    subprocess.run([_ollama_exe(), "pull", base], check=True)
    params = ((["num_gpu 0"] if model.endswith(CPU_SUFFIX) else [])
              + (["num_gpu 999"] if model.endswith(GPU_SUFFIX) else []) + _EXTRA_PARAMS.get(model, []))
    if params:
        _create_variant(model, base, params)
    for variant, variant_params in _EXTRA_VARIANTS.get(model, []):
        _create_variant(variant, base, variant_params)


# Variantes que se crean ademas de la pedida (mismo archivo, otros ajustes):
# el agente de codigo usa qwen3-coder con 12 capas en la GPU (ver config.yaml,
# medido el 2026-10-13: 4,5 veces mas rapido que solo en CPU).
_EXTRA_VARIANTS = {"qwen3-coder:30b-cpu": [("qwen3-coder:30b-mixto", ["num_gpu 12", "num_ctx 32768"])]}


def _create_variant(model: str, base: str, params: list[str]) -> None:
    modelfile = "\n".join([f"FROM {base}", *(f"PARAMETER {p}" for p in params)]) + "\n"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "Modelfile"
        path.write_text(modelfile, encoding="utf-8")
        print(f"  ollama create {model}")
        subprocess.run([_ollama_exe(), "create", model, "-f", str(path)], check=True)


def _log(line: str, error: bool = False) -> None:
    print(line, flush=True, file=sys.stderr if error else sys.stdout)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {line}\n")
    except OSError:
        pass


def download_selected(entry_ids: list[str]) -> list[str]:
    """Descarga lo elegido; si uno falla, sigue con los demas (antes el primer
    fallo cortaba todo y en silencio). Devuelve los que fallaron."""
    all_entries = _all_entries()
    failed = []
    for entry_id in entry_ids:
        entry = all_entries.get(entry_id)
        if entry is None:
            _log(f"AVISO: id de catalogo desconocido, se ignora: {entry_id}", error=True)
            continue
        _log(f"[{entry.modality}] {entry.label}")
        try:
            if entry.ollama_model:
                _pull_ollama_model(entry.ollama_model)
            elif entry.download_url:
                _download_file(entry.download_url, _destination_for(entry), entry.sha256)
            for url, relative, sha256 in entry.extra_files:
                _download_file(url, model_registry.IMG_DIR / relative, sha256)
            _log(f"  hecho: {entry.label}")
        except (OSError, RuntimeError, subprocess.CalledProcessError, requests.RequestException) as exc:
            _log(f"  FALLO: {entry.label}: {exc}", error=True)
            failed.append(entry.label)
    return failed


def main() -> None:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--selected-file", type=Path, help="Archivo con un id de catalogo por linea")
    group.add_argument("--selected", nargs="+", help="Ids de catalogo directamente")
    group.add_argument("--new-base", action="store_true", help="Los modelos base que aun no se ofrecieron")
    args = parser.parse_args()

    if args.selected_file:
        ids = [line.strip() for line in args.selected_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    elif args.new_base:
        ids = new_base_models()
    else:
        ids = args.selected

    if download(ids):
        sys.exit(1)


def download(ids: list[str]) -> list[str]:
    """Descarga y apunta lo ofrecido. Devuelve los que fallaron."""
    failed = download_selected(ids)
    # todo el catalogo cuenta como ofrecido (el asistente lo enseña entero),
    # salvo lo que fallo: se reintenta la proxima vez
    failed_ids = {i for i, e in _all_entries().items() if e.label in failed}
    OFFERED_FILE.parent.mkdir(parents=True, exist_ok=True)
    OFFERED_FILE.write_text("\n".join(sorted((_offered() | set(_all_entries())) - failed_ids)) + "\n",
                            encoding="utf-8")
    if failed:
        _log(f"No se pudieron descargar: {', '.join(failed)}. Detalles en {LOG_FILE}")
    return failed


def download_new_base() -> None:
    """Al arrancar Chati: los modelos base que trae una version nueva. Antes lo
    hacia el instalador como administrador y en la 1.4.0 ese python ni arrancaba
    (0xc0000142, 2026-10-23); los modelos van a la carpeta del usuario, no hace falta."""
    ids = new_base_models()
    if ids:
        _log(f"Modelos base nuevos de esta version: {', '.join(ids)}")
        download(ids)


if __name__ == "__main__":
    main()
