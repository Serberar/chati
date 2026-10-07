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


def test_the_vision_verdict_is_read_from_its_answer():
    ollama = MagicMock()
    jpg = photo_edit.to_jpeg(_textured(64, 64))
    ollama.chat.return_value = "yes"
    assert photo_edit.verify_edit(ollama, "vision", jpg, "Put a hat on him.").ok
    ollama.chat.return_value = "No | She is holding two phones, not one. | Sostiene dos moviles."
    verdict = photo_edit.verify_edit(ollama, "vision", jpg, "Give her a red dress.")
    assert (verdict.ok, verdict.problem_en, verdict.problem_es) == (False, "She is holding two phones, not one",
                                                                     "Sostiene dos moviles")
    ollama.chat.return_value = "I think it looks nice"  # ilegible: se da por buena, no estropea la edicion
    assert photo_edit.verify_edit(ollama, "vision", jpg, "x") is None
    assert photo_edit.verify_edit(ollama, None, jpg, "x") is None
    assert "two phones" in photo_edit.insist("Give her a red dress.", "two phones")
    steps = [photo_edit.EditPlan("Give her a red dress. Keep her face exactly the same.", "local")]
    assert photo_edit.change_requested(steps) == "Give her a red dress."


@pytest.mark.parametrize("message,intent", [
    # Sergio, 2026-10-05/06: hablar normal con una foto en marcha
    ("ahora haz que este sentado", "editar"), ("que sea al atardecer", "editar"),
    ("como quedaria de noche?", "editar"), ("¿puedes quitarle las gafas?", "editar"),
    ("el pelo mas corto", "editar"), ("mas grande", "editar"), ("el coctel que sea una cerveza", "editar"),
    ("deshaz eso", "deshacer"), ("vuelve a como estaba", "deshacer"), ("vuelve a la original", "original"),
    ("otra vez", "repetir"), ("no me gusta, otra", "repetir"),
    ("hazme una imagen de un perro en la luna", "nueva"),
    ("¿qué lleva puesto?", "preguntar"), ("¿de qué color es el bañador?", "preguntar"),
    ("que tal?", "otra"), ("gracias!", "otra"), ("perfecto, gracias", "otra"),
    ("agente: arregla el bug del login", "otra"),
])
def test_what_the_user_wants_with_a_photo_in_the_conversation(message, intent):
    ollama = MagicMock()
    ollama.chat.return_value = "otra"  # el modelo solo decide lo que las reglas no saben
    assert photo_edit.photo_intent(ollama, "m", message) == intent


def test_the_planner_sees_what_was_already_done_to_the_photo():
    ollama = MagicMock()
    ollama.chat.return_value = '{"pasos": [{"instruction": "Make the hat bigger.", "mode": "local"}]}'
    photo_edit.plan_edit(ollama, "m", "mas grande", ["centro"], "a man", ["ponme en la playa", "con un sombrero"])
    prompt = ollama.chat.call_args.args[1][0]["content"]
    assert "- ponme en la playa\n- con un sombrero" in prompt and '"mas grande"' in prompt
    photo_edit.plan_edit(ollama, "m", "ponme en la playa", ["centro"], "a man")
    assert "Esta foto ya es el resultado" not in ollama.chat.call_args.args[1][0]["content"]


def test_pose_changes_use_the_whole_kontext_result():
    # 2026-10-06: al recomponer quedaba un fantasma del brazo levantado de antes
    assert photo_edit.is_pose_change("Make him kneel, zoomed out. Keep his face and pose.")
    assert photo_edit.is_pose_change("Make the man and the woman hold hands. Keep their faces.")
    assert not photo_edit.is_pose_change("Put a hat on him. Keep his face, pose and framing exactly the same.")
    orig, kontext = _textured(seed=1), _textured(seed=2)
    assert np.array_equal(photo_edit.finish(orig, kontext, "entera"), kontext)


@pytest.mark.parametrize("request_text,wrong,right", [
    # 2026-10-07: qwen3:8b traducia lo contrario y Kontext lo hacia tal cual
    ("ponme un vestido rojo de tirantes", "a red strapless dress", "spaghetti-strap"),
    ("ponme una chaqueta vaquera", "a leather jacket", "denim jacket"),
])
def test_known_wrong_clothing_translations_are_fixed(request_text, wrong, right):
    ollama = MagicMock()
    ollama.chat.return_value = (f'{{"pasos": [{{"instruction": "Replace her sweater with {wrong}. Keep her face. '
                                f'Do not add any other people.", "mode": "local", "resumen": "te he puesto"}}]}}')
    (plan,) = photo_edit.plan_edit(ollama, "m", request_text, ["centro"], "a woman")
    assert right in plan.instruction
    prompt = ollama.chat.call_args.args[1][0]["content"]
    assert "Traducciones OBLIGATORIAS" in prompt and right in prompt


