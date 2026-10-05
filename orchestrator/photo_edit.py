"""Editar una foto real con instrucciones ("ponnos en una playa", "la camiseta
verde en vez de negra") sin que cambie nada mas. FLUX Kontext hace la edicion
(ver ImageAgent.edit_with_kontext), pero redibuja la foto entera: las caras
salen parecidas, no identicas, y el resto cambia un poco. Por eso aqui se
vuelve a poner la foto ORIGINAL en todo lo que no se pidio cambiar:

- "local" (cambiar algo concreto): se compara original y editada y solo se
  toma de Kontext la zona que de verdad cambio. Medido con la foto de prueba
  (2026-10-01): lo editado difiere ~160 en Lab, el redibujado de Kontext en
  caras y arboles no pasa de ~23 - de ahi los umbrales.
- "fondo" (cambiar el sitio): Kontext mueve y reescala a las personas, asi
  que se recortan las personas de la original (MODNet), se recolocan encima
  de las de Kontext y se pegan: caras identicas pixel a pixel.

Si algo no cuadra (no hay personas, no se pueden alinear, cambio casi todo)
se devuelve lo que hizo Kontext tal cual, que sigue siendo una buena edicion."""

import base64
import io
import json
import re

import cv2
import numpy as np
import onnxruntime as ort
import pillow_heif
from PIL import Image, ImageOps

from paths import MODELS_DIR

pillow_heif.register_heif_opener()

MATTING_MODEL = MODELS_DIR / "img" / "matting" / "modnet.onnx"
KONTEXT_AREA = 1024 * 1024

_STRONG_CHANGE = 45
_WEAK_CHANGE = 22
_MAX_LOCAL_AREA = 0.6
_MIN_ALIGN_CC = 0.6

_matting_session = None

