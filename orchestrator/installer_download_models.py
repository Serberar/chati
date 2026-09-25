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
import subprocess
import sys
from pathlib import Path

import requests

import model_catalog
import model_registry
from paths import MODELS_DIR


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


def _pull_ollama_model(model: str) -> None:
    print(f"  ollama pull {model}")
    subprocess.run(["ollama", "pull", model], check=True)


def download_selected(entry_ids: list[str]) -> None:
    all_entries = {e.id: e for tier in ["cpu_only", "minimo", "8gb", "12gb", "16gb_plus"]
                   for e in model_catalog.catalog_for_tier(tier)}
    for entry_id in entry_ids:
        entry = all_entries.get(entry_id)
        if entry is None:
            print(f"AVISO: id de catalogo desconocido, se ignora: {entry_id}", file=sys.stderr)
            continue
        print(f"[{entry.modality}] {entry.label}")
        if entry.ollama_model:
            _pull_ollama_model(entry.ollama_model)
        elif entry.download_url:
            dest = _destination_for(entry)
            _download_file(entry.download_url, dest)
        print(f"  hecho: {entry.label}")


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

    download_selected(ids)


if __name__ == "__main__":
    main()
