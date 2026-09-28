"""Detecta si una imagen contiene una cara humana - usado para decidir
automaticamente que tecnica usar cuando se adjunta una foto en modo Imagen
(ver ROADMAP.md): con cara, preservar esa cara (FaceID); sin cara (una
mariposa, un paisaje, un objeto...), seguir la composicion de la foto en
vez de intentar preservar una cara que no existe (bug real encontrado en
vivo: pedir "una mariposa similar con los colores invertidos" generaba una
cara alucinada, porque FaceID se aplicaba siempre sin comprobar nada).

Usa YuNet (cv2.FaceDetectorYN, ya incluido en opencv-python-headless desde
que dejo de traer el CascadeClassifier/Haar cascade clasico) - rapido,
CPU, sin necesitar la GPU que ya usan ComfyUI/los modelos de imagen."""

import io
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

_MODEL_PATH = Path(__file__).parent / "assets" / "face_detection_yunet_2023mar.onnx"

_detector = None


def _get_detector():
    global _detector
    if _detector is None:
        _detector = cv2.FaceDetectorYN_create(str(_MODEL_PATH), "", (320, 320))
    return _detector


def has_face(image_bytes: bytes, confidence_threshold: float = 0.7) -> bool:
    """True si se detecta al menos una cara con confianza razonable. Nunca
    lanza por una imagen rara/corrupta - en ese caso se asume que no hay
    cara (mejor caer al camino de ControlNet, que funciona con cualquier
    imagen, que fallar aqui)."""
    try:
        img = np.array(Image.open(io.BytesIO(image_bytes)).convert("RGB"))[:, :, ::-1]  # RGB -> BGR
    except Exception:
        return False

    height, width = img.shape[:2]
    if height == 0 or width == 0:
        return False

    detector = _get_detector()
    detector.setInputSize((width, height))
    _, faces = detector.detect(img)
    if faces is None:
        return False
    return any(face[14] >= confidence_threshold for face in faces)  # face[14] = score de confianza