PLAN_PROMPT = """Eres el traductor de un editor de fotos con IA (FLUX Kontext). El usuario
pide un cambio sobre SU foto; el editor solo entiende instrucciones en ingles.

Lo que se ve en la foto: {scene}
Personas con la cara visible, de izquierda a derecha: {faces} ({count}).

Peticion del usuario: "{request}"

Reglas que no se pueden saltar (con fallos reales, 2026-10-05):
- Nombra a cada persona como es ("the man", "the woman with long hair") y la
  prenda u objeto CONCRETO que cambia tal como sale en la descripcion ("his
  grey wool sweater and collared shirt"). Si hay UNA sola persona, NUNCA
  escribas "all the people" ni "everyone": con eso el editor se inventaba
  gente nueva en bañador alrededor.
- Cambia SOLO lo que pide. Si no menciona la ropa, la ropa se queda tal cual
  aunque el sitio nuevo sea una playa ("ponme en la playa" = mismo jersey,
  solo cambia el fondo: un paso "fondo").
- No añadas personas que el usuario no pide. Termina siempre con "Do not add
  any other people."
- Incluye TODO lo que pide, cada cosa: si pide bañador y un coctel, las dos
  (el coctel en la mano: "holding a cocktail glass in his hand").
- El sitio o el fondo va SOLO en el paso "fondo", nunca en el paso "local".
- Ropa de baño: para un hombre "swim trunks, bare chest"; para una mujer el
  bañador o bikini que pida (si no dice, "a one-piece swimsuit").

Devuelve SOLO un JSON con "pasos": una lista con uno o dos pasos.
DOS pasos solo si pide cambiar el lugar o el fondo Y ADEMAS algo de las
personas (ropa, colores, accesorios, postura...): primero el cambio de las
personas ("local", sin tocar el fondo) y despues el del lugar ("fondo", sin
tocar a las personas). Asi las caras salen identicas. Si no, UN paso.
Cada paso lleva:
- "instruction": la instruccion en ingles, concreta y en imperativo.
  * Si hay varias personas, di a quien afecta por su posicion y como es
    ("the woman on the right", "both the man's and the woman's t-shirts";
    si no sabes quien es quien, "all the people", "every t-shirt").
  * Si cambia la postura o un gesto, describe la postura nueva con detalle
    (que brazo, que pierna, hacia donde) y que el cuerpo siga siendo de esa
    persona: "Make the woman on the right raise her right arm above her head
    in a waving gesture, with her left arm resting at her side."
  * Termina SIEMPRE con lo que no debe cambiar, por ejemplo: "Keep the
    people exactly the same: same faces, facial features, expressions, hair,
    pose and framing." QUITA de esa lista lo que el usuario SI quiere cambiar
    (si cambia la postura de alguien, no pongas "same pose" para esa persona;
    si cambia la ropa, no pongas "same clothes").
- "resumen": en español, corto y para el usuario, lo que hace ESE paso
  ("te cambio el jersey por un bañador", "te pongo en una playa").
- "mode":
  "fondo" SOLO si pide cambiar el lugar, el fondo o el escenario y no toca
          nada de las personas: ni ropa, ni accesorios, ni pose, ni quitar o
          añadir gente.
  "local" para todo lo demas (ropa, colores, objetos, quitar o añadir algo
          o a alguien, luz, estilo, expresion...).

Ejemplos:
"ponme en bañador en la playa con un coctel" (una persona: a man with a beard wearing a grey wool sweater over a collared shirt) -> {{"pasos": [{{"instruction": "Replace the man's grey wool sweater and collared shirt with swim trunks, leaving his chest bare, and put a cocktail glass in his right hand. Keep his face, facial features, expression, hair, pose and framing exactly the same. Do not add any other people.", "mode": "local", "resumen": "te cambio el jersey y la camisa por un bañador y te pongo un cóctel en la mano"}}, {{"instruction": "Change the background to a sunny beach with the sea behind him. Keep the man exactly the same: same face, facial features, expression, hair, swim trunks, cocktail, pose and framing. Do not add any other people.", "mode": "fondo", "resumen": "te pongo en una playa soleada"}}]}}
"ponme en la playa" (una persona: a man with a beard wearing a grey wool sweater over a collared shirt) -> {{"pasos": [{{"instruction": "Change the background to a sunny beach with the sea behind him. Keep the man exactly the same: same face, facial features, expression, hair, grey wool sweater and collared shirt, pose and framing. Do not add any other people.", "mode": "fondo", "resumen": "te pongo en una playa soleada, con la misma ropa"}}]}}
"ponnos en una playa" (izquierda, derecha) -> {{"pasos": [{{"instruction": "Change the background to a sunny beach with the sea behind them. Keep both people exactly the same: same faces, facial features, expressions, hair, clothes, pose and framing.", "mode": "fondo", "resumen": "os pongo en una playa soleada"}}]}}
"ponnos en la nieve con abrigos" (izquierda, derecha) -> {{"pasos": [{{"instruction": "Dress both people in warm winter coats. Keep their faces, facial features, expressions, hair, pose, the background and the framing exactly the same.", "mode": "local", "resumen": "os pongo abrigos de invierno"}}, {{"instruction": "Change the background to a snowy mountain landscape. Keep both people exactly the same: same faces, facial features, expressions, hair, clothes, pose and framing.", "mode": "fondo", "resumen": "os pongo en un paisaje nevado"}}]}}
"quita a la gente del fondo" (centro) -> {{"pasos": [{{"instruction": "Remove the other people in the background. Keep the main person and everything else exactly the same: same face, facial features, expression, hair, clothes, pose, lighting and framing.", "mode": "local", "resumen": "quito a la gente del fondo"}}]}}
"ponla a ella tumbada en la hierba" (izquierda, derecha) -> {{"pasos": [{{"instruction": "Make the woman on the right lie down on her back on the grass next to the man, her body stretched out and her head resting on the ground. Keep her face, facial features, expression, hair and clothes exactly the same, and keep the man exactly as he is.", "mode": "local", "resumen": "la tumbo a ella en la hierba"}}]}}
"que la camiseta sea verde en lugar de negra" (izquierda, derecha) -> {{"pasos": [{{"instruction": "Change the color of every black t-shirt to green, on both people. Keep everything else exactly the same: same faces, facial features, expressions, hair, pose, background, lighting and framing.", "mode": "local", "resumen": "cambio las camisetas negras a verde"}}]}}"""


WANTS_EDIT_PROMPT = """El usuario adjunta una foto y escribe: "{request}"

¿Pide MODIFICAR la foto (cambiar fondo, ropa, colores, quitar o añadir algo,
retocarla...) o solo pregunta o comenta algo SOBRE ella (que hay, quien es,
que opinas, describela...)?
Responde solo "editar" o "comentar"."""


# Peticiones de cambio que no admiten duda. qwen3:8b contestaba "comentar" a
# "ponme en bañador en la playa con un coctel" (2026-10-05): la foto iba al
# modelo de vision, que solo sabe describirla, y respondia que no lo hacia.
_EDIT_WORDS = re.compile(
    r"\b(pon(me|nos|le|les|la|lo|las|los)?|cambia(me|nos|le|la|lo)?|quita(me|nos|le|la|lo)?|"
    r"añade|añadele|agrega|mete(me|nos|le)?|viste(me|nos|le)?|vísteme|haz(me)? que|haznos|"
    r"conviert[ea](me|nos|le)?|transforma|sustituye|reemplaza|borra|elimina|retoca|edita|"
    r"en (lugar|vez) de|que (parezca|salga|este|esté|lleve)|llevando|con un[ao]? .{2,40}\ben (la|el|una|un)\b)",
    re.IGNORECASE)
