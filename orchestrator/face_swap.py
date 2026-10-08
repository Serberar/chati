"""Poner los rasgos de una persona (de su foto original) sobre una cara que la
IA ha dibujado con otro angulo, otra luz u otra expresion (Sergio, 2026-10-08:
"si el angulo es distinto habra que usar la otra tecnologia").

Al cambiar la postura o la escena, pegar la cara original (recta, con su luz)
quedaba como un recorte. Aqui la cara la dibuja Kontext como toca en la
escena y despues:
1. ArcFace (w600k_r50, de buffalo_l) saca la "huella" de la cara original;
2. inswapper_128 dibuja esa identidad sobre la cara nueva, con su angulo, su
   luz y su expresion;
3. GFPGAN 1.4 la restaura (inswapper trabaja a 128 px y sale blanda);
4. se devuelve a la foto con borde suave.
Todo en la CPU (unos segundos). Modelos de uso no comercial: es para uso
personal. Si faltan, swap_faces() devuelve la imagen tal cual.
"""
import logging

import cv2
import numpy as np
import onnxruntime as ort

from paths import DATA_ROOT, MODELS_DIR

SWAP_DIR = MODELS_DIR / "img" / "faceswap"
SWAPPER = SWAP_DIR / "inswapper_128.onnx"
EMAP = SWAP_DIR / "inswapper_emap.npy"      # matriz del propio inswapper (extraida con onnx)
RESTORER = SWAP_DIR / "gfpgan_1.4.onnx"
ARCFACE = DATA_ROOT / "ComfyUI" / "models" / "insightface" / "models" / "buffalo_l" / "w600k_r50.onnx"

# donde caen los 5 puntos (ojos, nariz, comisuras) en una cara alineada de
# 112 px (ArcFace) y de 512 px (FFHQ, GFPGAN)
ARCFACE_POINTS = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                           [41.5493, 92.3655], [70.7299, 92.2041]], np.float32)
FFHQ_POINTS = np.array([[192.98138, 239.94708], [318.90277, 240.1936], [256.63416, 314.01935],
                        [201.26117, 371.41043], [313.08905, 371.15118]], np.float32)

SECOND_PASS_BELOW = 0.6  # parecido ArcFace por debajo del cual se repite el clonado

log = logging.getLogger("chati")
_sessions: dict = {}


def available() -> bool:
    return all(p.exists() for p in (SWAPPER, EMAP, RESTORER, ARCFACE))


def _session(path):
    key = str(path)
    if key not in _sessions:
        _sessions[key] = ort.InferenceSession(key, providers=["CPUExecutionProvider"])
    return _sessions[key]


def _points(face: np.ndarray) -> np.ndarray:
    """Los 5 puntos de YuNet en el orden de las plantillas (ojo de la
    izquierda de la imagen primero)."""
    return face[4:14].reshape(5, 2).astype(np.float32)


def _align(rgb: np.ndarray, points: np.ndarray, template: np.ndarray, size: int) -> tuple[np.ndarray, np.ndarray]:
    m, _ = cv2.estimateAffinePartial2D(points, template, method=cv2.LMEDS)
    return cv2.warpAffine(rgb, m, (size, size), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE), m


def identity(rgb: np.ndarray, face: np.ndarray) -> np.ndarray:
    """La huella de la cara (ArcFace), normalizada."""
    crop, _ = _align(rgb, _points(face), ARCFACE_POINTS, 112)
    x = ((crop.astype(np.float32) - 127.5) / 127.5).transpose(2, 0, 1)[None]
    s = _session(ARCFACE)
    emb = s.run(None, {s.get_inputs()[0].name: x})[0][0]
    return emb / (np.linalg.norm(emb) + 1e-9)


