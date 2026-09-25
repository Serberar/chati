import json
import time
from pathlib import Path

import requests

import model_registry
from agents.comfyui_client import submit_and_wait
from agents.image_agent import NoModelInstalledError

WORKFLOW_PATH = Path(__file__).parent.parent / "workflows" / "ltxv_video.json"
IMG2VIDEO_WORKFLOW_PATH = Path(__file__).parent.parent / "workflows" / "ltxv_img2video.json"


class VideoAgent:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.template = json.loads(WORKFLOW_PATH.read_text(encoding="utf-8"))
        self.img2video_template = json.loads(IMG2VIDEO_WORKFLOW_PATH.read_text(encoding="utf-8"))

    def _checkpoint_path(self, model_id: str | None = None) -> str:
        """model_id: id de model_registry.py (p.ej. "ltxv:ltxv-2b-0.9.8-...")
        o None para el automatico (el primero instalado)."""
        entry = model_registry.get_video_model(model_id)
        if entry is None:
            if model_id:
                raise NoModelInstalledError(f"El modelo de video '{model_id}' ya no esta instalado.")
            raise NoModelInstalledError(
                "No hay ningun modelo de video instalado. Añade uno en "
                f"{model_registry.VID_DIR} (ver Opciones > Modelos > Video)."
            )
        return entry.comfy_path

    def upload_image(self, image_path: str) -> str:
        with open(image_path, "rb") as f:
            resp = requests.post(
                f"{self.base_url}/upload/image",
                files={"image": (Path(image_path).name, f)},
                data={"overwrite": "true"},
                timeout=30,
            )
        resp.raise_for_status()
        return resp.json()["name"]

    def generate(self, prompt: str, width: int = 512, height: int = 320, length: int = 25,
                 fps: int = 24, steps: int = 8, cfg: float = 1.5, timeout: int = 420,
                 model_id: str | None = None) -> bytes:
        seed = int(time.time() * 1000) % (2**32)
        raw = json.dumps(self.template)
        raw = raw.replace('"__CHECKPOINT_PATH__"', json.dumps(self._checkpoint_path(model_id)))
        raw = raw.replace('"__PROMPT__"', json.dumps(prompt))
        raw = raw.replace('"__WIDTH__"', str(width))
        raw = raw.replace('"__HEIGHT__"', str(height))
        raw = raw.replace('"__LENGTH__"', str(length))
        raw = raw.replace('"__FPS__"', str(fps))
        raw = raw.replace('"__STEPS__"', str(steps))
        raw = raw.replace('"__CFG__"', str(cfg))
        raw = raw.replace('"__SEED__"', str(seed))
        workflow = json.loads(raw)
        return submit_and_wait(self.base_url, workflow, "save_video", ["images", "videos"], timeout)["content"]

    def generate_from_image(self, prompt: str, start_image_path: str, width: int = 768, height: int = 512,
                             length: int = 25, fps: int = 24, steps: int = 8, cfg: float = 1.5,
                             timeout: int = 420, model_id: str | None = None) -> bytes:
        """Anima una imagen ya existente (p.ej. generada con IPAdapter FaceID
        para preservar una cara real) en vez de partir de ruido puro."""
        uploaded_filename = self.upload_image(start_image_path)
        seed = int(time.time() * 1000) % (2**32)

        raw = json.dumps(self.img2video_template)
        raw = raw.replace('"__CHECKPOINT_PATH__"', json.dumps(self._checkpoint_path(model_id)))
        raw = raw.replace('"__PROMPT__"', json.dumps(prompt))
        raw = raw.replace('"__WIDTH__"', str(width))
        raw = raw.replace('"__HEIGHT__"', str(height))
        raw = raw.replace('"__LENGTH__"', str(length))
        raw = raw.replace('"__FPS__"', str(fps))
        raw = raw.replace('"__STEPS__"', str(steps))
        raw = raw.replace('"__CFG__"', str(cfg))
        raw = raw.replace('"__SEED__"', str(seed))
        raw = raw.replace('"__IMAGE_FILENAME__"', json.dumps(uploaded_filename))
        workflow = json.loads(raw)
        return submit_and_wait(self.base_url, workflow, "save_video", ["images", "videos"], timeout)["content"]
