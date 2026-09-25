"""Pruebas deterministas de la parte de image_agent.py que no depende de
ComfyUI (deteccion de bordes de Canny, pura CPU/OpenCV). La generacion en si
(generate_with_controlnet) se prueba contra el servicio real, ver
tests/test_integration.py::test_image_with_controlnet_composition (marcado
slow)."""

import io

from PIL import Image, ImageDraw

from agents.image_agent import compute_canny_edges


def _sample_image_bytes() -> bytes:
    """Un cuadrado blanco sobre fondo negro: bordes nitidos y predecibles,
    no hace falta una foto real para probar que Canny encuentra un contorno."""
    img = Image.new("RGB", (200, 200), color="black")
    draw = ImageDraw.Draw(img)
    draw.rectangle([50, 50, 150, 150], fill="white")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def test_compute_canny_edges_returns_valid_png():
    result = compute_canny_edges(_sample_image_bytes())

    edges_img = Image.open(io.BytesIO(result))
    assert edges_img.format == "PNG"
    assert edges_img.size == (200, 200)


def test_compute_canny_edges_detects_the_square_outline():
    result = compute_canny_edges(_sample_image_bytes())
    edges_img = Image.open(io.BytesIO(result)).convert("L")

    # el centro (100,100) es interior solido del cuadrado, sin borde; pero el
    # contorno completo (200x200 - interior 100x100) SI debe tener pixeles
    # blancos (bordes detectados) en alguna parte
    assert edges_img.getpixel((100, 100)) == 0  # interior, sin borde
    assert edges_img.getextrema()[1] > 0  # algun pixel blanco de borde detectado


def test_compute_canny_edges_blank_image_has_no_edges():
    blank = Image.new("RGB", (100, 100), color="gray")
    out = io.BytesIO()
    blank.save(out, format="PNG")

    result = compute_canny_edges(out.getvalue())
    edges_img = Image.open(io.BytesIO(result)).convert("L")

    assert edges_img.getextrema() == (0, 0)  # nada de bordes en una imagen lisa
