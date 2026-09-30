"""Paso del instalador que descarga los modelos que el usuario eligio en el
asistente (ver setup/chati_installer.iss) - reutiliza model_catalog.py como
unica fuente de verdad de que hay y de donde se baja, asi que nunca hay dos
copias de una URL que puedan desincronizarse.

Se puede llamar de dos formas:
  installer_download_models.py --selected-file RUTA   (lo que usa el instalador)
  installer_download_models.py --selected id1 id2 ...  (para probarlo a mano)

Imprime una linea por paso a stdout - el instalador (o quien lo llame) puede
mostrar eso tal cual como progreso, no hace falta parsear nada mas fino."""

import argparse
import os
import shutil
import subprocess
import tempfile
import sys
import time
from pathlib import Path

import requests

import model_catalog
import model_registry
from paths import DATA_DIR, MODELS_DIR

OLLAMA_URL = "http://127.0.0.1:11434"
LOG_FILE = DATA_DIR / "logs" / "instalacion_modelos.log"


def _destination_for(entry: model_catalog.CatalogEntry) -> Path:
    if entry.modality == "imagen":
        return model_registry.image_model_folder(entry.architecture) / entry.filename
    if entry.modality == "video":
        return model_registry.video_model_folder(entry.architecture) / entry.filename
    if entry.modality == "voz":
        return MODELS_DIR / "voice" / "piper" / entry.filename
    raise ValueError(f"Modalidad sin carpeta de destino conocida: {entry.modality}")


def _download_file(url: str, dest: Path) -> None:
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
        with open(tmp_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8 * 1024 * 1024):
                f.write(chunk)
                written += len(chunk)
                if total:
                    pct = int(written * 100 / total)
                    if pct != last_pct_shown and pct % 10 == 0:
                        print(f"  {dest.name}: {pct}%")
                        last_pct_shown = pct
    tmp_path.replace(dest)


CPU_SUFFIX = "-cpu"
GPU_SUFFIX = "-gpu"
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
    _ensure_ollama_server()
    print(f"  ollama pull {base}")
    subprocess.run([_ollama_exe(), "pull", base], check=True)
    params = ((["num_gpu 0"] if model.endswith(CPU_SUFFIX) else [])
              + (["num_gpu 999"] if model.endswith(GPU_SUFFIX) else []) + _EXTRA_PARAMS.get(model, []))
    if not params:
        return
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
    all_entries = {e.id: e for tier in ["cpu_only", "minimo", "8gb", "12gb", "16gb_plus"]
                   for e in model_catalog.catalog_for_tier(tier)}
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
                _download_file(entry.download_url, _destination_for(entry))
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
    args = parser.parse_args()

    if args.selected_file:
        ids = [line.strip() for line in args.selected_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        ids = args.selected

    failed = download_selected(ids)
    if failed:
        _log(f"No se pudieron descargar: {', '.join(failed)}. Detalles en {LOG_FILE}")
        sys.exit(1)


if __name__ == "__main__":
    main()