def test_the_glossary_does_not_fire_on_other_meanings():
    import clothing_terms
    assert clothing_terms.find("que lo haga a medias") == []
    assert clothing_terms.find("disfrázame de vaquera") == []
    assert [en for _, en, _ in clothing_terms.find("un vestido sin tirantes")] == ["strapless"]


def test_clothing_changes_keep_the_body_shape():
    ollama = MagicMock()
    ollama.chat.return_value = ('{"pasos": [{"instruction": "Replace the woman\'s sweater with a white shirt. Keep her '
                                'face. Do not add any other people.", "mode": "local"}]}')
    (plan,) = photo_edit.plan_edit(ollama, "m", "con una camisa blanca", ["centro"], "a woman")
    assert "Keep her body shape and proportions exactly the same. Do not add" in plan.instruction
    # ni en un cambio de fondo ni en uno de postura
    assert "body shape" not in photo_edit._keep_body("Change the background to a beach. Keep his clothes.")
    assert "body shape" not in photo_edit._keep_body("Make him kneel in his shirt, zoomed out.")


def test_the_summary_talks_to_the_user_about_their_own_photo():
    ollama = MagicMock()
    ollama.chat.return_value = ('{"pasos": [{"instruction": "Replace her sweater with a white shirt.", '
                                '"mode": "local", "resumen": "le he puesto una camisa blanca"}]}')
    (plan,) = photo_edit.plan_edit(ollama, "m", "ahora con una camisa blanca", ["centro"], "a woman",
                                   ["ponme un vestido rojo"])
    assert plan.summary == "te he puesto una camisa blanca"
    (plan,) = photo_edit.plan_edit(ollama, "m", "ahora con una camisa blanca", ["centro"], "a woman",
                                   ["ponla en la playa"])
    assert plan.summary == "le he puesto una camisa blanca"


def test_compose_local_keeps_the_original_after_a_big_clothing_change():
    """2026-10-07: jersey negro -> camisa blanca en media foto hundia la
    alineacion (cc 0,30) y se tiraba la original entera, fondo incluido."""
    orig = _textured(seed=3)
    orig[150:, 150:500] = (15, 15, 15)  # jersey negro
    edited = cv2.GaussianBlur(orig, (3, 3), 0)
    edited[150:, 150:500] = (245, 245, 245)  # camisa blanca
    out = photo_edit.compose_local(orig, edited)
    assert np.abs(out[:100, :100].astype(int) - orig[:100, :100]).max() < 3  # el fondo es el original
    assert out[300:400, 250:400].mean() > 230


def test_kontext_gets_the_grain_of_the_photo():
    rng = np.random.default_rng(0)
    smooth = cv2.GaussianBlur(_textured(seed=4), (0, 0), 2)
    grainy = (smooth.astype(np.float32) + rng.normal(0, 4, smooth.shape[:2])[..., None]).clip(0, 255).astype(np.uint8)
    target = photo_edit.grain_sigma(grainy)
    assert photo_edit.grain_sigma(smooth) < 0.5 * target
    assert abs(photo_edit.grain_sigma(photo_edit.add_grain(smooth, target)) - target) < 0.25 * target


def test_esrgan_only_for_big_upscales():
    small = np.zeros((1088, 816, 3), np.uint8)
    assert not photo_edit.needs_upscale(np.zeros((1536, 1152, 3), np.uint8), small)  # foto de Sergio, 1,4x
    assert photo_edit.needs_upscale(np.zeros((4032, 3024, 3), np.uint8), small)


