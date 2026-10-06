import io
from unittest.mock import MagicMock

import cv2
import numpy as np
import pytest
from PIL import Image

import photo_edit

FIXTURES_DIR = __import__("pathlib").Path(__file__).parent / "fixtures"


def _textured(h=480, w=640, seed=0):
    """Imagen con textura (la alineacion necesita algo a lo que agarrarse)."""
    rng = np.random.default_rng(seed)
    noise = rng.integers(0, 255, (h // 8, w // 8, 3), dtype=np.uint8)
    return cv2.resize(noise, (w, h), interpolation=cv2.INTER_CUBIC)


@pytest.mark.parametrize("size", [(4032, 3024), (3024, 4032), (1024, 768), (1920, 1080), (1000, 1000)])
def test_kontext_size_keeps_the_aspect_ratio_and_about_one_megapixel(size):
    w, h = photo_edit.kontext_size(*size)
    assert w % 16 == 0 and h % 16 == 0
    assert 0.8 * 1024 * 1024 <= w * h <= 1.05 * 1024 * 1024
    assert abs(w / h - size[0] / size[1]) < 0.01


def test_compose_local_keeps_the_original_outside_what_changed():
    orig = _textured()
    edited = cv2.GaussianBlur(orig, (3, 3), 0)  # redibujado leve, como hace Kontext
    edited = np.roll(edited, 2, axis=1)          # y un par de pixeles descolocado
    edited[200:400, 100:300] = (40, 200, 40)     # lo que de verdad se pidio cambiar
    out = photo_edit.compose_local(orig, edited)
    assert np.array_equal(out[:150, 400:], orig[:150, 400:])
    assert np.abs(out[260:340, 160:240].astype(int) - (40, 200, 40)).max() < 10


def test_compose_local_keeps_the_original_when_kontext_changed_nothing_real():
    orig = _textured(seed=2)
    redrawn = cv2.GaussianBlur(orig, (3, 3), 0)  # mismas cosas, solo redibujadas
    assert np.array_equal(photo_edit.compose_local(orig, redrawn), orig)


def test_compose_local_gives_kontexts_version_when_almost_everything_changed():
    orig = _textured(seed=1)
    edited = 255 - orig  # p.ej. "hazla en negativo": mezclar trozos quedaria peor
    assert np.array_equal(photo_edit.compose_local(orig, edited), edited)


def test_compose_background_puts_back_the_original_person_where_kontext_moved_it():
    orig = photo_edit.load_rgb((FIXTURES_DIR / "sample_face.png").read_bytes())
    alpha = photo_edit.matte(orig)[..., None]
    shift = np.float32([[1, 0, 12], [0, 1, 6]])
    moved = cv2.warpAffine(orig, shift, orig.shape[1::-1], borderMode=cv2.BORDER_REPLICATE)
    moved_alpha = cv2.warpAffine(alpha[..., 0], shift, orig.shape[1::-1])[..., None]
    # Kontext: la persona algo redibujada y desplazada sobre un fondo nuevo
    redrawn = cv2.GaussianBlur(moved, (5, 5), 0).astype(np.float32)
    edited = (redrawn * moved_alpha + np.array([30, 90, 200], np.float32) * (1 - moved_alpha)).astype(np.uint8)
    out = photo_edit.compose_background(orig, edited)
    inside = cv2.erode((moved_alpha[..., 0] > 0.95).astype(np.uint8), np.ones((15, 15), np.uint8)) > 0
    assert inside.sum() > 1000
    # la persona es la original (nitida), no la redibujada (borrosa)
    assert np.abs(out[inside].astype(int) - moved[inside]).mean() < np.abs(out[inside].astype(int) - redrawn[inside]).mean()
    assert np.abs(out[inside].astype(int) - moved[inside]).mean() < 4
    corner = out[:10, :10].reshape(-1, 3).mean(axis=0)
    assert np.abs(corner - (30, 90, 200)).max() < 10


def test_load_rgb_applies_the_exif_rotation_of_phone_photos():
    img = Image.new("RGB", (40, 20), (255, 0, 0))
    exif = img.getexif()
    exif[0x0112] = 6  # "girar 90 grados" - lo que guardan los moviles en vertical
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    assert photo_edit.load_rgb(buf.getvalue()).shape[:2] == (40, 20)


def test_plan_edit_reads_the_models_json():
    ollama = MagicMock()
    ollama.chat.return_value = 'Claro: {"pasos": [{"instruction": "Change the background to a beach.", "mode": "fondo"}]}'
    [plan] = photo_edit.plan_edit(ollama, "qwen3:8b", "ponnos en una playa", ["izquierda", "derecha"])
    assert (plan.instruction, plan.mode) == ("Change the background to a beach.", "fondo")
    assert "izquierda, derecha" in ollama.chat.call_args.args[1][0]["content"]


def test_plan_edit_splits_people_changes_and_background_in_two_steps():
    ollama = MagicMock()
    ollama.chat.return_value = ('{"pasos": [{"instruction": "Make her dress red.", "mode": "local"}, '
                                '{"instruction": "Change the background to mountains.", "mode": "fondo"}]}')
    steps = photo_edit.plan_edit(ollama, "qwen3:8b", "ponla en la montaña con la ropa roja", ["centro"])
    assert [s.mode for s in steps] == ["local", "fondo"]


@pytest.mark.parametrize("answer", ["no se", '{"pasos": [{"mode": "fondo"}]}', '{"instruction": "x", "mode": "rara"}'])
def test_plan_edit_falls_back_to_a_local_edit(answer):
    ollama = MagicMock()
    ollama.chat.return_value = answer
    [plan] = photo_edit.plan_edit(ollama, "qwen3:8b", "la camiseta verde", ["centro"])
    assert plan.mode == "local"
    assert plan.instruction in ("la camiseta verde", "x")


@pytest.mark.live
@pytest.mark.parametrize("request_text,modes", [
    ("quiero que estemos en una playa", ["fondo"]),
    ("edita esta foto para que la camiseta sea verde en lugar de negra", ["local"]),
    ("ponnos en la nieve con abrigos", ["local", "fondo"]),
    ("ponla en la montaña y cambia el color de su ropa a rojo", ["local", "fondo"]),
    ("quita a la gente del fondo", ["local"]),
    ("ponla a ella de pie con un brazo en alto", ["local"]),
])
def test_plan_edit_with_the_real_model(request_text, modes):
    from agents.ollama_client import OllamaClient
    steps = photo_edit.plan_edit(OllamaClient("http://127.0.0.1:11434"), "qwen3:8b", request_text,
                                 ["izquierda", "derecha"])
    assert [s.mode for s in steps] == modes
    assert all(s.instruction.isascii() for s in steps)


def test_plan_edit_does_not_touch_the_person_when_only_the_place_is_asked():
    # "ponme en la playa": qwen3:8b copiaba el ejemplo del bañador (2026-10-05)
    ollama = MagicMock()
    ollama.chat.return_value = ('{"pasos": [{"instruction": "Replace his sweater with swim trunks.", "mode": "local"}, '
                                '{"instruction": "Change the background to a beach.", "mode": "fondo"}]}')
    [plan] = photo_edit.plan_edit(ollama, "qwen3:8b", "ponme en la playa", ["centro"])
    assert plan.mode == "fondo"
    steps = photo_edit.plan_edit(ollama, "qwen3:8b", "ponme en bañador en la playa", ["centro"])
    assert [s.mode for s in steps] == ["local", "fondo"]


def test_restore_faces_puts_back_the_original_face_where_kontext_moved_it():
    face = photo_edit.load_rgb((FIXTURES_DIR / "sample_face.png").read_bytes())
    h, w = face.shape[:2]
    if not photo_edit._faces(face):
        pytest.skip("el detector no ve la cara de la imagen de prueba")
    shift = np.float32([[1, 0, 12], [0, 1, 8]])
    moved = cv2.warpAffine(face, shift, (w, h), borderMode=cv2.BORDER_REPLICATE)
    redrawn = cv2.GaussianBlur(moved, (0, 0), 3)  # la cara "parecida" de Kontext
    out = photo_edit.restore_faces(face, redrawn)
    x, y, fw, fh = (int(v) for v in photo_edit._faces(moved)[0][:4])
    inner = (slice(y + fh // 4, y + 3 * fh // 4), slice(x + fw // 4, x + 3 * fw // 4))
    sharp = cv2.Laplacian(cv2.cvtColor(out, cv2.COLOR_RGB2GRAY), cv2.CV_64F)[inner].var()
    blurred = cv2.Laplacian(cv2.cvtColor(redrawn, cv2.COLOR_RGB2GRAY), cv2.CV_64F)[inner].var()
    assert sharp > 3 * blurred  # el detalle es el de la original, no el redibujado
    assert np.abs(out[inner].astype(int) - moved[inner].astype(int)).mean() < 12


def test_restore_faces_leaves_images_without_faces_alone():
    img = _textured()
    assert photo_edit.restore_faces(img, img[::-1].copy()) is not None
    assert np.array_equal(photo_edit.restore_faces(img, img[::-1].copy()), img[::-1])


@pytest.mark.parametrize("request_text,kept", [
    ("ponme en bañador en la playa", True), ("ponme en paris", True),
    ("ponme gafas de sol", False), ("hazme mas joven", False), ("cambiame el peinado", False)])
def test_the_face_is_only_redrawn_when_asked(request_text, kept):
    assert photo_edit.wants_face_kept(request_text) is kept


@pytest.mark.parametrize("request_text,kept", [
    ("ahora ponme un sombrero de paja", False), ("ponle una gorra roja", False),
    ("ponme en la playa", True), ("quita a la otra persona", True)])
def test_the_original_hair_is_not_pasted_over_a_hat(request_text, kept):
    assert photo_edit.wants_hair_kept(request_text) is kept


def test_restore_faces_without_hair_only_touches_the_face():
    face = photo_edit.load_rgb((FIXTURES_DIR / "sample_face.png").read_bytes())
    faces = photo_edit._faces(face)
    if not faces:
        pytest.skip("el detector no ve la cara de la imagen de prueba")
    hat = face.copy()
    x, y, fw, fh = (int(v) for v in faces[0][:4])
    top = max(0, y - fh // 3)
    hat[top:y + fh // 10, max(0, x - fw // 4):x + fw + fw // 4] = (200, 170, 90)  # el sombrero
    out = photo_edit.restore_faces(face, hat, keep_hair=False)
    assert np.abs(out[top:y, x:x + fw].astype(int) - (200, 170, 90)).mean() < 15


def test_person_and_place_go_in_a_single_kontext_pass():
    # cada pasada son ~2,5 min en la GPU de 8 GB; la cara la recoloca restore_faces
    person = photo_edit.EditPlan("Replace her blouse with a bikini. Keep her face and pose exactly the same. "
                                 "Do not add any other people.", "local", "te pongo un bikini")
    place = photo_edit.EditPlan("Change the background to a beach. Keep the woman exactly the same.", "fondo",
                                "te pongo en una playa")
    [merged] = photo_edit.merge_steps([person, place])
    assert merged.mode == "local"
    assert merged.instruction == ("Replace her blouse with a bikini. Change the background to a beach. "
                                  "Keep her face and pose exactly the same. Do not add any other people.")
    assert merged.summary == "te pongo un bikini; te pongo en una playa"
    assert photo_edit.merge_steps([place]) == [place]


def test_huge_photos_are_reduced_on_load():
    big = Image.new("RGB", (9000, 6000), (90, 120, 150))  # 54 MP, como una de movil de 50+ MP
    buf = io.BytesIO()
    big.save(buf, format="JPEG", quality=70)
    rgb = photo_edit.load_rgb(buf.getvalue())
    assert rgb.shape[0] * rgb.shape[1] <= photo_edit.MAX_PIXELS
    assert abs(rgb.shape[1] / rgb.shape[0] - 1.5) < 0.01