_QUESTION_WORDS = re.compile(
    r"^\s*¿?\s*(que|qué|quien|quién|cuantos|cuántos|cuantas|cuántas|donde|dónde|describe|descríbeme|"
    r"como|cómo|por que|por qué|de que|de qué|sabes|es|son|hay)\b", re.IGNORECASE)


def wants_edit(ollama, model: str, request: str) -> bool:
    """En el chat automatico una foto adjunta iba siempre al modelo de vision,
    que solo sabe comentarla: "ponnos en una playa" no hacia nada. Primero las
    reglas (sin dudas ni negativas del modelo); el modelo solo para lo ambiguo."""
    if not request.strip():
        return False
    if _EDIT_WORDS.search(request):
        return True
    if _QUESTION_WORDS.match(request):
        return False
    raw = ollama.chat(model, [{"role": "user", "content": WANTS_EDIT_PROMPT.format(request=request)}],
                      temperature=0.0, think=False, num_predict=10)
    return "editar" in raw.lower()


class EditPlan:
    def __init__(self, instruction: str, mode: str, summary: str = ""):
        self.instruction = instruction
        self.mode = mode if mode in ("fondo", "local") else "local"
        self.summary = summary  # en español, para decirle al usuario que se ha hecho


DESCRIBE_PROMPT = ("Describe the people in this photo in ONE short English sentence for a photo editor: "
                   "how many, man or woman, and exactly what each one is wearing and holding. "
                   "Example: \"one man with a short beard wearing a grey wool sweater over a collared shirt\". "
                   "Only that sentence.")


def describe_photo(ollama, vision_model: str | None, image_bytes: bytes) -> str:
    """Que hay en la foto, para que el traductor sepa QUE prenda cambiar y a
    quien: solo con "una cara en el centro" escribia "pon a todas las personas
    en bañador" y Kontext añadia gente (2026-10-05). Si el modelo de vision
    falla, se sigue sin descripcion."""
    if not vision_model:
        return "(sin descripcion)"
    try:
        rgb = load_rgb(image_bytes)
        scale = 768 / max(rgb.shape[:2])
        if scale < 1:
            rgb = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        b64 = base64.b64encode(to_jpeg(rgb)).decode("ascii")
        raw = ollama.chat(vision_model, [{"role": "user", "content": DESCRIBE_PROMPT, "images": [b64]}],
                          temperature=0.0)
        return " ".join(raw.split())[:300] or "(sin descripcion)"
    except Exception:  # noqa: BLE001 - describir es una ayuda, nunca debe impedir editar
        return "(sin descripcion)"


def plan_edit(ollama, model: str, request: str, faces: list[str], scene: str = "(sin descripcion)") -> list[EditPlan]:
    """faces: posiciones de face_detect.face_positions(); scene: describe_photo().
    Uno o dos pasos: "ponla en la montaña con la ropa roja" en uno solo no
    salia - al cambiar el fondo Kontext recoloca a la persona, la ropa nueva
    impide pegar la original encima y la cara quedaba redibujada (2026-10-01)."""
    count = "una persona" if len(faces) == 1 else f"{len(faces)} personas" if faces else "ninguna persona"
    content = PLAN_PROMPT.format(request=request, faces=", ".join(faces) or "ninguna", count=count, scene=scene)
    raw = ollama.chat(model, [{"role": "user", "content": content}], temperature=0.0, think=False)
    try:
        data = json.loads(raw[raw.index("{"):raw.rindex("}") + 1])
        steps = data["pasos"] if "pasos" in data else [data]
        plans = [EditPlan(str(step["instruction"]).strip(), str(step.get("mode", "local")).strip().lower(),
                          str(step.get("resumen", "")).strip())
                 for step in steps[:2] if str(step.get("instruction", "")).strip()]
    except (ValueError, KeyError, TypeError, AttributeError, json.JSONDecodeError):
        plans = []
    # quitar algo nunca es "fondo": ese modo vuelve a pegar a TODAS las
    # personas de la original, y "quita a la gente del fondo" las devolvia
    # (qwen3:8b lo clasificaba como "fondo" por la palabra, 2026-10-05)
    for plan in plans:
        if _REMOVAL.search(plan.instruction) or _REMOVAL_ES.search(request):
            plan.mode = "local"
    # "ponme en la playa" no pide tocar a la persona: qwen3:8b copiaba el
    # ejemplo del bañador y le quitaba el jersey (2026-10-05). Si la peticion
    # no nombra nada de la persona, se queda solo el paso del fondo.
    if len(plans) == 2 and plans[1].mode == "fondo" and not _PERSON_CHANGE.search(request):
        plans = plans[1:]
    # sin instruccion en ingles Kontext entiende peor, pero entiende algo
    return plans or [EditPlan(request, "local")]


