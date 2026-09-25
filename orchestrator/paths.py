"""Rutas del proyecto - separadas en dos raices, no una sola (ver
ROADMAP.md, "instalador con deteccion de GPU"):

- CODE_ROOT: donde vive el codigo (orchestrator/, setup/) - en una
  instalacion hecha con el instalador .exe esto es C:\\Program Files\\Chati
  IA\\, que Windows bloquea sin permisos de administrador. De solo lectura
  en uso normal (iconos, plantillas de workflow).
- DATA_ROOT: donde vive todo lo que la app escribe mientras funciona
  (modelos descargados, datos cifrados de usuarios, imagenes/videos
  generados) - nunca deberia vivir dentro de Archivos de Programa, Windows
  bloquearia cada escritura nueva, no solo la instalacion inicial. Por
  defecto %LOCALAPPDATA%\\ChatiIA, para que anadir un modelo nuevo desde la
  propia app nunca pida permisos de administrador.

Se puede forzar con la variable de entorno CHATI_DATA_ROOT - por ejemplo
para mantener el uso "todo en una sola carpeta" de antes de separar
codigo y datos (ver punto 3b, llevar la carpeta entera a otro equipo a
mano), apuntandola al mismo sitio que el codigo."""

import os
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent

_data_root_override = os.environ.get("CHATI_DATA_ROOT")
if _data_root_override:
    DATA_ROOT = Path(_data_root_override)
elif os.environ.get("LOCALAPPDATA"):
    DATA_ROOT = Path(os.environ["LOCALAPPDATA"]) / "ChatiIA"
else:
    # sin LOCALAPPDATA (Linux/Mac, o un Windows raro sin esa variable) -
    # cae al lado del codigo, el comportamiento de siempre antes de separar
    DATA_ROOT = CODE_ROOT

DATA_DIR = DATA_ROOT / "data"
MODELS_DIR = DATA_ROOT / "models"
OUTPUT_DIR = DATA_ROOT / "outputs"
