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
import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

try:  # fotos HEIC del iPhone, como en photo_edit
    import pillow_heif
    pillow_heif.register_heif_opener()
except ImportError:
    pass

_MODEL_PATH = Path(__file__).parent / "assets" / "face_detection_yunet_2023mar.onnx"

# Uno por hilo: cada uso le cambia el tamaño de entrada (setInputSize) antes de
# detectar, y con uno compartido dos peticiones a la vez se lo pisaban (2026-10-06)
_local = threading.local()


def _get_detector():
    if getattr(_local, "detector", None) is None:
        _local.detector = cv2.FaceDetectorYN_create(str(_MODEL_PATH), "", (320, 320))
    return _local.detector


# YuNet busca caras de hasta unos cientos de pixeles: en una foto de movil de
# 12 MP una cara de 900 px no la encontraba (ninguna cara, y el editor no
# volvia a poner la original, 2026-10-07). Se detecta con la foto reducida.
DETECT_SIDE = 1280


def detect(rgb: np.ndarray, confidence_threshold: float = 0.7) -> list[np.ndarray]:
    """Caras de YuNet (x, y, ancho, alto, 5 puntos, confianza) en coordenadas
    de `rgb`. Nunca lanza: con una imagen rara, ninguna cara."""
    height, width = rgb.shape[:2]
    if height == 0 or width == 0:
        return []
    side = max(height, width)
    # de mas detalle a menos: los puntos de la cara salen mas precisos con mas
    # pixeles; las caras grandes (un selfie de cerca) solo a 640 o 320, y las
    # pequeñas (un grupo lejos) solo a tamaño completo
    scales = [min(1.0, DETECT_SIDE / side)] + [s / side for s in (640, 320) if s < min(side, DETECT_SIDE)]
    if side > DETECT_SIDE:
        scales.append(1.0)
    faces: list[np.ndarray] = []
    for scale in scales:
        for face in _detect_at(rgb, scale, confidence_threshold):
            if not any(_overlap(face, other) for other in faces):
                faces.append(face)
    return faces


def _detect_at(rgb: np.ndarray, scale: float, confidence_threshold: float) -> list[np.ndarray]:
    height, width = rgb.shape[:2]
    small = rgb if scale == 1.0 else cv2.resize(rgb, (max(1, round(width * scale)), max(1, round(height * scale))),
                                                interpolation=cv2.INTER_AREA)
    try:
        detector = _get_detector()
        detector.setInputSize((small.shape[1], small.shape[0]))
        _, faces = detector.detect(np.ascontiguousarray(small[:, :, ::-1]))  # RGB -> BGR
    except cv2.error:
        return []
    out = []
    for face in [] if faces is None else faces:
        if face[14] < confidence_threshold:  # face[14] = score de confianza
            continue
        face = face.copy()
        face[:14] /= scale
        out.append(face)
    return out


def _overlap(a: np.ndarray, b: np.ndarray) -> bool:
    ax, ay, aw, ah = a[:4]
    bx, by, bw, bh = b[:4]
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def load_rgb(image_bytes: bytes) -> np.ndarray | None:
    """Girada segun el EXIF: las fotos del movil vienen tumbadas y la cara de
    lado no se detectaba."""
    try:
        return np.array(ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB"))
    except Exception:
        return None


def face_positions(image_bytes: bytes, confidence_threshold: float = 0.7) -> list[str]:
    """Donde esta cada cara, de izquierda a derecha ("izquierda", "centro",
    "derecha") - para que el editor de fotos sepa a quien se refiere "ella"
    o "el de la derecha". Nunca lanza por una imagen rara/corrupta - en ese
    caso ninguna cara (mejor caer al camino de ControlNet, que funciona con
    cualquier imagen, que fallar aqui)."""
    img = load_rgb(image_bytes)
    if img is None:
        return []
    width = img.shape[1]
    centers = sorted((face[0] + face[2] / 2) / width for face in detect(img, confidence_threshold))
    return ["izquierda" if c < 0.4 else "derecha" if c > 0.6 else "centro" for c in centers]


def count_faces(image_bytes: bytes, confidence_threshold: float = 0.7) -> int:
    return len(face_positions(image_bytes, confidence_threshold))


def has_face(image_bytes: bytes, confidence_threshold: float = 0.7) -> bool:
    return count_faces(image_bytes, confidence_threshold) > 0