def _paste(base: np.ndarray, face_img: np.ndarray, m: np.ndarray, size: int, feather: float) -> np.ndarray:
    """Devuelve `face_img` (alineada con `m`) a su sitio en `base`, con borde suave."""
    h, w = base.shape[:2]
    inv = cv2.invertAffineTransform(m)
    back = cv2.warpAffine(face_img, inv, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    mask = np.zeros((size, size), np.float32)
    margin = int(size * 0.08)
    mask[margin:size - margin, margin:size - margin] = 1
    mask = cv2.GaussianBlur(mask, (0, 0), size * feather)
    mask = cv2.warpAffine(mask, inv, (w, h), flags=cv2.INTER_LINEAR)[..., None]
    return (back.astype(np.float32) * mask + base.astype(np.float32) * (1 - mask)).clip(0, 255).astype(np.uint8)


def _swap_one(rgb: np.ndarray, target_face: np.ndarray, source_id: np.ndarray) -> np.ndarray:
    # 2-3: la identidad sobre la cara nueva (inswapper, 128 px)
    template = ARCFACE_POINTS * (128 / 112)
    crop, m = _align(rgb, _points(target_face), template, 128)
    latent = source_id[None] @ np.load(EMAP)
    latent /= np.linalg.norm(latent) + 1e-9
    s = _session(SWAPPER)
    out = s.run(None, {"target": (crop.astype(np.float32) / 255.0).transpose(2, 0, 1)[None],
                       "source": latent.astype(np.float32)})[0][0]
    swapped = (out.transpose(1, 2, 0) * 255).clip(0, 255).astype(np.uint8)
    result = _paste(rgb, swapped, m, 128, 0.06)
    # 4: restaurar (GFPGAN, 512 px) para que no quede blanda
    crop512, m512 = _align(result, _points(target_face), FFHQ_POINTS, 512)
    s = _session(RESTORER)
    x = ((crop512.astype(np.float32) / 255.0 - 0.5) / 0.5).transpose(2, 0, 1)[None]
    restored = s.run(None, {s.get_inputs()[0].name: x})[0][0]
    restored = ((restored.transpose(1, 2, 0) * 0.5 + 0.5) * 255).clip(0, 255).astype(np.uint8)
    # mitad restaurada y mitad sin restaurar, y el grano de la foto: GFPGAN sola
    # alisa la piel como un filtro de belleza (a Mon le quitaba las marcas de
    # la piel y a Sergio le difuminaba la barba, 2026-10-08)
    restored = cv2.addWeighted(restored, 0.5, crop512, 0.5, 0)
    noise = np.random.default_rng(0).normal(0, 1, restored.shape[:2]).astype(np.float32)
    grain = _grain(rgb) * cv2.GaussianBlur(noise, (0, 0), 0.6) / max(float(noise.std()), 1e-6)
    restored = (restored.astype(np.float32) + grain[..., None]).clip(0, 255).astype(np.uint8)
    return _paste(result, restored, m512, 512, 0.05)


def _grain(rgb: np.ndarray) -> float:
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    res = g - cv2.GaussianBlur(g, (0, 0), 1.2)
    return float(min(4.0, 1.4826 * np.median(np.abs(res))))


def swap_faces(original: np.ndarray, result: np.ndarray, faces_original: list, faces_result: list) -> np.ndarray:
    """Las caras de `result` con los rasgos de las de `original`, emparejadas
    de izquierda a derecha (como restore_faces). Las que sobran se dejan."""
    if not available() or not faces_original or not faces_result:
        return result
    order_o = sorted(faces_original, key=lambda f: f[0] + f[2] / 2)
    order_r = sorted(faces_result, key=lambda f: f[0] + f[2] / 2)
    out = result
    for fo, fr in zip(order_o, order_r):
        source_id = identity(original, fo)
        out = _swap_one(out, fr, source_id)
        # comprobar el parecido: si se queda corto (cara pequeña, de perfil),
        # una segunda pasada lo acerca
        score = float(source_id @ identity(out, fr))
        if score < SECOND_PASS_BELOW:
            out = _swap_one(out, fr, source_id)
            score = float(source_id @ identity(out, fr))
        log.info("Edicion: parecido con la original %.2f", score)
    return out


def similarity(a_rgb: np.ndarray, a_face: np.ndarray, b_rgb: np.ndarray, b_face: np.ndarray) -> float:
    """Parecido entre dos caras (coseno de ArcFace: >0,5 suele ser la misma persona)."""
    return float(identity(a_rgb, a_face) @ identity(b_rgb, b_face))