_KEEP = re.compile(r"\s*\b(Keep|Do not)\b.*$", re.IGNORECASE | re.DOTALL)


def merge_steps(plans: list[EditPlan]) -> list[EditPlan]:
    """Persona + fondo en UNA sola pasada de Kontext. Los dos pasos eran para
    no perder la cara (pegando la original encima); ahora restore_faces la
    vuelve a poner al final, y cada pasada son ~2,5 min en la RTX 5060 de 8 GB:
    Sergio paraba la edicion porque tardaba 7 (2026-10-05)."""
    if len(plans) != 2 or [p.mode for p in plans] != ["local", "fondo"]:
        return plans
    person, place = plans
    keep = _KEEP.search(person.instruction)
    keep_text = keep.group(0).strip() if keep else "Keep the people's faces, facial features, expressions, hair, pose and framing exactly the same."
    if "background" in keep_text:  # "...the background and the framing exactly the same": ya no
        keep_text = re.sub(r",\s*the background and", " and", keep_text)
        keep_text = re.sub(r",?\s*the background\b", "", keep_text)
    text = f"{_KEEP.sub('', person.instruction).strip()} {_KEEP.sub('', place.instruction).strip()} {keep_text}"
    if "Do not add any other people" not in text:
        text += " Do not add any other people."
    summary = "; ".join(s for s in (person.summary, place.summary) if s)
    return [EditPlan(text, "local", summary)]


_REMOVAL = re.compile(r"\b(remove|erase|delete|get rid of)\b", re.IGNORECASE)
_REMOVAL_ES = re.compile(r"\b(quita\w*|borra\w*|elimina\w*|saca\w*)\b", re.IGNORECASE)
# algo de la persona en la peticion: ropa, objetos, postura, "con ...", "vestido de ..."
_PERSON_CHANGE = re.compile(
    r"\b(con|sin|lleva\w*|llevando|vest\w*|viste\w*|ropa|bañador|bikini|traje|camis\w*|jersey|abrigo|"
    r"chaqueta|pantal\w*|falda|vestido|zapat\w*|gafas|gorr[ao]|sombrero|tumbad\w*|sentad\w*|de pie|"
    r"levant\w*|bail\w*|corr\w*|salt\w*|sujet\w*|cogi\w*|tomando|bebiendo|disfraz\w*|disfrazad\w*)\b",
    re.IGNORECASE)


def load_rgb(image_bytes: bytes) -> np.ndarray:
    """Las fotos del movil vienen giradas en el EXIF: sin exif_transpose la
    edicion sale tumbada."""
    return np.array(ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB"))


def to_png(rgb: np.ndarray) -> bytes:
    out = io.BytesIO()
    Image.fromarray(rgb).save(out, format="PNG")
    return out.getvalue()


def to_jpeg(rgb: np.ndarray) -> bytes:
    out = io.BytesIO()
    Image.fromarray(rgb).save(out, format="JPEG", quality=95, subsampling=0)
    return out.getvalue()


def kontext_size(width: int, height: int) -> tuple[int, int]:
    """~1 megapixel, multiplos de 16 y la MISMA proporcion que la foto. El
    nodo FluxKontextImageScale de ComfyUI recorta a una lista fija de
    proporciones y entonces Kontext reencuadra: las personas salian movidas
    y no se podia volver a poner la original encima."""
    ratio = width / height
    best = None
    for h in range(256, 4096, 16):
        w = round(h * ratio / 16) * 16
        if w < 16 or not 0.8 * KONTEXT_AREA <= w * h <= 1.05 * KONTEXT_AREA:
            continue
        err = abs(w / h - ratio)
        if best is None or err < best[0]:
            best = (err, w, h)
    return best[1], best[2]


def prepare_for_kontext(rgb: np.ndarray) -> bytes:
    w, h = kontext_size(rgb.shape[1], rgb.shape[0])
    return to_png(cv2.resize(rgb, (w, h), interpolation=cv2.INTER_AREA))


def matte(rgb: np.ndarray) -> np.ndarray:
    """Silueta de las personas, 0..1 con el pelo en suave (MODNet)."""
    global _matting_session
    if _matting_session is None:
        _matting_session = ort.InferenceSession(str(MATTING_MODEL), providers=["CPUExecutionProvider"])
    h, w = rgb.shape[:2]
    r = 512 / min(h, w)
    nh, nw = max(32, int(round(h * r / 32) * 32)), max(32, int(round(w * r / 32) * 32))
    x = (cv2.resize(rgb, (nw, nh), interpolation=cv2.INTER_AREA).astype(np.float32) - 127.5) / 127.5
    m = _matting_session.run(None, {"input": x.transpose(2, 0, 1)[None]})[0][0, 0]
    return np.clip(cv2.resize(m, (w, h), interpolation=cv2.INTER_LINEAR), 0, 1)


def _align(template: np.ndarray, moving: np.ndarray, mask: np.ndarray | None,
           init: np.ndarray) -> tuple[float, np.ndarray]:
    """Transformacion afin que lleva `moving` sobre `template` (ECC, en dos
    escalas para que aguante desplazamientos de decenas de pixeles)."""
    g1 = cv2.cvtColor(template, cv2.COLOR_RGB2GRAY).astype(np.float32)
    g2 = cv2.cvtColor(moving, cv2.COLOR_RGB2GRAY).astype(np.float32)
    base = 1024 / max(template.shape[:2])
    warp = init.astype(np.float32).copy()
    cc = 0.0
    criteria = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 300, 1e-7)
    for f in (0.25 * base, 0.5 * base):
        warp[:, 2] *= f
        small_mask = None if mask is None else cv2.resize(mask, None, fx=f, fy=f, interpolation=cv2.INTER_NEAREST)
        try:
            cc, warp = cv2.findTransformECC(cv2.resize(g1, None, fx=f, fy=f), cv2.resize(g2, None, fx=f, fy=f),
                                            warp, cv2.MOTION_AFFINE, criteria, small_mask, 5)
        except cv2.error:
            return 0.0, init
        warp[:, 2] /= f
    return cc, warp


