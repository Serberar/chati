import io
import json
import time
from pathlib import Path

import cv2
import numpy as np
import requests
from PIL import Image

import model_registry
from agents.comfyui_client import submit_and_wait

WORKFLOW_PATH = Path(__file__).parent.parent / "workflows" / "flux_image.json"
SDXL_WORKFLOW_PATH = Path(__file__).parent.parent / "workflows" / "sdxl_image.json"
FACEID_WORKFLOW_PATH = Path(__file__).parent.parent / "workflows" / "faceid_image.json"
UPSCALE_WORKFLOW_PATH = Path(__file__).parent.parent / "workflows" / "upscale_image.json"
INPAINT_WORKFLOW_PATH = Path(__file__).parent.parent / "workflows" / "inpaint_image.json"
CONTROLNET_WORKFLOW_PATH = Path(__file__).parent.parent / "workflows" / "controlnet_image.json"

# Que placeholder de checkpoint usa la plantilla de cada arquitectura - FLUX
# separa el unet en un archivo aparte (UnetLoaderGGUF), el resto son
# checkpoints de un solo archivo (CheckpointLoaderSimple). Ver model_registry.py.
_CHECKPOINT_PLACEHOLDER_BY_ARCH = {"flux": "__UNET_PATH__"}


class NoModelInstalledError(Exception):
    """No hay ningun modelo de la arquitectura necesaria instalado - ver
    model_registry.py y pendiente/pendiente.md sobre como añadir uno."""

# Nombres que acepta SetUnionControlNetType en ComfyUI para el modelo union-sdxl-1.0
# (ver https://huggingface.co/xinsir/controlnet-union-sdxl-1.0) - "canny" es el unico
# tipo que preprocesamos nosotros mismos (deteccion de bordes clasica, sin red
# neuronal de por medio); el resto quedan fuera de alcance por ahora.
CONTROL_TYPE_MAP = {"canny": "canny/lineart/anime_lineart/mlsd"}


def compute_canny_edges(image_bytes: bytes, low_threshold: int = 100, high_threshold: int = 200) -> bytes:
    """Deteccion de bordes con el algoritmo de Canny clasico (OpenCV, sin
    modelo neuronal) - es el mapa de control que espera el ControlNet, no la
    imagen original. Devuelve un PNG en escala de grises replicado a RGB
    (formato que ComfyUI espera para el nodo LoadImage)."""
    image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    gray = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2GRAY)
    edges = cv2.Canny(gray, low_threshold, high_threshold)
    edges_rgb = cv2.cvtColor(edges, cv2.COLOR_GRAY2RGB)
    out = io.BytesIO()
    Image.fromarray(edges_rgb).save(out, format="PNG")
    return out.getvalue()


