"""Registro de modelos de imagen/video instalados: escanea subcarpetas por
arquitectura y expone lo que hay disponible, sin tocar codigo cada vez que
se añade un modelo nuevo de una arquitectura ya soportada. Ver ROADMAP.md
y pendiente/pendiente.md (vision original de Sergio para este sistema).

Convenio de carpetas (subcarpeta = arquitectura, dentro del tipo de nodo de
ComfyUI que le corresponde - los archivos ya viven ahi por como ComfyUI
carga modelos, esto solo añade una capa de organizacion encima):
  models/img/checkpoints/sdxl/<archivo>.safetensors
  models/img/checkpoints/sd15/<archivo>.safetensors
  models/img/diffusion_models/flux/<archivo>.gguf   (unet; clip/vae son compartidos, no van aqui)
  models/vid/checkpoints/<arquitectura>/<archivo>.safetensors

Añadir un modelo de una arquitectura YA soportada = soltar el archivo en su
subcarpeta, se detecta solo la proxima vez que se liste. Una arquitectura
nueva de verdad (ni SDXL, ni SD1.5, ni FLUX, ni las que haya en
VIDEO_ARCHITECTURES) no se puede detectar sola - hace falta escribirle su
propia plantilla de workflow primero. Ese es el techo tecnico real: no es
magia universal, es "facil de ampliar dentro de lo ya soportado"."""

from dataclasses import dataclass
from pathlib import Path

from paths import MODELS_DIR

IMG_DIR = MODELS_DIR / "img"
VID_DIR = MODELS_DIR / "vid"

# "comfy_dir": la carpeta de ComfyUI (segun extra_model_paths.yaml) donde
# vive el tipo de archivo que carga esta arquitectura - "checkpoints" para
# modelos de fichero unico (SDXL, SD1.5, ...), "diffusion_models" para FLUX
# (que separa unet/clip/vae en archivos distintos, clip y vae son
# compartidos entre variantes de FLUX y no viven en esta subcarpeta).
IMAGE_ARCHITECTURES = {
    "sdxl": {"comfy_dir": "checkpoints", "label": "SDXL", "extensions": (".safetensors",)},
    "sd15": {"comfy_dir": "checkpoints", "label": "SD 1.5", "extensions": (".safetensors",)},
    "flux": {"comfy_dir": "diffusion_models", "label": "FLUX", "extensions": (".safetensors", ".gguf")},
    # solo edita una foto que se le da (ver photo_edit.py), no genera desde
    # texto: no sale en la lista de generadores
    "flux_kontext": {"comfy_dir": "diffusion_models", "label": "FLUX Kontext (editar fotos)",
                     "extensions": (".safetensors", ".gguf"), "edit_only": True},
}

# Solo LTX-Video por ahora - deliberadamente abierto a añadir mas
# arquitecturas de video segun haga falta (ver pendiente/pendiente.md),
# no se han precreado plantillas para modelos que Sergio todavia no ha
# pedido en concreto.
VIDEO_ARCHITECTURES = {
    "ltxv": {"comfy_dir": "checkpoints", "label": "LTX-Video", "extensions": (".safetensors",)},
}


@dataclass
class ModelEntry:
    id: str            # "sdxl:sd_xl_base_1.0" - estable, se usa para elegir el modelo
    architecture: str  # "sdxl"
    architecture_label: str  # "SDXL"
    label: str          # "sd_xl_base_1.0" (nombre de archivo sin extension)
    comfy_path: str     # "sdxl\\sd_xl_base_1.0.safetensors" - separador nativo del SO, lo que espera ComfyUI


def _scan(base_dir: Path, architectures: dict) -> list[ModelEntry]:
    entries = []
    for arch, meta in architectures.items():
        arch_dir = base_dir / meta["comfy_dir"] / arch
        if not arch_dir.exists():
            continue
        for f in sorted(arch_dir.iterdir()):
            if not f.is_file() or f.suffix.lower() not in meta["extensions"]:
                continue
            entries.append(ModelEntry(
                id=f"{arch}:{f.stem}",
                architecture=arch,
                architecture_label=meta["label"],
                label=f.stem,
                comfy_path=str(Path(arch) / f.name),
            ))
    return entries


def list_image_models() -> list[ModelEntry]:
    return [m for m in _scan(IMG_DIR, IMAGE_ARCHITECTURES)
            if not IMAGE_ARCHITECTURES[m.architecture].get("edit_only")]


def get_edit_model() -> ModelEntry | None:
    edit_archs = {a: meta for a, meta in IMAGE_ARCHITECTURES.items() if meta.get("edit_only")}
    return next(iter(_scan(IMG_DIR, edit_archs)), None)


def list_video_models() -> list[ModelEntry]:
    return _scan(VID_DIR, VIDEO_ARCHITECTURES)


def get_image_model(model_id: str | None) -> ModelEntry | None:
    """model_id=None -> "automatico": FLUX si hay (rapido, calidad alta, el
    generador de texto-a-imagen principal hasta ahora), si no el primero
    que exista. model_id concreto -> ese exacto, o None si ya no existe
    (se borro el archivo, por ejemplo)."""
    models = list_image_models()
    if not models:
        return None
    if model_id:
        return next((m for m in models if m.id == model_id), None)
    return next((m for m in models if m.architecture == "flux"), models[0])


def get_video_model(model_id: str | None) -> ModelEntry | None:
    models = list_video_models()
    if not models:
        return None
    if model_id:
        return next((m for m in models if m.id == model_id), None)
    return models[0]


def image_model_folder(architecture: str) -> Path | None:
    """Ruta absoluta de la subcarpeta donde hay que soltar un archivo nuevo
    de esta arquitectura - para el "Añadir modelo" de la interfaz."""
    meta = IMAGE_ARCHITECTURES.get(architecture)
    if not meta:
        return None
    return IMG_DIR / meta["comfy_dir"] / architecture


def video_model_folder(architecture: str) -> Path | None:
    meta = VIDEO_ARCHITECTURES.get(architecture)
    if not meta:
        return None
    return VID_DIR / meta["comfy_dir"] / architecture


def image_model_path(model_id: str) -> Path | None:
    """Ruta absoluta del ARCHIVO instalado (no la carpeta) - para poder
    borrarlo desde la interfaz. None si el model_id no existe."""
    entry = get_image_model(model_id)
    if entry is None:
        return None
    return image_model_folder(entry.architecture) / Path(entry.comfy_path).name


def video_model_path(model_id: str) -> Path | None:
    entry = get_video_model(model_id)
    if entry is None:
        return None
    return video_model_folder(entry.architecture) / Path(entry.comfy_path).name


def list_image_architectures() -> list[dict]:
    """Una fila por cada arquitectura de imagen SOPORTADA (no solo las que ya
    tienen algo instalado) - para el "Añadir modelo" de Opciones > Modelos,
    que tiene que enseñar donde soltar un archivo nuevo aunque la carpeta
    todavia este vacia."""
    installed = _scan(IMG_DIR, IMAGE_ARCHITECTURES)
    return [
        {
            "id": arch,
            "label": meta["label"],
            "folder": str(image_model_folder(arch)),
            "installed_count": sum(1 for m in installed if m.architecture == arch),
        }
        for arch, meta in IMAGE_ARCHITECTURES.items()
    ]


def list_video_architectures() -> list[dict]:
    installed = list_video_models()
    return [
        {
            "id": arch,
            "label": meta["label"],
            "folder": str(video_model_folder(arch)),
            "installed_count": sum(1 for m in installed if m.architecture == arch),
        }
        for arch, meta in VIDEO_ARCHITECTURES.items()
    ]