def test_compose_local_keeps_the_original_when_kontext_also_zooms_in():
    """2026-10-07: Kontext amplio la foto un 10% ademas de cambiar media foto
    (camisa), y se usaba su version entera con el fondo redibujado."""
    orig = _textured(seed=5)
    orig[200:, 200:450] = (15, 15, 15)
    h, w = orig.shape[:2]
    zoom = cv2.getRotationMatrix2D((w / 2, h / 2), 0, 1.1)
    edited = cv2.warpAffine(orig, zoom, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    big = cv2.transform(np.float32([[[200, 200], [450, 480]]]), zoom)[0].astype(int)
    edited[big[0, 1]:, big[0, 0]:big[1, 0]] = (245, 245, 245)
    out = photo_edit.compose_local(orig, edited)
    # el fondo es el de la original (puede venir recortado: la camisa llega al borde)
    patch = out[60:140, 60:140]
    best = cv2.matchTemplate(orig, patch, cv2.TM_SQDIFF)
    y, x = np.unravel_index(best.argmin(), best.shape)
    assert np.abs(orig[y:y + 80, x:x + 80].astype(int) - patch).mean() < 3


def test_keeping_the_pose_is_not_a_pose_change():
    # 2026-10-07: con ", keeping ... pose" se usaba la de Kontext entera
    assert not photo_edit.is_pose_change("Replace his grey wool sweater with a red plaid button-up shirt, keeping "
                                         "his face, facial features, expression, beard, hair, pose and framing "
                                         "exactly the same. Do not add any other people.")
    assert photo_edit.is_pose_change("Make him kneel on the ground, keeping his face and clothes.")


def test_no_old_clothes_left_in_the_border_kontext_did_not_generate():
    """2026-10-07: Kontext amplio la foto; en la franja del borde que no genero
    seguia la manga del jersey gris junto a la camisa nueva."""
    orig = _textured(seed=6)
    orig[150:, :300] = (128, 128, 128)  # jersey gris hasta el borde izquierdo
    h, w = orig.shape[:2]
    zoom = cv2.getRotationMatrix2D((w / 2, h / 2), 0, 1.08)
    edited = cv2.warpAffine(orig, zoom, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    corner = cv2.transform(np.float32([[[300, 150]]]), zoom)[0, 0].astype(int)
    edited[corner[1]:, :corner[0]] = (200, 30, 30)  # camisa roja
    out = photo_edit.compose_local(orig, edited)
    assert out.shape[0] < h and abs(out.shape[1] / out.shape[0] - w / h) < 0.01  # recortada, misma proporcion
    left = out[out.shape[0] // 2:, :8].reshape(-1, 3).astype(int)
    assert np.abs(left - (128, 128, 128)).max(axis=1).min() > 40  # ni rastro del gris


def test_new_skin_from_kontext_gets_the_real_skin_tone(monkeypatch):
    """2026-10-07: Kontext hacia el cuello mas palido que la cara original y al
    volver a ponerla se notaba el corte bajo la barbilla."""
    monkeypatch.setattr(photo_edit, "matte", lambda rgb: np.ones(rgb.shape[:2], np.float32))
    pale, warm = (225, 190, 175), (215, 160, 125)
    result = np.full((400, 300, 3), pale, np.uint8)       # cara y cuello de Kontext
    result[:, :40] = (30, 60, 200)                        # algo que no es piel (fondo azul)
    lit = result.copy()
    lit[60:220, 90:210] = warm                            # la cara original, mas calida
    face = np.array([90, 60, 120, 160] + [0] * 11, np.float32)
    out = photo_edit._match_skin(result.astype(np.float32), result, lit, np.zeros((400, 300), np.float32), face)
    neck = out[300:380, 120:180].reshape(-1, 3).mean(axis=0)
    assert np.abs(neck - warm).max() < np.abs(np.array(pale) - warm).max() / 2  # el cuello va hacia el tono real
    assert np.abs(out[300:, :30] - result[300:, :30]).max() < 2                 # lo que no es piel no se toca


def test_the_seam_band_is_only_outside_the_face():
    """2026-10-07: con la franja a los dos lados del borde la IA redibujaba la
    mandibula y cambiaba la forma de la cara. Solo por fuera."""
    head = np.zeros((600, 500), np.float32)
    cv2.ellipse(head, (250, 260), (130, 190), 0, 0, 360, 1, -1)
    face = np.array([150, 100, 200, 260] + [0] * 11, np.float32)  # x, y, ancho, alto
    band = photo_edit._seam_band(head, face, 200)
    assert band[head > 0.5].sum() == 0                 # nada de la cara
    assert band[:230].sum() == 0                       # nada por encima de los ojos
    assert band[455:470, 230:270].sum() > 0            # si el cuello justo bajo la barbilla


def test_only_the_seam_band_is_pasted_back():
    img = _textured(seed=7)
    band = np.zeros(img.shape[:2], np.float32)
    band[300:330, 200:400] = 1
    face = np.array([200, 100, 200, 220] + [0] * 11, np.float32)
    protected = np.zeros(img.shape[:2], np.float32)
    protected[250:300, 200:400] = 1                    # la cara pegada, justo encima
    seams = [(band, face, protected)]
    box, crop_png, mask_png = photo_edit.seam_crop(img, seams)
    repainted = photo_edit.to_png(np.full_like(photo_edit.load_rgb(crop_png), 128))
    out = photo_edit.paste_seam(img, repainted, box, seams)
    assert np.array_equal(out[:200], img[:200])        # lejos de la franja, identica
    assert np.array_equal(out[250:300, 200:400], img[250:300, 200:400])  # la cara, ni un pixel
    assert np.abs(out[310:320, 250:350].astype(int) - img[310:320, 250:350]).mean() > 10
    assert photo_edit.seam_crop(img, []) is None


def test_clothes_only_steps_are_told_apart():
    plan = photo_edit.EditPlan
    assert photo_edit.is_clothes_only(plan("Replace her black sweater with a red spaghetti-strap dress. Keep her "
                                           "face, pose and framing.", "local"), "ponme un vestido rojo de tirantes")
    assert not photo_edit.is_clothes_only(plan("Change the background to a beach. Keep his clothes.", "fondo"),
                                          "ponme en la playa")
    assert not photo_edit.is_clothes_only(plan("Put a red wool hat on him. Keep his shirt.", "local"), "ponle un gorro")
    assert not photo_edit.is_clothes_only(plan("Make him kneel in his shirt, zoomed out.", "local"), "de rodillas")


def test_the_clothes_mask_leaves_the_head_out():
    """2026-10-07: con la ropa, Kontext solo genera el cuerpo; la cabeza de la
    foto no se toca y el cuello sale siguiendola."""
    rgb = np.array(Image.open(FIXTURES_DIR / "sample_face.png").convert("RGB"))
    (face,) = photo_edit._faces(rgb)
    x, y, fw, fh = (int(v) for v in face[:4])
    mask = photo_edit.clothes_mask(rgb)
    assert mask is not None
    # la cara en si (de los ojos a la boca), fuera; las esquinas del recuadro
    # ya son cuello u hombros y pueden cambiar
    assert mask[int(y + fh * 0.15):int(y + fh * 0.85), int(x + fw * 0.25):int(x + fw * 0.75)].max() == 0
    assert mask[y + fh + 5:].max() > 0                 # el cuerpo, dentro


def test_the_garment_asked_decides_what_changes():
    """2026-10-07: con el modelo de ropa se cambia la prenda pedida y nada mas."""
    upper, lower, whole = photo_edit._UPPER_PARTS, photo_edit._LOWER_PARTS, photo_edit._WHOLE_PARTS
    assert photo_edit._parts_to_change("Replace his white t-shirt with a Hawaiian shirt. Keep his jeans.") == upper
    assert photo_edit._parts_to_change("Replace his jeans with black shorts. Keep his shirt.") == lower
    assert photo_edit._parts_to_change("Replace her sweater with a red dress.") == whole
    assert photo_edit._parts_to_change("Dress both people in warm winter coats.") == upper  # vestir, no vestido


def test_the_clothes_mask_never_touches_face_or_hair(monkeypatch):
    labels = np.zeros((200, 100), np.uint8)
    labels[10:60, 30:70] = photo_edit.FACE
    labels[0:10, 25:75] = photo_edit.HAIR
    labels[60:200, 10:90] = photo_edit.UPPER
    monkeypatch.setattr(photo_edit, "parse_people", lambda rgb: labels)
    mask = photo_edit.clothes_mask(np.zeros((200, 100, 3), np.uint8), "Replace his shirt with a jacket.")
    assert mask[labels == photo_edit.FACE].max() == 0 and mask[labels == photo_edit.HAIR].max() == 0
    assert mask[100:190, 20:80].min() == 1


def test_leaving_the_pose_is_not_a_pose_change():
    # 2026-10-07: "leaving his ... pose ... the same" al quitar una camiseta se
    # tomaba por postura: Kontext entera, motas por toda la foto, cara pegada
    assert not photo_edit.is_pose_change("Remove the white t-shirt from the man, leaving his short beard, "
                                         "facial features, expression, hair, pose, and framing exactly the same.")
    assert photo_edit.is_pose_change("Make him kneel on the grass, leaving his clothes the same.")