class ImageAgent:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.template = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.sdxl_template = json.loads(SDXL_WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.faceid_template = json.loads(FACEID_WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.upscale_template = json.loads(UPSCALE_WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.inpaint_template = json.loads(INPAINT_WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.controlnet_template = json.loads(CONTROLNET_WORKFLOW_PATH.read_text(encoding="utf-8"))

    def _sdxl_checkpoint_path(self, model_id: str | None = None) -> str:
        """El checkpoint SDXL que usan FaceID/Inpaint/ControlNet - esas tres
        tecnicas siguen limitadas a SDXL por ahora (ver ROADMAP.md). Con
        model_id se usa ese checkpoint SDXL concreto; sin el, el primero
        instalado."""
        sdxl_entries = [m for m in model_registry.list_image_models() if m.architecture == "sdxl"]
        if not sdxl_entries:
            raise NoModelInstalledError(
                "No hay ningun modelo SDXL instalado - hace falta uno en "
                f"{model_registry.image_model_folder('sdxl')} para esta funcion."
            )
        if model_id:
            entry = next((m for m in sdxl_entries if m.id == model_id), None)
            if entry is None:
                raise NoModelInstalledError(f"El modelo SDXL '{model_id}' ya no esta instalado.")
            return entry.comfy_path
        return sdxl_entries[0].comfy_path

    def generate(self, prompt: str, width: int = 1024, height: int = 1024, timeout: int = 480,
                 model_id: str | None = None) -> bytes:
        """model_id: id de model_registry.py (p.ej. "flux:flux1-schnell-Q4_K_S")
        o None para el automatico (FLUX si hay, si no el primero que exista)."""
        entry = model_registry.get_image_model(model_id)
        if entry is None:
            raise NoModelInstalledError(
                "No hay ningun modelo de imagen instalado. Añade uno en "
                f"{model_registry.IMG_DIR} (ver Opciones > Modelos > Imagen)."
            )
        template = self.template if entry.architecture == "flux" else self.sdxl_template
        placeholder = _CHECKPOINT_PLACEHOLDER_BY_ARCH.get(entry.architecture, "__CHECKPOINT_PATH__")

        seed = int(time.time() * 1000) % (2**32)
        raw = json.dumps(template)
        raw = raw.replace(f'"{placeholder}"', json.dumps(entry.comfy_path))
        raw = raw.replace('"__PROMPT__"', json.dumps(prompt))
        raw = raw.replace('"__WIDTH__"', str(width))
        raw = raw.replace('"__HEIGHT__"', str(height))
        raw = raw.replace('"__SEED__"', str(seed))
        workflow = json.loads(raw)
        return submit_and_wait(self.base_url, workflow, "save_image", "images", timeout)["content"]

    def upscale(self, image_path: str, timeout: int = 120) -> bytes:
        """Escala x4 una imagen ya existente (RealESRGAN), sin volver a generarla."""
        uploaded_filename = self.upload_image(image_path)
        raw = json.dumps(self.upscale_template)
        raw = raw.replace('"__IMAGE_FILENAME__"', json.dumps(uploaded_filename))
        workflow = json.loads(raw)
        return submit_and_wait(self.base_url, workflow, "save_image", "images", timeout)["content"]

    def inpaint(self, prompt: str, image_path: str, mask_path: str,
                denoise: float = 1.0, timeout: int = 480) -> bytes:
        """Repinta solo la zona marcada en blanco en la mascara, dejando el
        resto de la imagen intacto (SDXL, checkpoint que ya tenemos).

        denoise=1.0 por defecto: comprobado que con un checkpoint base (no
        uno especializado en inpainting) un denoise menor (probado 0.85) deja
        la zona enmascarada como un bloque gris sin terminar de repintar - el
        modelo necesita partir de ruido completo en esa zona para poder
        regenerarla de verdad."""
        uploaded_image = self.upload_image(image_path)
        uploaded_mask = self.upload_image(mask_path)  # mismo endpoint de subida sirve para mascaras

        raw = json.dumps(self.inpaint_template)
        raw = raw.replace('"__CHECKPOINT_PATH__"', json.dumps(self._sdxl_checkpoint_path()))
        raw = raw.replace('"__PROMPT__"', json.dumps(prompt))
        raw = raw.replace('"__IMAGE_FILENAME__"', json.dumps(uploaded_image))
        raw = raw.replace('"__MASK_FILENAME__"', json.dumps(uploaded_mask))
        raw = raw.replace('"__DENOISE__"', str(denoise))
        raw = raw.replace('"__SEED__"', str(int(time.time() * 1000) % (2**32)))
        workflow = json.loads(raw)
        return submit_and_wait(self.base_url, workflow, "save_image", "images", timeout)["content"]

    def upload_image(self, image_path: str) -> str:
        """Sube una imagen de referencia a ComfyUI y devuelve el nombre de archivo
        que hay que usar en el nodo LoadImage."""
        with open(image_path, "rb") as f:
            resp = requests.post(
                f"{self.base_url}/upload/image",
                files={"image": (Path(image_path).name, f)},
                data={"overwrite": "true"},
                timeout=30,
            )
        resp.raise_for_status()
        return resp.json()["name"]

    def upload_image_bytes(self, image_bytes: bytes, filename: str) -> str:
        """Igual que upload_image() pero a partir de bytes ya en memoria, no
        de una ruta en disco - para referencias de cara que pueden venir
        descifradas en memoria (ver ROADMAP.md, punto 0, fase 4), sin tener
        que escribir el original sin cifrar a un archivo temporal."""
        resp = requests.post(
            f"{self.base_url}/upload/image",
            files={"image": (filename, image_bytes)},
            data={"overwrite": "true"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["name"]

    def generate_with_controlnet(self, prompt: str, reference_image_bytes: bytes,
                                  control_type: str = "canny", strength: float = 0.8,
                                  width: int = 1024, height: int = 1024, timeout: int = 480) -> bytes:
        """Genera una imagen siguiendo la composicion/estructura de una imagen
        de referencia (ControlNet, SDXL + controlnet-union-sdxl-1.0-promax),
        no su cara ni su estilo - por ejemplo, la misma pose o el mismo
        encuadre pero con contenido distinto segun el prompt. Solo se admite
        control_type='canny' (deteccion de bordes clasica, sin red neuronal
        de por medio) - otros tipos del modelo union (depth, openpose, etc)
        necesitarian sus propios preprocesadores y quedan fuera de alcance
        por ahora."""
        if control_type not in CONTROL_TYPE_MAP:
            raise ValueError(f"control_type debe ser uno de {list(CONTROL_TYPE_MAP)}, no {control_type!r}")

        control_image_bytes = compute_canny_edges(reference_image_bytes)
        uploaded_filename = self.upload_image_bytes(control_image_bytes, "control.png")
        seed = int(time.time() * 1000) % (2**32)

        raw = json.dumps(self.controlnet_template)
        raw = raw.replace('"__CHECKPOINT_PATH__"', json.dumps(self._sdxl_checkpoint_path()))
        raw = raw.replace('"__PROMPT__"', json.dumps(prompt))
        raw = raw.replace('"__WIDTH__"', str(width))
        raw = raw.replace('"__HEIGHT__"', str(height))
        raw = raw.replace('"__SEED__"', str(seed))
        raw = raw.replace('"__STRENGTH__"', str(strength))
        raw = raw.replace('"__CONTROL_TYPE__"', json.dumps(CONTROL_TYPE_MAP[control_type]))
        raw = raw.replace('"__CONTROL_IMAGE_FILENAME__"', json.dumps(uploaded_filename))
        workflow = json.loads(raw)
        return submit_and_wait(self.base_url, workflow, "save_image", "images", timeout)["content"]

    def generate_with_face(self, prompt: str, reference_image_bytes: bytes,
                            width: int = 1024, height: int = 1024, timeout: int = 480,
                            model_id: str | None = None) -> bytes:
        """Genera una imagen a partir de un prompt preservando la cara de la
        persona (IPAdapter FaceID, SDXL). model_id: checkpoint SDXL concreto
        a usar (p.ej. uno fotorrealista/NSFW), o None para el primero
        instalado."""
        uploaded_filename = self.upload_image_bytes(reference_image_bytes, "reference.png")
        seed = int(time.time() * 1000) % (2**32)

        raw = json.dumps(self.faceid_template)
        raw = raw.replace('"__CHECKPOINT_PATH__"', json.dumps(self._sdxl_checkpoint_path(model_id)))
        raw = raw.replace('"__PROMPT__"', json.dumps(prompt))
        raw = raw.replace('"__WIDTH__"', str(width))
        raw = raw.replace('"__HEIGHT__"', str(height))
        raw = raw.replace('"__SEED__"', str(seed))
        raw = raw.replace('"__IMAGE_FILENAME__"', json.dumps(uploaded_filename))
        workflow = json.loads(raw)
        return submit_and_wait(self.base_url, workflow, "save_image", "images", timeout)["content"]
