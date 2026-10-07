"""Pruebas deterministas de face_detect.py contra imagenes reales (nada
mockeado - el modelo YuNet es pequeño y rapido, correr contra el de verdad
es mas fiable que simular su salida)."""

import io

from PIL import Image

import face_detect

FIXTURES_DIR = __import__("pathlib").Path(__file__).parent / "fixtures"


def _solid_color_png(color=(200, 100, 50), size=(300, 300)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


def test_has_face_true_for_a_real_face_photo():
    img_bytes = (__import__("pathlib").Path(__file__).parent / "fixtures" / "sample_face.png").read_bytes()
    assert face_detect.has_face(img_bytes) is True


def test_has_face_false_for_a_solid_color_image():
    """El caso real que motivo esto: una mariposa (sin cara humana) no
    deberia detectarse como una cara."""
    assert face_detect.has_face(_solid_color_png()) is False


def test_has_face_false_for_corrupt_image_data():
    """Nunca debe lanzar - una imagen rara/corrupta cae al lado seguro
    (ControlNet, que funciona con cualquier imagen) en vez de romper la
    peticion."""
    assert face_detect.has_face(b"esto no es una imagen de verdad") is False


def test_has_face_false_for_empty_bytes():
    assert face_detect.has_face(b"") is False


def test_has_face_respects_a_stricter_confidence_threshold():
    img_bytes = (__import__("pathlib").Path(__file__).parent / "fixtures" / "sample_face.png").read_bytes()
    # el umbral por defecto (0.7) detecta la cara real; uno imposible (1.1,
    # por encima del maximo teorico de 1.0) nunca deberia superarse
    assert face_detect.has_face(img_bytes, confidence_threshold=1.1) is False


def test_count_faces_counts_one_in_a_single_face_photo():
    assert face_detect.count_faces((FIXTURES_DIR / "sample_face.png").read_bytes()) == 1


def test_count_faces_zero_for_corrupt_image_data():
    assert face_detect.count_faces(b"no es una imagen") == 0


def test_face_positions_says_where_each_face_is():
    assert face_detect.face_positions((FIXTURES_DIR / "sample_face.png").read_bytes()) in (["centro"], ["izquierda"], ["derecha"])


def _sample_face_scaled(factor: float, exif_orientation: int | None = None) -> bytes:
    img = Image.open(FIXTURES_DIR / "sample_face.png").convert("RGB")
    img = img.resize((int(img.width * factor), int(img.height * factor)))
    exif = None
    if exif_orientation is not None:
        # guardada tumbada, como la saca el movil, con la marca de como girarla
        img = img.rotate(90, expand=True)
        exif = img.getexif()
        exif[274] = exif_orientation
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92, **({"exif": exif.tobytes()} if exif is not None else {}))
    return buf.getvalue()


def test_a_huge_face_from_a_phone_photo_is_found():
    """2026-10-07: en una foto de 12 MP la cara (900 px) no se encontraba y el
    editor no volvia a poner la original."""
    img_bytes = _sample_face_scaled(4032 / max(Image.open(FIXTURES_DIR / "sample_face.png").size))
    assert face_detect.has_face(img_bytes) is True


def test_a_phone_photo_stored_sideways_is_turned_before_detecting():
    assert face_detect.has_face(_sample_face_scaled(1.0, exif_orientation=6)) is True


def test_detect_returns_coordinates_of_the_full_size_image():
    import numpy as np
    small = np.array(Image.open(FIXTURES_DIR / "sample_face.png").convert("RGB"))
    big = np.array(Image.fromarray(small).resize((small.shape[1] * 4, small.shape[0] * 4)))
    (a,), (b,) = face_detect.detect(small), face_detect.detect(big)
    assert abs(b[0] / 4 - a[0]) < 0.05 * a[2] and abs(b[2] / 4 - a[2]) < 0.1 * a[2]