def _rigid(warp: np.ndarray, center: tuple[float, float]) -> np.ndarray:
    """La afin mas parecida sin estirar ni cizallar (giro + zoom uniforme),
    conservando donde cae `center`. Una afin libre deformaba la cara un 2%
    (x1,032 de ancho contra x1,052 de alto) y bajaba la similitud."""
    a, b, c, d = warp[0, 0], warp[0, 1], warp[1, 0], warp[1, 1]
    scale = np.sqrt(abs(a * d - b * c))
    angle = np.arctan2(c - b, a + d)
    rot = scale * np.array([[np.cos(angle), -np.sin(angle)], [np.sin(angle), np.cos(angle)]], np.float32)
    x0 = np.array(center, np.float32)
    target = warp[:, :2] @ x0 + warp[:, 2]
    return np.hstack([rot, (target - rot @ x0)[:, None]]).astype(np.float32)


def _flow_maps(reference: np.ndarray, moving: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Mapas para cv2.remap que deforman `moving` hasta encajar con
    `reference` (flujo optico DIS). Kontext no solo reescala: mueve un poco
    la cabeza respecto al cuerpo, y con una transformacion global la cara
    seguia descolocada unos pixeles y contaba como "cambiada" (similitud
    0,946 en vez de 1,0, 2026-10-01). El flujo se suaviza mucho para que solo
    corrija esos desplazamientos suaves y no deforme lo que si es nuevo."""
    h, w = reference.shape[:2]
    f = min(1.0, 1024 / max(h, w))
    g1 = cv2.cvtColor(cv2.resize(reference, None, fx=f, fy=f, interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(cv2.resize(moving, None, fx=f, fy=f, interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
    flow = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM).calc(g1, g2, None)
    flow = cv2.GaussianBlur(flow, (0, 0), 0.01 * max(g1.shape))
    # donde algo cambia de verdad (camiseta negra -> verde) el flujo se
    # confunde y llegaba a empujar 28 px fuera de la foto (rayas en el
    # borde); lo que Kontext descoloca son unos pocos pixeles
    limit = 0.01 * max(g1.shape)
    magnitude = np.linalg.norm(flow, axis=2, keepdims=True)
    flow = flow * np.minimum(1.0, limit / np.maximum(magnitude, 1e-6))
    flow = cv2.resize(flow, (w, h), interpolation=cv2.INTER_LINEAR) / f
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    return xx + flow[..., 0], yy + flow[..., 1]


def _follow(reference: np.ndarray, moving: np.ndarray) -> np.ndarray:
    map_x, map_y = _flow_maps(reference, moving)
    return cv2.remap(moving, map_x, map_y, cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)


def _silhouette_init(a_template: np.ndarray, a_moving: np.ndarray) -> np.ndarray:
    def stats(a):
        ys, xs = np.nonzero(a > 0.5)
        return xs.mean(), ys.mean(), np.sqrt(len(xs))
    tx, ty, ts = stats(a_template)
    mx, my, ms = stats(a_moving)
    s = ms / ts
    return np.array([[s, 0, mx - s * tx], [0, s, my - s * ty]], np.float32)


def compose_local(orig: np.ndarray, edited: np.ndarray) -> np.ndarray:
    """`edited` ya a la resolucion de `orig`."""
    h, w = orig.shape[:2]
    cc, warp = _align(orig, edited, None, np.eye(2, 3))
    if cc < _MIN_ALIGN_CC:
        return edited
    flags = cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP
    ed = cv2.warpAffine(edited, warp, (w, h), flags=flags, borderMode=cv2.BORDER_REPLICATE)
    valid = cv2.warpAffine(np.ones((h, w), np.float32), warp, (w, h), flags=flags, borderValue=0)
    map_x, map_y = _flow_maps(orig, ed)
    followed = cv2.remap(ed, map_x, map_y, cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
    # junto al borde el flujo puede pedir pixeles de fuera de la foto, que se
    # rellenaban repitiendo el borde (rayas verticales): ahi vale la version
    # sin flujo
    inside = cv2.remap(np.ones((h, w), np.float32), map_x, map_y, cv2.INTER_LINEAR, borderValue=0)[..., None]
    ed = (followed * inside + ed * (1 - inside)).astype(np.uint8)
    # y lo que ni siquiera genero Kontext (encoge un poco la foto) es de la original
    valid = valid > 0.99
    k = max(3, int(min(h, w) / 300)) | 1
    lab_o = cv2.cvtColor(cv2.GaussianBlur(orig, (k, k), 0), cv2.COLOR_RGB2LAB).astype(np.float32)
    lab_e = cv2.cvtColor(cv2.GaussianBlur(ed, (k, k), 0), cv2.COLOR_RGB2LAB).astype(np.float32)
    diff = np.linalg.norm(lab_o - lab_e, axis=2)

    u = max(3, int(min(h, w) / 150))
    weak = cv2.morphologyEx((diff > _WEAK_CHANGE).astype(np.uint8), cv2.MORPH_CLOSE, np.ones((u, u), np.uint8))
    strong = cv2.morphologyEx((diff > _STRONG_CHANGE).astype(np.uint8), cv2.MORPH_OPEN, np.ones((u, u), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(weak)
    changed = np.zeros_like(weak)
    for i in np.unique(labels[strong > 0]):
        if i and stats[i, cv2.CC_STAT_AREA] > 0.0005 * h * w:
            changed[labels == i] = 1
    changed = cv2.morphologyEx(changed, cv2.MORPH_CLOSE, np.ones((u * 3, u * 3), np.uint8))
    area = changed.mean()
    # Kontext no cambio nada de verdad: su version es la misma foto con las
    # caras sutilmente redibujadas (similitud 0,96 en vez de 0,998), mejor la
    # original
    if area < 0.001:
        return orig
    # cambio casi todo (estilo, encuadre): mezclar trozos sueltos de la
    # original quedaria peor que su version entera
    if area > _MAX_LOCAL_AREA:
        return edited
    changed = cv2.dilate(changed, np.ones((u * 2, u * 2), np.uint8))
    changed = (changed * cv2.erode(valid.astype(np.uint8), np.ones((u, u), np.uint8))).astype(np.float32)
    alpha = cv2.GaussianBlur(changed, (0, 0), u)[..., None]
    return (orig * (1 - alpha) + ed * alpha).clip(0, 255).astype(np.uint8)


def compose_background(orig: np.ndarray, edited: np.ndarray) -> np.ndarray:
    """`edited` ya a la resolucion de `orig`. El resultado queda en el
    encuadre de Kontext (no se puede volver al original sin dejar huecos del
    fondo nuevo sin generar) con las personas de la foto original."""
    a_orig, a_edit = matte(orig), matte(edited)
    if (a_orig > 0.5).mean() < 0.01 or (a_edit > 0.5).mean() < 0.01:
        return compose_local(orig, edited)
    h, w = orig.shape[:2]
    u = max(3, int(min(h, w) / 70))
    people = cv2.erode((a_edit > 0.5).astype(np.uint8), np.ones((u, u), np.uint8)) * 255
    cc, warp = _align(edited, orig, people, _silhouette_init(a_edit, a_orig))
    if cc < _MIN_ALIGN_CC:
        return edited
    ys, xs = np.nonzero(people)
    warp = _rigid(warp, (xs.mean(), ys.mean()))
    flags = cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP
    orig_w = cv2.warpAffine(orig, warp, (w, h), flags=flags, borderMode=cv2.BORDER_REPLICATE)
    a_w = cv2.warpAffine(a_orig, warp, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=0)
    # donde la original no llega tras recolocarla se funde poco a poco con
    # Kontext; un corte en seco dejaba una linea sobre la ropa
    yy, xx = np.mgrid[0:h, 0:w]
    edge = np.minimum.reduce([xx, w - 1 - xx, yy, h - 1 - yy]).astype(np.float32)
    ramp = np.clip(edge / (0.06 * min(h, w)), 0, 1)
    ramp_w = cv2.warpAffine(ramp, warp, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=0)
    # el borde suave (pelo) lo pone Kontext, que ya lo tiene sobre el fondo
    # nuevo; en la original esos pixeles llevan mezclado el fondo viejo
    core = np.clip((a_w - 0.6) / 0.3, 0, 1)
    core = cv2.erode(core, np.ones((5, 5), np.uint8))
    alpha = (cv2.GaussianBlur(core, (0, 0), 2) * ramp_w)[..., None]
    people_w = _relight(orig_w, _follow(orig_w, edited), (a_w > 0.5).astype(np.float32) * (a_edit > 0.5))
    return (people_w * alpha + edited * (1 - alpha)).clip(0, 255).astype(np.uint8)


def _relight(people: np.ndarray, edited: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Las personas de la original con la luz del sitio nuevo: el detalle fino
    (cara, piel, tela) se queda de la original y la iluminacion de baja
    frecuencia se toma de Kontext, que ya las ilumino como en la escena
    nueva. Pegadas tal cual, en "Paris de noche" quedaban con luz de dia.
    Solo la luz general (desenfoque del 6%): con uno mas fino se colaban las
    sombras de la cara de Kontext y la similitud bajaba hasta 0,035. El
    desenfoque es normalizado por la mascara para que el fondo (distinto en
    cada imagen) no se cuele en el borde."""
    sigma = 0.06 * min(people.shape[:2])
    weight = cv2.GaussianBlur(mask, (0, 0), sigma)[..., None] + 1e-3

    def low(img):
        return cv2.GaussianBlur(img.astype(np.float32) * mask[..., None], (0, 0), sigma) / weight

    gain = np.clip((low(edited) + 8) / (low(people) + 8), 0.3, 3.0)
    return people.astype(np.float32) * gain


# Peticiones que cambian la cara a proposito: ahi no se vuelve a poner la original
_FACE_EDIT = re.compile(
    r"\b(gafas|lentes|cara|rostro|ojos|boca|nariz|labios|barba|bigote|afeitad\w*|maquilla\w*|sonri\w*|serio|"
    r"seria|triste|enfadad\w*|expresion|expresión|gesto|guiñ\w*|joven|mayor|viej\w*|envejec\w*|rejuvenec\w*|"
    r"arrugas|pecas|tatuaje en la cara|piercing|pelo|peinado|calvo|rubi\w*|morena?|pelirroj\w*|"
    r"mascarilla|careta|disfraz de cara)\b", re.IGNORECASE)


# Algo encima de la cabeza: ahi se vuelve a poner la cara, pero no el pelo
# (pegaba el pelo original encima del sombrero y solo asomaban las alas, 2026-10-05)
_HEAD_ITEM = re.compile(
    r"\b(sombrer\w*|gorr\w*|casco|corona|diadema|pañuelo|turbante|capucha|peluca|cinta|boina|tiara|velo|"
    r"cuernos|orejas|auriculares|cascos|capirote|bandana|visera|toca|mitra|tocado)\b", re.IGNORECASE)


def wants_face_kept(request: str) -> bool:
    return not _FACE_EDIT.search(request)


def wants_hair_kept(request: str) -> bool:
    return not _HEAD_ITEM.search(request)


def _faces(rgb: np.ndarray) -> list[np.ndarray]:
    """Caras con sus 5 puntos (ojos, nariz, comisuras), del detector YuNet."""
    import face_detect
    h, w = rgb.shape[:2]
    det = face_detect._get_detector()
    det.setInputSize((w, h))
    _, faces = det.detect(np.ascontiguousarray(rgb[:, :, ::-1]))
    return [] if faces is None else [f for f in faces if f[14] >= 0.7]


def restore_faces(orig: np.ndarray, result: np.ndarray, keep_hair: bool = True) -> np.ndarray:
    """Vuelve a poner cada cara ORIGINAL encima de la del resultado, alineada
    por ojos, nariz y boca y con la luz del sitio nuevo. Sin esto, al cambiar
    mucha ropa (jersey -> bañador) el paso "local" se quedaba entero con lo de
    Kontext, cara redibujada incluida: parecida, no la misma (similitud 0,69
    en la prueba del 2026-10-05). Si una cara cambio de postura (no encaja),
    se deja la de Kontext."""
    faces_o, faces_r = _faces(orig), _faces(result)
    if not faces_o or not faces_r:
        return result
    h, w = result.shape[:2]
    out = result.astype(np.float32)
    used = set()
    for f in faces_o:
        pts_o = f[4:14].reshape(5, 2).astype(np.float32)
        best = None
        for j, g in enumerate(faces_r):
            if j in used:
                continue
            pts_r = g[4:14].reshape(5, 2).astype(np.float32)
            m, _ = cv2.estimateAffinePartial2D(pts_o, pts_r)
            if m is None:
                continue
            err = np.linalg.norm(pts_o @ m[:, :2].T + m[:, 2] - pts_r, axis=1).mean() / max(float(g[2]), 1.0)
            if best is None or err < best[0]:
                best = (err, j, m)
        if best is None or best[0] > 0.08:
            continue
        _, j, m = best
        used.add(j)
        warped = cv2.warpAffine(orig, m, (w, h), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
        x, y, fw, fh = (float(v) for v in f[:4])
        scale = float(np.sqrt(abs(np.linalg.det(m[:, :2]))))
        # la cabeza entera (pelo, orejas, barba): solo los rasgos con el pelo y
        # el contorno de Kontext seguia sin ser la misma persona
        zone = np.zeros(orig.shape[:2], np.float32)
        cx = int(x + fw / 2)
        if keep_hair:
            cv2.ellipse(zone, (cx, int(y + fh * 0.35)), (int(fw * 0.85), int(fh * 0.95)), 0, 0, 360, 1, -1)
            # de la boca para abajo solo la mandibula: a los lados ya asoma la
            # ropa vieja (el cuello del jersey en la playa)
            zone[int(y + fh * 0.75):] = 0
        else:
            # sombrero, gorra...: de las cejas para abajo, el resto es de Kontext
            cv2.ellipse(zone, (cx, int(y + fh * 0.5)), (int(fw * 0.47), int(fh * 0.38)), 0, 0, 360, 1, -1)
        cv2.ellipse(zone, (cx, int(y + fh * 0.6)), (int(fw * 0.5), int(fh * 0.47)), 0, 0, 360, 1, -1)
        if not keep_hair:
            zone[:int(y + fh * 0.15)] = 0
        zone_w = cv2.warpAffine(zone, m, (w, h), flags=cv2.INTER_LINEAR)
        zone_w = cv2.GaussianBlur(zone_w, (0, 0), max(2.0, fw * scale * 0.05))
        # recortada por la silueta de la persona, para no traerse el fondo viejo
        head = matte(warped) * zone_w
        # el pelo de Kontext que asome por fuera de la cabeza original se borra
        leftover = (zone_w > 0.1) & (matte(result) > 0.2) & (head < 0.3) & keep_hair
        mouth_y = m[1, 0] * cx + m[1, 1] * (y + fh * 0.75) + m[1, 2]
        leftover[max(0, int(mouth_y)):] = False  # el cuello nuevo se queda
        if leftover.any():
            k = max(7, int(fw * scale * 0.04)) | 1
            grown = cv2.dilate(leftover.astype(np.uint8), np.ones((k, k), np.uint8))
            # la cabeza entra tambien en el hueco: que se rellene solo con fondo y
            # no con la piel de la cabeza de Kontext de al lado (va encima despues)
            hole = grown | cv2.dilate((head > 0.05).astype(np.uint8), np.ones((k, k), np.uint8))
            filled = cv2.inpaint(out.clip(0, 255).astype(np.uint8), hole, 9, cv2.INPAINT_TELEA).astype(np.float32)
            out = np.where(grown[..., None] > 0, filled, out)
        lit = _relight(warped, result, (head > 0.5).astype(np.float32))
        alpha = head[..., None]
        out = lit * alpha + out * (1 - alpha)
    return out.clip(0, 255).astype(np.uint8)


def finish(orig: np.ndarray, edited_small: np.ndarray, mode: str) -> np.ndarray:
    """`edited_small`: lo que devolvio Kontext (o ya reescalado), se lleva a la
    resolucion de la original antes de componer."""
    h, w = orig.shape[:2]
    edited = cv2.resize(edited_small, (w, h), interpolation=cv2.INTER_LANCZOS4)
    if mode == "fondo":
        return compose_background(orig, edited)
    return compose_local(orig, edited)


def needs_upscale(orig: np.ndarray, edited_small: np.ndarray) -> bool:
    return max(orig.shape[:2]) > 1.4 * max(edited_small.shape[:2])

