"""Rutas del proyecto, calculadas a partir de donde vive este archivo, no
fijas a C:\\AI - para que la carpeta entera se pueda mover, renombrar, o
vivir en un disco externo sin romper nada. Ver ROADMAP.md, punto 3b."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
MODELS_DIR = PROJECT_ROOT / "models"
