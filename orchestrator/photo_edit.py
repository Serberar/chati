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
import logging
import re

import cv2
import numpy as np
import onnxruntime as ort
import pillow_heif
from PIL import Image, ImageOps

import clothing_terms
from paths import MODELS_DIR

log = logging.getLogger("chati")

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
{history}
Peticion del usuario: "{request}"
{glossary}
Reglas que no se pueden saltar (con fallos reales, 2026-10-05):
- Nombra a cada persona como es ("the man", "the woman with long hair") y la
  prenda u objeto CONCRETO que cambia tal como sale en "Lo que se ve en la
  foto" de ARRIBA, nunca la de los ejemplos de abajo (si la foto dice "a white
  t-shirt", es "his white t-shirt"; copiar "grey wool sweater" de un ejemplo
  dejaba la ropa sin cambiar, 2026-10-07). Si hay UNA sola persona, NUNCA
  escribas "all the people" ni "everyone": con eso el editor se inventaba
  gente nueva en bañador alrededor.
- Cambia SOLO lo que pide. Si no menciona la ropa, la ropa se queda tal cual
  aunque el sitio nuevo sea una playa ("ponme en la playa" = mismo jersey,
  solo cambia el fondo: un paso "fondo").
- Si la peticion se refiere a algo de un cambio anterior ("quitale el gorro",
  "el coctel mas grande"), usa ese cambio para saber QUE es y a QUIEN: "the red
  wool hat of the person on the left". Con la descripcion sola el editor tocaba
  otra cosa (cambio el abrigo de otro en vez de quitar el gorro, 2026-10-06).
- Una peticion corta sin decir de que ("mas grande", "mas oscuro", "en rojo",
  "mas a la izquierda") habla de lo ULTIMO que se cambio: tras "con un sombrero
  de paja", "mas grande" es el sombrero, no la persona.
- Cambiar SOLO una parte del fondo (el cielo, el mar, la pared, el suelo) es
  "local", no "fondo": nombra lo que se queda ("replace only the sky; keep the
  mountains, trees, path and the man exactly the same"). Con "fondo" el editor
  rehacia el paisaje entero y desaparecian las montañas (2026-10-06).
- Sin personas en la foto, habla del sujeto que diga la descripcion ("the dog")
  y en singular si es uno: nunca "the person on the left", "them" ni "both".
  Termina con "Do not add any other animals or people." (con "them" salian dos
  perros, 2026-10-06).
- De noche, al atardecer o con poca luz: que la escena se siga viendo bien y,
  si hay lamparas o luces, encendidas dando luz calida ("at night, the lamps
  switched on casting warm light, the room cozy and clearly visible"). Sin eso
  salia casi negro (2026-10-06).
- Posturas e interacciones (de pie, tumbado, de rodillas, inclinado, la mano
  arriba, abrazarse, bailar, ir de la mano, un beso): UNA frase natural y clara,
  como se describiria una foto real ("the man and the woman hug each other
  warmly, her head resting on his shoulder"). NO coreografia brazo por brazo ni
  dedo por dedo: con eso salian brazos y manos raros (2026-10-06). Solo di que
  mano o brazo si el usuario lo dice ("la mano derecha"). Las caras siguen
  visibles hacia la camara. En un cambio de postura NUNCA "same pose" ni "same
  framing"; si hace falta ver mas cuerpo (manos en la cintura, piernas, de
  rodillas, tumbado, bailando), "zoomed out to show <lo que haga falta>".
- Mejor una accion concreta y visual que una abstracta: "bailar" -> "he twirls
  her under his raised arm like a salsa couple" (con "dance together" salian
  quietos de la mano). Si la accion pasa en las manos o el cuerpo, "zoomed out"
  para que se vea (ir de la mano sin abrir el plano dejaba las manos cortadas y
  el editor no las juntaba, 2026-10-06).
- Si la postura nueva sustituye a una de un cambio anterior (estaban bailando
  y ahora "de la mano mirando a camara"), dilo: "they stop dancing and now
  stand still side by side, facing the camera, holding hands". Sin eso el
  editor dejaba la postura de antes (2026-10-06).
- Una postura nueva sustituye ENTERA a la anterior, brazos incluidos ("tumbada
  en un sofa" tras "con los brazos cruzados" no lleva los brazos cruzados),
  salvo que el usuario diga mantener algo.
- Tumbado/tumbada es horizontal: "lying down stretched out on her side along
  the sofa, her head on a cushion, her whole body horizontal, zoomed out to
  show her whole body". Con menos, el editor la dejaba sentada (2026-10-06).
- De pie desde una foto sentada o de medio cuerpo: "standing up, zoomed out to
  show her whole body from head to feet".
- "Darse la mano" es ir de la mano, uno junto al otro ("holding hands"), no
  un apreton de manos (salvo que diga "estrecharse la mano").
- No añadas personas que el usuario no pide. Termina siempre con "Do not add
  any other people."
- Incluye TODO lo que pide, cada cosa: si pide bañador y un coctel, las dos
  (el coctel en la mano: "holding a cocktail glass in his hand").
- Un objeto en la mano: UNO, en una mano concreta, y la otra como estaba
  ("holding a single cocktail glass in his right hand, his left arm relaxed
  and empty"). Si no, el editor ponia uno en cada mano (2026-10-06).
- Lo que ya tiene en las manos y no se pide cambiar (un movil, una taza...)
  se nombra en lo que se mantiene: "still holding her single smartphone in her
  right hand" (el editor lo duplicaba en la otra mano). PERO en un cambio de
  POSTURA no: las manos libres y relajadas, salvo que el usuario nombre el
  objeto ("con la copa"). Salia tirandose en paracaidas con la copa (2026-10-08).
- El sitio o el fondo va SOLO en el paso "fondo", nunca en el paso "local".
- Ropa de baño: para un hombre "swim trunks, bare chest"; para una mujer el
  bañador o bikini que pida (si no dice, "a one-piece swimsuit").
- Quitar una prenda: di que queda a la vista en su lugar, no solo "remove".
  Con "Remove the white t-shirt" el editor dejaba otra camiseta beige
  (2026-10-07). "quitame la camiseta" (un hombre) -> "Remove the man's white
  t-shirt completely so that he is shirtless, with his bare neck, chest and
  shoulders visible."; quitar una chaqueta -> lo que lleva debajo ("showing
  the shirt he wears underneath"). Di siempre lo que SE VE, nunca lo que no
  ("Do not add a necklace" hacia que el editor dibujara un collar).
- Al cambiar ropa, describe la prenda nueva con TODO lo que diga el usuario
  (color, tirantes, mangas, largo, tejido, estampado) y mantén el cuerpo:
  "Keep her body shape and proportions exactly the same." (el editor le
  cambiaba la figura y el escote, foto real de Sergio, 2026-10-07).

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
- "resumen": en español, corto y en pasado, lo que se ha hecho en ESE paso, con
  la misma persona que use el usuario: "ponme..." -> "te he puesto...",
  "ponla..." -> "la he puesto...", "ponnos..." -> "os he puesto..." ("te he
  cambiado el jersey por un bañador", "la he puesto de pie", "le he quitado el
  gorro").
- "mode":
  "fondo" SOLO si pide cambiar el lugar, el fondo o el escenario y no toca
          nada de las personas: ni ropa, ni accesorios, ni pose, ni quitar o
          añadir gente.
  "local" para todo lo demas (ropa, colores, objetos, quitar o añadir algo
          o a alguien, luz, estilo, expresion...).

Ejemplos:
"ponme en bañador en la playa con un coctel" (una persona: a man with a beard wearing a grey wool sweater over a collared shirt) -> {{"pasos": [{{"instruction": "Replace the man's grey wool sweater and collared shirt with swim trunks, leaving his chest bare, and put a single cocktail glass in his right hand, his left arm relaxed and empty. Keep his face, facial features, expression, hair, pose and framing exactly the same. Do not add any other people.", "mode": "local", "resumen": "te he cambiado el jersey y la camisa por un bañador y te he puesto un cóctel en la mano"}}, {{"instruction": "Change the background to a sunny beach with the sea behind him. Keep the man exactly the same: same face, facial features, expression, hair, swim trunks, cocktail, pose and framing. Do not add any other people.", "mode": "fondo", "resumen": "te he puesto en una playa soleada"}}]}}
"ponme en la playa" (una persona: a man with a beard wearing a grey wool sweater over a collared shirt) -> {{"pasos": [{{"instruction": "Change the background to a sunny beach with the sea behind him. Keep the man exactly the same: same face, facial features, expression, hair, grey wool sweater and collared shirt, pose and framing. Do not add any other people.", "mode": "fondo", "resumen": "te he puesto en una playa soleada, con la misma ropa"}}]}}
"ponnos en una playa" (izquierda, derecha) -> {{"pasos": [{{"instruction": "Change the background to a sunny beach with the sea behind them. Keep both people exactly the same: same faces, facial features, expressions, hair, clothes, pose and framing.", "mode": "fondo", "resumen": "os he puesto en una playa soleada"}}]}}
"ponnos en la nieve con abrigos" (izquierda, derecha) -> {{"pasos": [{{"instruction": "Dress both people in warm winter coats. Keep their faces, facial features, expressions, hair, pose, the background and the framing exactly the same.", "mode": "local", "resumen": "os he puesto abrigos de invierno"}}, {{"instruction": "Change the background to a snowy mountain landscape. Keep both people exactly the same: same faces, facial features, expressions, hair, clothes, pose and framing.", "mode": "fondo", "resumen": "os he puesto en un paisaje nevado"}}]}}
"quitale el gorro" (cambios anteriores: "que el de la izquierda lleve un gorro de lana rojo") -> {{"pasos": [{{"instruction": "Remove the red wool hat from the person on the left, showing their hair as it was. Keep everything else exactly the same: same faces, facial features, expressions, clothes, pose, background and framing.", "mode": "local", "resumen": "le he quitado el gorro rojo al de la izquierda"}}]}}
"mas grande" (cambios anteriores: "ponme en la playa", "con un sombrero de paja") -> {{"pasos": [{{"instruction": "Make the man's straw hat noticeably bigger, with a wider brim. Keep everything else exactly the same: same face, facial features, expression, hair, clothes, pose, background and framing.", "mode": "local", "resumen": "te he puesto el sombrero mas grande"}}]}}
"cambia el cielo por un atardecer naranja" (una persona: a man on a mountain trail, snowy peaks and pine trees behind) -> {{"pasos": [{{"instruction": "Replace only the sky with a warm orange sunset sky. Keep the snowy mountains, the pine trees, the trail and the man exactly the same: same face, facial features, expression, hair, clothes, pose and framing. Do not add any other people.", "mode": "local", "resumen": "te he cambiado el cielo por un atardecer naranja"}}]}}
"ponme de rodillas" (una persona: a man in a sweater, upper body photo) -> {{"pasos": [{{"instruction": "Make the man kneel on the ground, zoomed out to show his full body, looking at the camera. Keep his face, hair, clothes and the background exactly the same. Do not add any other people.", "mode": "local", "resumen": "te he puesto de rodillas"}}]}}
"con la mano derecha levantada saludando" (una persona: a woman) -> {{"pasos": [{{"instruction": "Make the woman wave at the camera with her right hand raised. Keep her face, hair, clothes, background and framing exactly the same. Do not add any other people.", "mode": "local", "resumen": "te he puesto saludando con la mano derecha"}}]}}
"que nos abracemos" (izquierda, derecha: a man and a woman) -> {{"pasos": [{{"instruction": "Make the man and the woman hug each other warmly, side by side, her head resting on his shoulder, both smiling at the camera with their faces visible. Keep their faces, hair, clothes and the background exactly the same. Do not add any other people.", "mode": "local", "resumen": "os he puesto abrazados"}}]}}
"que el le de un beso en la mejilla" (izquierda, derecha: a man and a woman) -> {{"pasos": [{{"instruction": "Make the man kiss the woman on her cheek while she smiles at the camera. Keep their faces, hair, clothes and the background exactly the same. Do not add any other people.", "mode": "local", "resumen": "le he puesto dandote un beso en la mejilla"}}]}}
"que estemos bailando" (izquierda, derecha: a man and a woman) -> {{"pasos": [{{"instruction": "Make the man and the woman dance together like a salsa couple: he twirls her under his raised arm, her dress swirling, both smiling, zoomed out to show their full bodies. Keep their faces, hair, clothes and the background exactly the same. Do not add any other people.", "mode": "local", "resumen": "os he puesto bailando juntos"}}]}}
"que nos demos la mano" (izquierda, derecha: a man and a woman) -> {{"pasos": [{{"instruction": "Make the man and the woman hold hands: his left hand and her right hand are clasped together between them, zoomed out to show their full bodies and their joined hands clearly, both smiling at the camera. Keep their faces, hair, clothes and the background exactly the same. Do not add any other people.", "mode": "local", "resumen": "os he puesto de la mano"}}]}}
"quita a la gente del fondo" (centro) -> {{"pasos": [{{"instruction": "Remove the other people in the background. Keep the main person and everything else exactly the same: same face, facial features, expression, hair, clothes, pose, lighting and framing.", "mode": "local", "resumen": "he quitado a la gente del fondo"}}]}}
"ponla a ella tumbada en la hierba" (izquierda, derecha) -> {{"pasos": [{{"instruction": "Make the woman on the right lie down on her back on the grass next to the man, her body stretched out and her head resting on the ground. Keep her face, facial features, expression, hair and clothes exactly the same, and keep the man exactly as he is.", "mode": "local", "resumen": "la he tumbado a ella en la hierba"}}]}}
"que la camiseta sea verde en lugar de negra" (izquierda, derecha) -> {{"pasos": [{{"instruction": "Change the color of every black t-shirt to green, on both people. Keep everything else exactly the same: same faces, facial features, expressions, hair, pose, background, lighting and framing.", "mode": "local", "resumen": "he cambiado las camisetas negras a verde"}}]}}"""


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


# ---------- que quiere el usuario cuando ya hay una foto en la conversacion ----------

_UNDO = re.compile(
    r"\b(deshaz\w*|deshacer|vuelve (a como estaba|atr[aá]s|a la (de antes|anterior))|volver atr[aá]s|"
    r"como estaba (antes)?|la (foto|imagen|versi[oó]n) (anterior|de antes)|"
    r"quita(le)? (el|ese) ([uú]ltimo )?cambio|no,? (as[ií] )?no,? (vuelve|d[eé]jala))\b", re.IGNORECASE)
_TO_ORIGINAL = re.compile(
    r"\b((vuelve|volver|vuelvo) a la (foto |imagen )?original|la (foto|imagen) original|"
    r"empieza(mos)? (otra vez |de nuevo )?(con|desde) la original|desde la original)\b", re.IGNORECASE)
# reintento puro (sin cambiar lo pedido): se repite el ultimo cambio desde la version anterior
_RETRY = re.compile(
    r"^\W*(no me gusta[\s,.!]*|no[\s,.!]+|mal[\s,.!]+)?(otra vez|int[eé]nta(lo)? (otra vez|de nuevo)|"
    r"prueba (otra vez|de nuevo)|rep[ií]te(lo)?|hazlo (otra vez|de nuevo)|otra (versi[oó]n|opci[oó]n|distinta)|"
    r"dame otra|otra)\W*$", re.IGNORECASE)
# claramente no es la foto (va al chat normal o al agente)
_NOT_THE_PHOTO = re.compile(
    r"\b(agente|bug|error del|fallo del|login|archivo|carpeta|documento|programa\w*|c[oó]digo|script|"
    r"instala\w*|ordenador|correo|e-?mail|excel|word|pdf)\b", re.IGNORECASE)
_NEW_IMAGE = re.compile(
    r"\b(otra (imagen|foto) (de|con)|imagen nueva|foto nueva|desde cero|"
    r"(hazme|genera(me)?|crea(me)?|dib[uú]ja(me)?) (una|un) (imagen|foto|dibujo|ilustraci[oó]n) (de|con))\b",
    re.IGNORECASE)
# arreglos y ajustes tipicos de una conversacion editando ("mas grande", "que sonria")
_TWEAK = re.compile(
    r"\b(corrig\w*|arregl\w*|que no (salga|haya|lleve|tenga|aparezca|se vea)|sin (la|el|los|las|esa|ese|esos|esas) |"
    r"sobra\w*|deja(me|la|lo)? (solo|igual)|mant[eé]n\w*|igual que antes|m[aá]s (grande|peque[nñ]|claro|oscur|"
    r"alto|bajo|largo|corto|cerca|lejos|real|natural|bonit|delgad|gord|joven|mayor|serio|feliz|brillante|"
    r"saturad|color|luz|sol|nubes|gente)\w*|menos \w+|que (sonr[ií]a|mire|est[eé]|parezca|se vea|salga|tenga|lleve|sea|haya|se|vaya|brille|"
    r"use|coja|sujete|beba|coma|baile|salte|corra|vuele|nieve|llueva)|"
    r"m[aá]s a la (izquierda|derecha)|en vez de|en lugar de|cambia\w*|ahora (con|sin|en|de|que|ponle|ponme|ponla)|"
    r"que (nos|se|os|me|te|le|les) \w+|"
    r"tambi[eé]n|y (ahora|tambi[eé]n|que)|el (pelo|fondo|cielo|color|sombrero|vestido|ba[nñ]ador|coctel|c[oó]ctel)|"
    r"la (cara|ropa|luz|mano|camiseta|chaqueta|gorra|playa|monta[nñ]a))\b", re.IGNORECASE)

_REFERS_TO_PHOTO = re.compile(r"\b(me|nos|conmigo|yo|mi|esta foto|esa foto|la foto|la misma|el mismo)\b",
                              re.IGNORECASE)
# cortesia: nunca es un cambio ("gracias" no puede lanzar otra edicion)
_THANKS = re.compile(
    r"^\W*(muchas |mil )?(gracias|perfecto|genial|vale|ok|okay|guay|me encanta|bien|est[aá] bien|as[ií] s[ií]|"
    r"ya est[aá]|estupendo|chulo|precioso|bonito|qu[eé] bien|de lujo|brutal)([\s,.!]+(gracias|as[ií]|me gusta))?\W*$",
    re.IGNORECASE)
_ABOUT_PHOTO = re.compile(
    r"\b(foto|imagen|lleva|llevo|sale|salgo|sal[ei]mos|aparece|ves|hay|color|qui[eé]n|d[oó]nde|ropa|fondo|"
    r"se ve|parece|parezco|queda|quedo|est[aá]|estoy|tiene|tengo|mano|cara|pelo)\b", re.IGNORECASE)
_SHOW_ME = re.compile(
    r"\b(c[oó]mo (quedar[ií]a|ser[ií]a|estar[ií]a|saldr[ií]a)|qu[eé] tal (si|con|en)|y si |a ver (c[oó]mo|si|qu[eé])|"
    r"mu[eé]stra(me)?|ens[eé][nñ]a(me)?|pru[eé]ba(lo)? (con|en|sin)|podr[ií]as (poner|quitar|cambiar|hacer|a[nñ]adir)|"
    r"puedes (poner|quitar|cambiar|hacer|a[nñ]adir))", re.IGNORECASE)

INTENT_PROMPT = """El usuario esta editando una foto con un asistente. Lo ultimo que se le pidio a la foto: "{last}".
Nuevo mensaje del usuario: "{message}"

¿Que quiere? Responde con UNA sola palabra:
- editar: cualquier cambio, ajuste, correccion o queja sobre ESA foto, aunque sea corto o vago ("mas", "mas grande", "el pelo esta raro", "que sonria", "sin eso", "mejor de noche", "no me gusta el color", "ponle algo en la mano").
- nueva: quiere una imagen distinta desde cero, que no parte de esa foto ("ahora hazme un perro en la luna").
- otra: una pregunta o comentario que no pide cambiar nada, o algo que no tiene nada que ver con imagenes.
Si dudas entre editar y otra cosa, responde editar."""


def photo_intent(ollama, model: str, message: str, last_request: str = "") -> str:
    """Con una foto ya en la conversacion: "deshacer", "original", "repetir",
    "editar", "nueva" u "otra". Primero reglas; el modelo solo para lo dudoso,
    y sabiendo que hay una foto en marcha (antes no lo sabia: "haz que..."
    acababa en el agente, Sergio 2026-10-05)."""
    text = (message or "").strip()
    if not text:
        return "otra"
    if _TO_ORIGINAL.search(text):
        return "original"
    if _UNDO.search(text):
        return "deshacer"
    if _RETRY.match(text):
        return "repetir"
    if _NOT_THE_PHOTO.search(text):
        return "otra"
    if _THANKS.match(text):
        return "otra"
    if _NEW_IMAGE.search(text) and not _REFERS_TO_PHOTO.search(text):
        return "nueva"
    if _EDIT_WORDS.search(text) or _SHOW_ME.search(text):
        # _SHOW_ME: "¿como quedaria de noche?" quiere verlo, no que se lo cuenten
        return "editar"
    # pregunta de verdad: "¿de que color es el bañador?". "que sea de noche" no
    # lo es (iba al chat de texto, que contestaba "he hecho esto" sin tocar la foto)
    if _QUESTION_WORDS.match(text.lstrip("¿")) and ("?" in text or text.startswith("¿")):
        # sobre la foto: la contesta el modelo de vision mirandola (el de texto no la ve y se la inventaba)
        return "preguntar" if _ABOUT_PHOTO.search(text) else "otra"
    if _TWEAK.search(text):
        return "editar"
    if "?" in text and _ABOUT_PHOTO.search(text):
        return "preguntar"
    raw = ollama.chat(model, [{"role": "user", "content": INTENT_PROMPT.format(last=last_request or "(nada aun)",
                                                                            message=text)}],
                      temperature=0.0, think=False, num_predict=10).lower()
    for answer in ("nueva", "otra", "editar"):
        if answer in raw:
            return answer
    return "editar"


class EditPlan:
    def __init__(self, instruction: str, mode: str, summary: str = ""):
        self.instruction = instruction
        self.mode = mode if mode in ("fondo", "local") else "local"
        self.summary = summary  # en español, para decirle al usuario que se ha hecho


# Personas, animales u objetos: solo con "describe the people", en la foto de un
# perro el planificador no sabia que habia y escribia "the person on the left" o
# "them" (salian dos perros en la playa, 2026-10-06)
DESCRIBE_PROMPT = ("Describe the main subject of this photo in ONE short English sentence for a photo editor: "
                   "how many people or animals there are and what they are (man, woman, dog...), exactly what each "
                   "one is wearing and holding, and if there are none, the main object. "
                   "Examples: \"one man with a short beard wearing a grey wool sweater over a collared shirt\", "
                   "\"one yellow labrador dog sitting on grass, no people\". Only that sentence.")


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


# ---------- comprobar que se hizo TODO lo pedido ----------
# Sergio, 2026-10-05: "ha hecho la mitad de lo que he pedido" (bikini y coctel
# en la playa: salio en la playa con la misma ropa). Kontext a veces se salta
# una parte; ahora el modelo de vision mira el resultado y, si falta algo, se
# repite una vez insistiendo en eso.

# Antes eran preguntas sueltas que escribia qwen3:8b ("¿lleva exactamente una
# blusa?" cuando la blusa se habia quitado, "¿hay exactamente una nieve?"): daban
# avisos falsos y repeticiones de 3,5 min sin motivo. Mejor que el modelo de
# vision juzgue directamente con la orden delante (probado el 2026-10-06 con
# resultados conocidos: detecta los dos moviles y el gorro sin quitar, y no
# protesta en los buenos).
VERIFY_PROMPT = """A photo editor was asked by the user (in Spanish) to do this to a photo:
"{instruction}"

Look carefully at the resulting photo below and judge it strictly:
- Is every requested change clearly visible?
- Is anything duplicated that should be single (for example a person holding the same object in both hands, or two drinks when one was asked)?

Answer on ONE line, exactly in one of these two forms:
yes
no | <what is wrong, in English, a few words> | <lo mismo en español>"""


class Verdict:
    def __init__(self, ok: bool, problem_en: str = "", problem_es: str = ""):
        self.ok, self.problem_en, self.problem_es = ok, problem_en, problem_es


def change_requested(steps) -> str:
    """Lo que se pidio a Kontext, sin la coletilla de lo que se mantiene."""
    return " ".join(_KEEP.sub("", s.instruction).strip() for s in steps).strip()


def verify_edit(ollama, vision_model: str | None, image_bytes: bytes, instruction: str) -> Verdict | None:
    """¿Se ve todo lo pedido? None si no se pudo comprobar (sin modelo de
    vision o respuesta ilegible): entonces se da por buena, comprobar nunca
    debe estropear una edicion."""
    if not vision_model or not instruction:
        return None
    try:
        rgb = load_rgb(image_bytes)
        scale = 768 / max(rgb.shape[:2])
        if scale < 1:
            rgb = cv2.resize(rgb, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        b64 = base64.b64encode(to_jpeg(rgb)).decode("ascii")
        raw = ollama.chat(vision_model, [{"role": "user", "content": VERIFY_PROMPT.format(instruction=instruction),
                                          "images": [b64]}], temperature=0.0)
    except Exception:  # noqa: BLE001
        return None
    line = " ".join(raw.split())
    if re.match(r"^\W*yes\b", line, re.IGNORECASE):
        return Verdict(True)
    if not re.match(r"^\W*no\b", line, re.IGNORECASE):
        return None
    parts = [p.strip(" .") for p in line.split("|")]
    return Verdict(False, parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else "")


def insist(instruction: str, problem_en: str) -> str:
    """La misma instruccion, recalcando lo que salio mal la primera vez."""
    if not problem_en:
        return f"{instruction} Important: make sure every requested change is clearly visible."
    return f"{instruction} Important, the previous attempt failed because: {problem_en}. Fix exactly that."


HISTORY_BLOCK = """
Esta foto ya es el resultado de cambios anteriores que el usuario pidio, en orden:
{items}
La peticion nueva puede referirse a ellos ("mas grande", "quitale eso", "no, el
otro", "mejor rojo"): entiendela con ese contexto y cambia SOLO lo nuevo; lo de
antes ya esta hecho en la foto y se queda como esta.
"""


def plan_edit(ollama, model: str, request: str, faces: list[str], scene: str = "(sin descripcion)",
              history: list[str] | None = None) -> list[EditPlan]:
    """faces: posiciones de face_detect.face_positions(); scene: describe_photo().
    Uno o dos pasos: "ponla en la montaña con la ropa roja" en uno solo no
    salia - al cambiar el fondo Kontext recoloca a la persona, la ropa nueva
    impide pegar la original encima y la cara quedaba redibujada (2026-10-01).
    history: lo pedido antes sobre esta foto, en orden (conversacion)."""
    count = "una persona" if len(faces) == 1 else f"{len(faces)} personas" if faces else "ninguna persona"
    past = HISTORY_BLOCK.format(items="\n".join(f"- {h}" for h in history[-6:])) if history else ""
    content = PLAN_PROMPT.format(request=request, faces=", ".join(faces) or "ninguna", count=count, scene=scene,
                                 history=past, glossary=clothing_terms.glossary_block(request))
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
    for plan in plans:
        plan.instruction = _keep_body(clothing_terms.fix_instruction(request, plan.instruction))
        plan.instruction = _real_clothes(plan.instruction, scene)
        plan.instruction = _natural_pose(plan.instruction, request)
        plan.summary = _same_person(plan.summary, [*(history or []), request])
    # sin instruccion en ingles Kontext entiende peor, pero entiende algo
    return plans or [EditPlan(request, "local")]


_HELD = re.compile(r",?\s*(still\s+)?holding\b[^.,]*?(glass|cup|mug|drink|cocktail|beer|wine|phone|smartphone|"
                   r"bottle|can)\b[^.,]*", re.IGNORECASE)
# en la lista de "Keep ... exactly the same", lo que se tenia en la mano
# ("the glass of water", "the lemon slice in his hand")
_OBJECT_ITEM = re.compile(r"(,\s*(?:and\s+)?|\s+and\s+)(?:the\s+|his\s+|her\s+|their\s+)?[\w\s-]*?\b(glass|cup|mug|drink|"
                          r"cocktail|beer|wine|phone|smartphone|bottle|can|lemon|straw)\b[\w\s-]*?(?=,|\s+and\b|\s+exactly\b|\.)",
                          re.IGNORECASE)
_MENTIONS_OBJECT = re.compile(r"\b(copa|vaso|taza|bebida|coctel|cóctel|cerveza|vino|m[oó]vil|tel[eé]fono|botella|lata)\b",
                              re.IGNORECASE)


def _natural_pose(instruction: str, request: str) -> str:
    """En un cambio de postura: la cabeza recta y natural, no la inclinacion
    que tenia por el angulo de la camara (en un selfie ladeado, de pie parecia
    jorobado), y las manos libres salvo que pida el objeto (salia en
    paracaidas con la copa). Sergio, 2026-10-08."""
    if not is_pose_change(instruction):
        return instruction
    low = instruction.lower()
    who = "her" if re.search(r"\b(woman|her|she|girl)\b", low) else \
        "his" if re.search(r"\b(man|his|he|boy)\b", low) else "their"
    if not _MENTIONS_OBJECT.search(request or ""):
        change, keep = instruction, ""
        m = _KEEP.search(instruction)
        if m:
            change, keep = instruction[:m.start()], instruction[m.start():]
        change = _HELD.sub("", change)
        keep = _OBJECT_ITEM.sub("", _HELD.sub("", keep))
        # ", keeping ..." sigue la frase; "Keep ..." empieza otra
        joint = "." if not keep or keep.lstrip(" ,")[:1].isupper() else ""
        instruction = change.rstrip(" ,.") + f", with {who} hands empty, open and relaxed{joint}" + keep
    posture = (f" {who.capitalize()} head is upright and straight in a natural relaxed posture, "
               f"as seen from a natural eye-level camera angle.")
    m = re.search(r"\s*Keep\b", instruction)
    if m:
        return instruction[:m.start()].rstrip() + posture + " " + instruction[m.start():].lstrip()
    return instruction.rstrip() + posture


_REPLACE_OBJ = re.compile(r"\bReplace\s+(the man's|the woman's|the person's|his|her|their)\s+(.+?)\s+with\s+",
                          re.IGNORECASE | re.DOTALL)
_SCENE_GARMENT = re.compile(
    r"((?:[a-z-]+\s+){0,2}(?:t-shirt|shirt|blouse|sweater|jumper|hoodie|sweatshirt|jacket|coat|blazer|dress|top|"
    r"tank top|polo shirt|cardigan|vest|suit|uniform|turtleneck(?: sweater)?))\b", re.IGNORECASE)
_NOT_ADJ = {"a", "an", "the", "wearing", "in", "with", "and", "his", "her", "their", "over", "under"}


def _real_clothes(instruction: str, scene: str) -> str:
    """Si la instruccion cambia prendas que la foto no tiene, se nombran las
    que si tiene. qwen3:8b copiaba la del ejemplo ("his grey wool sweater and
    collared shirt") con una camiseta blanca: Kontext no encontraba ningun
    jersey, no cambiaba nada y la edicion se repetia para nada (5 min,
    Sergio, 2026-10-07)."""
    m = _REPLACE_OBJ.search(instruction or "")
    if not m or not scene:
        return instruction
    named = {g.lower() for g in _CLOTHES_EN.findall(m.group(2))}
    scene_l = scene.lower()
    # la prenda entera: "shirt" no esta en "t-shirt"
    if not named or any(re.search(rf"(?<![\w-]){re.escape(g)}\b", scene_l) for g in named):
        return instruction
    found = _SCENE_GARMENT.search(scene)
    if not found:
        return instruction
    words = [w for w in found.group(1).split() if w.lower() not in _NOT_ADJ]
    return instruction[:m.start(2)] + " ".join(words) + instruction[m.end(2):]


_CLOTHES_EN = re.compile(
    r"\b(sweater|shirt|t-shirt|blouse|dress|gown|jacket|coat|blazer|suit|tuxedo|hoodie|sweatshirt|cardigan|vest|"
    r"skirt|trousers|pants|jeans|shorts|swimsuit|swim trunks|bikini|outfit|clothes|clothing|top|tank top|"
    r"jumpsuit|uniform)\b",
    re.IGNORECASE)


def _keep_body(instruction: str) -> str:
    """Al cambiar la ropa, Kontext cambiaba tambien la figura (mas pecho, otro
    escote) en la foto real de Sergio (2026-10-07); qwen3:8b no lo añadia
    aunque la regla se lo pedia. No en cambios de postura: ahi el cuerpo se
    mueve a proposito."""
    if not _CLOTHES_EN.search(_KEEP.sub("", instruction)) or "body shape" in instruction.lower() \
            or is_pose_change(instruction):
        return instruction
    low = instruction.lower()
    who = "her" if re.search(r"\b(woman|her|she|girl)\b", low) else \
        "his" if re.search(r"\b(man|his|he|boy)\b", low) else "their"
    # Nada de "Do not add any necklace": estos modelos entienden mal las
    # negaciones y nombrarlo le hacia dibujar un collar (2026-10-07)
    keep = f"Keep {who} body shape and proportions exactly the same."
    if who == "her":
        # con solo "body shape" Kontext le agrandaba el pecho y bajaba el escote
        # al ponerle un vestido (Mon, 2026-10-08); dicho en positivo
        keep = ("Keep her body shape, her natural bust size and her proportions exactly the same as in the "
                "photo, with a modest neckline.")
    m = re.search(r"\s*Do not add any other", instruction)
    if m:
        return f"{instruction[:m.start()].rstrip()} {keep} {instruction[m.start():].lstrip()}"
    return f"{instruction.rstrip()} {keep}"


_FIRST_PERSON = re.compile(r"\b(me|ponme|mi|mis|yo|conmigo|hazme|quítame|quitame|cámbiame|cambiame)\b", re.IGNORECASE)
_THIRD_PERSON = re.compile(r"\b(ella|él|ellos|ellas|el de|la de|los dos|ponla|ponle|ponlo|ponles|quítale|quitale|"
                           r"cámbiale|cambiale|vístela|vistela|vístelo|vistelo)\b", re.IGNORECASE)


def _same_person(summary: str, requests: list[str]) -> str:
    """"ahora con una camisa blanca" tras "ponme un vestido" es la misma
    persona, el usuario: "te he puesto", no "le he puesto" (2026-10-07). Solo
    si la conversacion habla en primera persona y nunca de otro."""
    said = " ".join(requests)
    if not _FIRST_PERSON.search(said) or _THIRD_PERSON.search(said):
        return summary
    return re.sub(r"^le he\b", "te he", summary, flags=re.IGNORECASE)


# Desde donde empieza "lo que no cambia". Tambien ", keeping ...": sin eso el
# "pose" de "keeping his face, pose and framing" contaba como cambio de postura
# y se usaba la de Kontext entera, sin la foto original (2026-10-07). Y todas
# las formas de decirlo: con "leaving his ... pose ... the same" paso lo mismo
# al quitar una camiseta (motas por toda la foto y la cara pegada).
_KEEP = re.compile(r"[\s,]*\b(while keeping|keeping|Keep|leaving|leave|preserving|preserve|retaining|retain|"
                   r"maintaining|maintain|Do not|Don't|without changing)\b.*$", re.IGNORECASE | re.DOTALL)


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


def is_removal(instruction: str) -> bool:
    return bool(_REMOVAL.search(instruction or ""))
_REMOVAL_ES = re.compile(r"\b(quita\w*|borra\w*|elimina\w*|saca\w*)\b", re.IGNORECASE)
# algo de la persona en la peticion: ropa, objetos, postura, "con ...", "vestido de ..."
_PERSON_CHANGE = re.compile(
    r"\b(con|sin|lleva\w*|llevando|vest\w*|viste\w*|ropa|bañador|bikini|traje|camis\w*|jersey|abrigo|"
    r"chaqueta|pantal\w*|falda|vestido|zapat\w*|gafas|gorr[ao]|sombrero|tumbad\w*|sentad\w*|de pie|"
    r"levant\w*|bail\w*|corr\w*|salt\w*|sujet\w*|cogi\w*|tomando|bebiendo|disfraz\w*|disfrazad\w*|"
    r"rodillas|inclinad\w*|agachad\w*|abraz\w*|bes\w*|mano|manos|brazo\w*|cruzad\w*|salud\w*|apoyad\w*|"
    r"postura|pose|posando|mirando|toc\w*)\b",
    re.IGNORECASE)


# Mas que de sobra para una foto (y para Kontext, que trabaja a 1 MP). Una
# de movil de 108 MP pasaba sin aviso y restore_faces/compose_* la llevan a
# float32 varias veces: varios GB de RAM por foto (auditoria 2026-10-05).
MAX_PIXELS = 40_000_000


def load_rgb(image_bytes: bytes) -> np.ndarray:
    """Las fotos del movil vienen giradas en el EXIF: sin exif_transpose la
    edicion sale tumbada. Las enormes se reducen a MAX_PIXELS."""
    img = Image.open(io.BytesIO(image_bytes))
    w, h = img.size
    if w * h > MAX_PIXELS:
        scale = (MAX_PIXELS / (w * h)) ** 0.5
        target = (max(1, int(w * scale)), max(1, int(h * scale)))
        img.draft("RGB", target)  # JPEG: decodifica ya reducida, sin pasar por el tamaño completo
        img = ImageOps.exif_transpose(img).convert("RGB")
        side = max(target)  # el lado largo: vale este girada por el EXIF o no
        img.thumbnail((side, side), Image.LANCZOS)
        return np.array(img)
    return np.array(ImageOps.exif_transpose(img).convert("RGB"))


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


HEAD_TILT_MIN = 6.0  # grados de inclinacion a partir de los que se endereza la cabeza


def head_tilt(face: np.ndarray) -> float:
    """Inclinacion de la cabeza hacia un hombro (grados, por la linea de los ojos)."""
    (lx, ly), (rx, ry) = face[4:6], face[6:8]
    return float(np.degrees(np.arctan2(ry - ly, rx - lx)))


def straighten_heads(rgb: np.ndarray) -> np.ndarray:
    """Para un cambio de postura: la foto que ve Kontext con la cabeza recta.
    Kontext copiaba la inclinacion del selfie aunque se le pidiera la cabeza
    recta, y de pie o en paracaidas quedaba torcida ("la cara esta girada
    rara", Sergio, 2026-10-08). Se gira solo la cabeza, sobre el cuello; lo
    que queda al descubierto se rellena con lo de alrededor. Es solo la
    referencia: Kontext redibuja el cuerpo entero y luego va la cara original,
    girada igual (restore_faces encaja giros en el plano).
    Dos pasadas: con la cara algo girada de lado, una sola dejaba 5 grados."""
    return _straighten_once(_straighten_once(rgb, HEAD_TILT_MIN), 3.0)


def hide_held(original: np.ndarray, reference: np.ndarray) -> np.ndarray:
    """Para un cambio de postura sin pedir el objeto: la referencia de Kontext
    sin lo que se tiene en la mano. Aunque la instruccion no lo nombrara (y
    pidiera las manos vacias), Kontext copiaba la copa de la foto ("no hace
    falta que salga en todas con la copa", Sergio, 2026-10-08). Las manos se
    buscan en la original (en la enderezada a veces no salen); se borra la mano
    y lo de encima (una copa, un movil), sin tocar la cara."""
    h, w = reference.shape[:2]
    mask = np.zeros((h, w), np.uint8)
    for x, y, size in detect_hands(original):
        cv2.circle(mask, (int(x), int(y - 0.8 * size)), int(2.2 * size), 255, -1)
    if not mask.any():
        return reference
    skin = _skin_mask(reference)
    if skin is not None:
        mask[skin > 0] = 0
    small = max(1, int(max(h, w) / 512))
    sw, sh = w // small, h // small
    filled = cv2.inpaint(cv2.resize(reference, (sw, sh), interpolation=cv2.INTER_AREA),
                         cv2.resize(mask, (sw, sh), interpolation=cv2.INTER_NEAREST), 20, cv2.INPAINT_TELEA)
    filled = cv2.resize(filled, (w, h), interpolation=cv2.INTER_LINEAR)
    return np.where(mask[..., None] > 0, filled, reference)


def mentions_held_object(request: str) -> bool:
    return bool(_MENTIONS_OBJECT.search(request or ""))


def _straighten_once(rgb: np.ndarray, min_tilt: float) -> np.ndarray:
    out = rgb.copy()
    h, w = rgb.shape[:2]
    for face in _faces(rgb):
        angle = head_tilt(face)
        if abs(angle) < min_tilt or abs(angle) > 60:
            continue  # recta, o tumbada (eso es la postura, no un selfie)
        x, y, fw, fh = (float(v) for v in face[:4])
        cx = x + fw / 2
        pivot = (cx, y + fh * 1.25)  # la base del cuello
        head = np.zeros((h, w), np.uint8)
        cv2.ellipse(head, (int(cx), int(y + fh * 0.45)), (int(fw * 0.85), int(fh * 1.0)), 0, 0, 360, 255, -1)
        head[int(y + fh * 1.15):] = 0
        # solo la persona: con la elipse entera giraba tambien el fondo de alrededor
        head = (head.astype(np.float32) / 255) * matte(out)
        m = cv2.getRotationMatrix2D(pivot, angle, 1.0)
        turned = cv2.warpAffine(out, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        new_head = cv2.warpAffine(head, m, (w, h), flags=cv2.INTER_LINEAR)
        # lo que deja la cabeza vieja, rellenado con lo de alrededor
        k = max(9, int(fw * 0.08)) | 1
        # (tambien bajo la cabeza nueva, que va encima: si no, el relleno
        # copiaba su piel y quedaba un fantasma de la cabeza al lado)
        hole = cv2.dilate(((head > 0.02) | (new_head > 0.02)).astype(np.uint8) * 255, np.ones((k, k), np.uint8))
        hole[int(pivot[1]):] = 0  # el cuello y el cuerpo se quedan
        if hole.any():
            small = max(1.0, max(h, w) / 1024)
            sw, sh = int(w / small), int(h / small)
            filled = cv2.inpaint(cv2.resize(out, (sw, sh), interpolation=cv2.INTER_AREA),
                                 cv2.resize(hole, (sw, sh), interpolation=cv2.INTER_NEAREST), 15, cv2.INPAINT_TELEA)
            filled = cv2.resize(filled, (w, h), interpolation=cv2.INTER_LINEAR)
            out = np.where(hole[..., None] > 0, filled, out)
        alpha = cv2.GaussianBlur(new_head, (0, 0), max(1.5, fw * 0.01))[..., None]
        out = (turned * alpha + out * (1 - alpha)).clip(0, 255).astype(np.uint8)
    return out


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


def _feature_align(orig: np.ndarray, edited: np.ndarray) -> np.ndarray | None:
    """Giro + zoom + desplazamiento (orig -> edited, como la de _align) con
    los puntos que coinciden en las dos (ORB + RANSAC): lo que cambio no
    tiene puntos parecidos y no cuenta. None si no hay bastantes."""
    f = min(1.0, 1024 / max(orig.shape[:2]))
    g1 = cv2.cvtColor(cv2.resize(orig, None, fx=f, fy=f, interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
    g2 = cv2.cvtColor(cv2.resize(edited, None, fx=f, fy=f, interpolation=cv2.INTER_AREA), cv2.COLOR_RGB2GRAY)
    orb = cv2.ORB_create(4000)
    k1, d1 = orb.detectAndCompute(g1, None)
    k2, d2 = orb.detectAndCompute(g2, None)
    if d1 is None or d2 is None or len(k1) < 20 or len(k2) < 20:
        return None
    matches = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True).match(d1, d2)
    if len(matches) < 20:
        return None
    p1 = np.float32([k1[m.queryIdx].pt for m in matches])
    p2 = np.float32([k2[m.trainIdx].pt for m in matches])
    m, inliers = cv2.estimateAffinePartial2D(p1, p2, method=cv2.RANSAC, ransacReprojThreshold=3.0)
    if m is None or inliers is None or inliers.sum() < 15:
        return None
    m[:, 2] /= f
    return m.astype(np.float32)


def _unchanged(orig: np.ndarray, edited: np.ndarray) -> np.ndarray | None:
    """Mascara (uint8) de lo que se parece en las dos sin moverlas, a grandes
    rasgos (desenfocado: Kontext desplaza unos pixeles). None si casi todo
    cambio: entonces no hay en que apoyarse."""
    f = min(1.0, 512 / max(orig.shape[:2]))
    a = cv2.resize(orig, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
    b = cv2.resize(edited, (a.shape[1], a.shape[0]), interpolation=cv2.INTER_AREA)
    lab_a = cv2.cvtColor(cv2.GaussianBlur(a, (0, 0), 3), cv2.COLOR_RGB2LAB).astype(np.float32)
    lab_b = cv2.cvtColor(cv2.GaussianBlur(b, (0, 0), 3), cv2.COLOR_RGB2LAB).astype(np.float32)
    same = (np.linalg.norm(lab_a - lab_b, axis=2) < _WEAK_CHANGE).astype(np.uint8)
    same = cv2.erode(same, np.ones((5, 5), np.uint8))
    if same.mean() < 0.2:
        return None
    return cv2.resize(same * 255, (orig.shape[1], orig.shape[0]), interpolation=cv2.INTER_NEAREST)


def compose_local(orig: np.ndarray, edited: np.ndarray) -> np.ndarray:
    """`edited` ya a la resolucion de `orig`."""
    h, w = orig.shape[:2]
    cc, warp = _align(orig, edited, None, np.eye(2, 3))
    first = cc
    if cc < _MIN_ALIGN_CC:
        # un cambio grande (jersey negro -> camisa blanca en media foto) hunde la
        # correlacion aunque el encuadre sea el mismo, y se tiraba la original
        # entera: fondo y cara de Kontext (cc 0,30 con la foto de Sergio,
        # 2026-10-07). Se alinea otra vez solo con lo que no cambio, partiendo
        # de los puntos que coinciden (aguanta que Kontext amplie la foto).
        init = _feature_align(orig, edited)
        moved = edited if init is None else cv2.warpAffine(
            edited, init, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderMode=cv2.BORDER_REPLICATE)
        keep = _unchanged(orig, moved)
        if keep is not None:
            cc, warp = _align(orig, edited, keep, np.eye(2, 3) if init is None else init)
    log.info("Componer: alineacion %.2f (sin mascara %.2f)", cc, first)
    if cc < _MIN_ALIGN_CC:
        return edited
    flags = cv2.INTER_LANCZOS4 | cv2.WARP_INVERSE_MAP
    ed = cv2.warpAffine(edited, warp, (w, h), flags=flags, borderMode=cv2.BORDER_REPLICATE)
    rigid = ed
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
    log.info("Componer: cambiado %.0f%% de la foto", 100 * area)
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
    # Dentro de lo nuevo (la camisa) va Kontext sin el flujo: el flujo intenta
    # encajarlo en la forma de lo viejo (el jersey) y doblaba la tira de
    # botones y ondulaba los cuadros (2026-10-07). El flujo solo cerca del
    # borde, para que empalme con la original.
    core = cv2.GaussianBlur(cv2.erode(changed, np.ones((u * 4, u * 4), np.uint8)).astype(np.float32), (0, 0), u * 2)
    ed = (rigid * core[..., None] + ed * (1 - core[..., None])).astype(np.uint8)
    # Si Kontext amplio la foto, en el borde queda una franja que no genero:
    # alli seguia la ropa vieja (una manga gris junto a la camisa nueva). Si
    # lo nuevo llega a esa franja, mejor recortarla.
    crop = _valid_crop(valid, changed, (h, w))
    if crop is None:
        changed = changed * cv2.erode(valid.astype(np.uint8), np.ones((u, u), np.uint8))
    alpha = cv2.GaussianBlur(changed.astype(np.float32), (0, 0), u)[..., None]
    out = (orig * (1 - alpha) + ed * alpha).clip(0, 255).astype(np.uint8)
    if crop is not None:
        y0, y1, x0, x1 = crop
        out = out[y0:y1, x0:x1]
    return out


def _valid_crop(valid: np.ndarray, changed: np.ndarray, shape: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """(y0, y1, x0, x1): lo mas grande que genero Kontext, con la proporcion
    de la foto, si lo cambiado llega a la franja del borde que no genero.
    None si no hace falta o si habria que recortar demasiado (>12% por lado)."""
    h, w = shape
    missing = ~valid
    if not missing.any() or (changed.astype(bool) & missing).sum() < 0.002 * h * w:
        return None
    # con un zoom lo que falta es un marco por los cuatro lados: el rectangulo
    # de dentro, medido desde el centro y encogido hasta que todo sea valido
    rows = np.nonzero(valid[:, w // 2])[0]
    cols = np.nonzero(valid[h // 2, :])[0]
    if not len(rows) or not len(cols):
        return None
    y0, y1, x0, x1 = int(rows[0]), int(rows[-1]) + 1, int(cols[0]), int(cols[-1]) + 1
    while (y1 - y0) >= 0.76 * h and (x1 - x0) >= 0.76 * w and not valid[y0:y1, x0:x1].all():
        dy, dx = max(1, h // 200), max(1, w // 200)
        y0, y1, x0, x1 = y0 + dy, y1 - dy, x0 + dx, x1 - dx
    if (y1 - y0) < 0.76 * h or (x1 - x0) < 0.76 * w:
        return None
    # la proporcion de la foto, centrado en lo valido
    ch, cw = y1 - y0, x1 - x0
    if cw / ch > w / h:
        nw = int(round(ch * w / h))
        x0 += (cw - nw) // 2
        x1 = x0 + nw
    else:
        nh = int(round(cw * h / w))
        y0 += (ch - nh) // 2
        y1 = y0 + nh
    return int(y0), int(y1), int(x0), int(x1)


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
    # Es luz de baja frecuencia: se calcula reducida y se amplia. A tamaño
    # completo, en una foto de 12 MP, el desenfoque de 165 px tardaba 30 s
    # (2026-10-07).
    h, w = people.shape[:2]
    f = min(1.0, 512 / min(h, w))
    size = (max(1, round(w * f)), max(1, round(h * f)))
    small_mask = cv2.resize(mask.astype(np.float32), size, interpolation=cv2.INTER_AREA)
    sigma = 0.06 * min(size[1], size[0])
    weight = cv2.GaussianBlur(small_mask, (0, 0), sigma)[..., None] + 1e-3

    def low(img):
        small = cv2.resize(img.astype(np.float32), size, interpolation=cv2.INTER_AREA)
        return cv2.GaussianBlur(small * small_mask[..., None], (0, 0), sigma) / weight

    gain = np.clip((low(edited) + 8) / (low(people) + 8), 0.3, 3.0)
    if f < 1.0:
        gain = cv2.resize(gain, (w, h), interpolation=cv2.INTER_LINEAR)
    return people.astype(np.float32) * gain


# Peticiones que cambian la cara a proposito: ahi no se vuelve a poner la original
_FACE_EDIT = re.compile(
    r"\b(gafas|lentes|cara|rostro|ojos|boca|nariz|labios|barba|bigote|afeitad\w*|maquilla\w*|sonri\w*|serio|"
    r"seria|triste|enfadad\w*|expresion|expresión|gesto|guiñ\w*|ri[eé]nd\w*|re[ií]r\w*|r[ií]e\w*|"
    r"carcajada\w*|llor\w*|grit\w*|sorprend\w*|asustad\w*|boca abierta|ojos cerrados|bostez\w*|joven|mayor|viej\w*|envejec\w*|rejuvenec\w*|"
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
    return face_detect.detect(rgb, 0.7)  # imagen rara o muy pequeña: sin caras, la edicion sigue


def _background_fill(source: np.ndarray, out: np.ndarray, hole: np.ndarray, k: int) -> np.ndarray | None:
    """El fondo de la original para tapar el pelo sobrante de Kontext, si
    alrededor del hueco la original y el resultado tienen el mismo fondo (se
    cambio la ropa, no el sitio). El relleno inventado (inpaint) dejaba un
    borron en las hojas de detras de la cabeza que delataba el recorte
    (Sergio, foto real, 2026-10-07). None si el fondo es otro."""
    ring = cv2.dilate(hole, np.ones((3 * k, 3 * k), np.uint8)).astype(bool) & ~hole.astype(bool)
    if ring.sum() < 50:
        return None
    if source.shape != out.shape:
        return None
    lab_w = cv2.cvtColor(source, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab_o = cv2.cvtColor(out.clip(0, 255).astype(np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)
    # la mayoria del anillo debe coincidir (alli puede asomar algo de pelo)
    if np.median(np.linalg.norm(lab_w[ring] - lab_o[ring], axis=1)) > 12:
        return None
    return source.astype(np.float32)


def face_angle_changed(faces_o: list, faces_r: list, tolerance: float = 0.05) -> bool:
    """¿Alguna cara tiene otro angulo que en la original? Se mira si sus 5
    puntos (ojos, nariz, comisuras) encajan con los originales moviendolos,
    girandolos y escalandolos sin mas: con otro giro de cabeza no encajan.
    Caras emparejadas de izquierda a derecha."""
    return bool(turned_faces(faces_o, faces_r, tolerance)[0]) or len(faces_o) != len(faces_r)


def turned_faces(faces_o: list, faces_r: list, tolerance: float = 0.05) -> tuple[list, list]:
    """Las parejas (original, resultado) cuya cabeza esta girada de otra forma:
    a esas no se les puede pegar la cara original (no encaja) y se les clonan
    los rasgos; a las demas, la cara original tal cual."""
    order_o = sorted(faces_o, key=lambda f: f[0] + f[2] / 2)
    order_r = sorted(faces_r, key=lambda f: f[0] + f[2] / 2)
    turned_o, turned_r = [], []
    for fo, fr in zip(order_o, order_r):
        po = fo[4:14].reshape(5, 2).astype(np.float32)
        pr = fr[4:14].reshape(5, 2).astype(np.float32)
        m, _ = cv2.estimateAffinePartial2D(po, pr)
        if m is None or (np.linalg.norm(po @ m[:, :2].T + m[:, 2] - pr, axis=1).mean()
                         / max(float(fr[2]), 1.0)) > tolerance:
            turned_o.append(fo)
            turned_r.append(fr)
    return turned_o, turned_r


def _skin_mask(rgb: np.ndarray) -> np.ndarray | None:
    """Cara (con cuello y barba), pelo y gafas segun el segmentador, con los
    huecos de dentro rellenos (el bigote salia a veces como otra cosa); lo que
    se abre hacia fuera, como el borde de una copa delante, se queda fuera.
    None si no esta el modelo."""
    labels = parse_people(rgb)
    if labels is None:
        return None
    keep = np.isin(labels, [FACE, HAIR, SUNGLASSES]).astype(np.uint8)
    outside = np.pad(1 - keep, 1, constant_values=1)
    cv2.floodFill(outside, None, (0, 0), 2)
    return ((keep > 0) | (outside[1:-1, 1:-1] == 1)).astype(np.float32)


def restore_faces(orig: np.ndarray, result: np.ndarray, keep_hair: bool = True,
                  face_only: bool = False, seams: list | None = None) -> np.ndarray:
    """Vuelve a poner cada cara ORIGINAL encima de la del resultado, alineada
    por ojos, nariz y boca y con la luz del sitio nuevo. Sin esto, al cambiar
    mucha ropa (jersey -> bañador) el paso "local" se quedaba entero con lo de
    Kontext, cara redibujada incluida: parecida, no la misma (similitud 0,69
    en la prueba del 2026-10-05). Si una cara cambio de postura (no encaja),
    se deja la de Kontext.
    face_only (cambios de postura): solo de las cejas a la barbilla, con borde
    suave; el pelo y el contorno de la cabeza son los de Kontext. Con la cabeza
    entera, la original (recta, con su pelo) quedaba "pegada encima" de una
    persona tumbada, con halo alrededor (Sergio, 2026-10-06)."""
    faces_o, faces_r = _faces(orig), _faces(result)
    if not faces_o or not faces_r:
        return result
    skin = _skin_mask(orig) if face_only else None
    h, w = result.shape[:2]
    out = result.astype(np.float32)
    used = set()
    # Mismas personas antes y despues: se emparejan por orden de izquierda a
    # derecha, que no cambia al abrazarse, bailar o cambiar de postura (y la
    # cabeza si se mueve). Con una sola persona, eso es "la de siempre".
    same_people = len(faces_o) == len(faces_r)
    order_o = sorted(range(len(faces_o)), key=lambda i: faces_o[i][0] + faces_o[i][2] / 2)
    order_r = sorted(range(len(faces_r)), key=lambda i: faces_r[i][0] + faces_r[i][2] / 2)
    partner = {o: r for o, r in zip(order_o, order_r)} if same_people else {}
    for i, f in enumerate(faces_o):
        pts_o = f[4:14].reshape(5, 2).astype(np.float32)
        best = None
        for j, g in enumerate(faces_r):
            if j in used or (same_people and j != partner[i]):
                continue
            pts_r = g[4:14].reshape(5, 2).astype(np.float32)
            # si no son las mismas personas, la cara va donde sigue estando esa
            # persona: dos caras siempre "encajan" de forma y en un grupo se podia
            # pegar la cara de una sobre otra (2026-10-06)
            moved = np.linalg.norm(pts_o.mean(axis=0) - pts_r.mean(axis=0)) / max(float(f[2]), 1.0)
            if moved > 0.6 and not same_people:
                continue
            m, _ = cv2.estimateAffinePartial2D(pts_o, pts_r)
            if m is None:
                continue
            err = np.linalg.norm(pts_o @ m[:, :2].T + m[:, 2] - pts_r, axis=1).mean() / max(float(g[2]), 1.0)
            score = err + 0.05 * moved
            if best is None or score < best[0]:
                best = (score, j, m, err)
        # en un cambio de postura, solo si la cara esta casi de frente como la
        # original: girada en 3D no encaja y se notaba pegada
        if best is None or best[3] > (0.05 if face_only else 0.08):
            continue
        _, j, m, _ = best
        used.add(j)
        warped = cv2.warpAffine(orig, m, (w, h), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REPLICATE)
        x, y, fw, fh = (float(v) for v in f[:4])
        scale = float(np.sqrt(abs(np.linalg.det(m[:, :2]))))
        # la cabeza entera (pelo, orejas, barba): solo los rasgos con el pelo y
        # el contorno de Kontext seguia sin ser la misma persona
        zone = np.zeros(orig.shape[:2], np.float32)
        cx = int(x + fw / 2)
        if face_only:
            keep_hair = False
        if keep_hair:
            cv2.ellipse(zone, (cx, int(y + fh * 0.35)), (int(fw * 0.85), int(fh * 0.95)), 0, 0, 360, 1, -1)
            # de la boca para abajo solo la mandibula: a los lados ya asoma la
            # ropa vieja (el cuello del jersey en la playa)
            zone[int(y + fh * 0.75):] = 0
        else:
            # sombrero, gorra...: de las cejas para abajo, el resto es de Kontext
            cv2.ellipse(zone, (cx, int(y + fh * 0.5)), (int(fw * 0.47), int(fh * 0.38)), 0, 0, 360, 1, -1)
        # la mandibula y la barbilla enteras, con margen: Kontext dibuja la cara
        # algo mas delgada y, con una zona mas justa, por los lados asomaba su
        # mandibula y cambiaba la forma de la cara (Sergio, 2026-10-07). La
        # union queda en el cuello y la disimula el repintado (_seam_band).
        cv2.ellipse(zone, (cx, int(y + fh * 0.6)), (int(fw * 0.55), int(fh * 0.47)), 0, 0, 360, 1, -1)
        if not keep_hair:
            zone[:int(y + fh * 0.15)] = 0
        zone_w = cv2.warpAffine(zone, m, (w, h), flags=cv2.INTER_LINEAR)
        zone_w = cv2.GaussianBlur(zone_w, (0, 0), max(2.0, fw * scale * (0.09 if face_only else 0.05)))
        if face_only and skin is not None:
            # solo piel, barba y pelo de la original: lo que tuviera delante
            # (la copa bajo la barbilla) se quedaba como un arco fantasma en el
            # cuello nuevo (Sergio, 2026-10-08)
            skin_w = cv2.warpAffine(skin, m, (w, h), flags=cv2.INTER_LINEAR)
            zone_w *= cv2.GaussianBlur(skin_w, (0, 0), max(1.5, fw * scale * 0.015))
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
            # la original tal cual (en un cambio de ropa el resultado ya esta en su
            # encuadre) o movida como la cara
            filled = _background_fill(orig, out, hole, k)
            if filled is None:
                filled = _background_fill(warped, out, hole, k)
            if filled is None:
                filled = cv2.inpaint(out.clip(0, 255).astype(np.uint8), hole, 9, cv2.INPAINT_TELEA).astype(np.float32)
            out = np.where(grown[..., None] > 0, filled, out)
        lit = _relight(warped, result, (head > 0.5).astype(np.float32))
        out = _match_skin(out, result, lit, head, faces_r[j])
        out = _multiband(lit, out, head, fw * scale)
        if seams is not None:
            protected = (head > 0.05).astype(np.float32)
            seams.append((_seam_band(protected, faces_r[j], fw * scale), faces_r[j], protected))
    return out.clip(0, 255).astype(np.uint8)


# 0,3-0,6 con la referencia no cambiaban nada; sin ella, 0,5 aun dejaba el
# borde y 0,7 ya era continuo (foto de Sergio, 2026-10-07). Solo se repinta
# por FUERA de la cara (ver _seam_band), asi que la cara no cambia nunca.
SEAM_DENOISE = 0.65
SEAM_PROMPT = ("Make the skin, hair and lighting look natural and seamless where the face meets the neck and "
               "the hair, as in one single real photograph. Keep the face, facial features, expression, hair, "
               "clothes and background exactly the same.")


def _seam_band(protected: np.ndarray, face: np.ndarray, face_w: float) -> np.ndarray:
    """La franja de FUERA de la cabeza pegada que la toca, de los ojos para
    abajo (el cuello bajo la barbilla, el pelo a los lados): lo unico que se
    repinta al final (seam_crop). Nada de la cara original, ni su contorno:
    con la franja a los dos lados del borde la IA redibujaba la mandibula y
    le cambiaba la forma de la cara (Sergio, 2026-10-07)."""
    inside = (protected > 0.5).astype(np.uint8)
    k = max(5, int(face_w * 0.12)) | 1
    band = cv2.dilate(inside, np.ones((k, k), np.uint8)) - inside
    x, y, fw, fh = (float(v) for v in face[:4])
    band[:int(max(0, y + fh * 0.5))] = 0
    return band.astype(np.float32)


def seam_crop(img: np.ndarray, seams: list) -> tuple[tuple[int, int, int, int], bytes, bytes] | None:
    """El trozo de la foto alrededor de la union (y su mascara), al tamaño de
    Kontext, para repintar la franja. None si no hay union que repintar."""
    if not seams:
        return None
    band = np.maximum.reduce([b for b, _, _ in seams])
    if band.sum() < 50:
        return None
    ys, xs = np.nonzero(band)
    fw = max(float(f[2]) for _, f, _ in seams)
    h, w = band.shape
    pad = int(fw * 0.6)
    y0, y1 = max(0, ys.min() - pad), min(h, ys.max() + pad + 1)
    x0, x1 = max(0, xs.min() - pad), min(w, xs.max() + pad + 1)
    kw, kh = kontext_size(x1 - x0, y1 - y0)
    crop = cv2.resize(img[y0:y1, x0:x1], (kw, kh), interpolation=cv2.INTER_LANCZOS4)
    mask = cv2.resize((band[y0:y1, x0:x1] * 255).astype(np.uint8), (kw, kh), interpolation=cv2.INTER_LINEAR)
    mask = cv2.dilate(mask, np.ones((9, 9), np.uint8))  # el latente va de 8 en 8 px
    return (int(y0), int(y1), int(x0), int(x1)), to_png(crop), to_png(np.dstack([mask] * 3))


def paste_seam(img: np.ndarray, refined_png: bytes, box: tuple[int, int, int, int], seams: list) -> np.ndarray:
    """Vuelve a poner SOLO la franja repintada, con borde suave: el resto de
    la foto se queda exactamente como estaba."""
    y0, y1, x0, x1 = box
    refined = cv2.resize(load_rgb(refined_png), (x1 - x0, y1 - y0), interpolation=cv2.INTER_LANCZOS4)
    band = np.maximum.reduce([b for b, _, _ in seams])[y0:y1, x0:x1]
    protected = np.maximum.reduce([p for _, _, p in seams])[y0:y1, x0:x1]
    fw = max(float(f[2]) for _, f, _ in seams)
    alpha = cv2.GaussianBlur(band, (0, 0), max(1.5, fw * 0.025))
    alpha = np.clip(alpha * 1.5, 0, 1)
    # la cara pegada no se toca: ni un pixel (ni por el borde suave)
    alpha = (alpha * (protected < 0.02))[..., None]
    out = img.copy()
    region = img[y0:y1, x0:x1].astype(np.float32)
    # el ida y vuelta por Kontext puede mover un poco el color: se ajusta al de
    # alrededor para que la franja no se distinga por eso
    ring = (alpha[..., 0] > 0.05) & (alpha[..., 0] < 0.5)
    if ring.sum() > 50:
        refined = refined.astype(np.float32) + (region[ring].mean(axis=0) - refined[ring].mean(axis=0))
    out[y0:y1, x0:x1] = (refined * alpha + region * (1 - alpha)).clip(0, 255).astype(np.uint8)
    return out


def _cheeks(img_lab: np.ndarray, face: np.ndarray) -> np.ndarray:
    """Piel de la cara (Lab, mediana): entre los ojos y la boca, sin el centro
    (nariz)."""
    x, y, fw, fh = (float(v) for v in face[:4])
    h, w = img_lab.shape[:2]
    pts = []
    for cx in (x + fw * 0.27, x + fw * 0.73):
        y0, y1 = int(max(0, y + fh * 0.48)), int(min(h, y + fh * 0.66))
        x0, x1 = int(max(0, cx - fw * 0.1)), int(min(w, cx + fw * 0.1))
        if y1 > y0 and x1 > x0:
            pts.append(img_lab[y0:y1, x0:x1].reshape(-1, 3))
    return np.median(np.concatenate(pts), axis=0) if pts else np.zeros(3, np.float32)


def _chin(img_lab: np.ndarray, face: np.ndarray) -> np.ndarray | None:
    x, y, fw, fh = (float(v) for v in face[:4])
    h, w = img_lab.shape[:2]
    y0, y1 = int(max(0, y + fh * 0.86)), int(min(h, y + fh * 0.97))
    x0, x1 = int(max(0, x + fw * 0.35)), int(min(w, x + fw * 0.65))
    if y1 <= y0 or x1 <= x0:
        return None
    return np.median(img_lab[y0:y1, x0:x1].reshape(-1, 3), axis=0)


def _match_skin(out: np.ndarray, result: np.ndarray, lit: np.ndarray, head: np.ndarray,
                face: np.ndarray) -> np.ndarray:
    """La piel nueva de Kontext (cuello, escote, brazos) con el tono de la piel
    de verdad: se le aplica el mismo cambio de color que hay entre la cara de
    Kontext y la original. Kontext la hacia mas palida y al volver a poner la
    cara quedaba una franja clara bajo la barbilla, el "corte del cuello"
    (Sergio, 2026-10-07)."""
    lab_r = cv2.cvtColor(result, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab_l = cv2.cvtColor(lit.clip(0, 255).astype(np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)
    x, y, fw, fh = (float(v) for v in face[:4])
    person = matte(result) > 0.5
    # El tono se mide justo en la union: la piel original por DENTRO del borde
    # (mandibula y barbilla) y la de Kontext por FUERA (el cuello). Comparando
    # con su cara, el escote de Kontext (de otro tono que su cara) apenas se
    # corregia y de lejos la cara se veia de otro color que el cuerpo, como
    # una mascara (Sergio, 2026-10-07).
    k = max(3, int(fw * 0.08)) | 1
    inside = (head > 0.5).astype(np.uint8)
    touched_head = (head > 0.05).astype(np.uint8)
    low = np.zeros(head.shape, bool)
    low[int(min(head.shape[0], max(0, y + fh * 0.75))):] = True
    inner = (inside - cv2.erode(inside, np.ones((k, k), np.uint8))).astype(bool) & low
    outer = (cv2.dilate(touched_head, np.ones((k, k), np.uint8)) - touched_head).astype(bool) & low & person
    cheek_l, cheek_r = _cheeks(lab_l, face), _cheeks(lab_r, face)
    # solo piel (no el pelo, ni el cuello negro del jersey original)
    inner &= np.linalg.norm(lab_l[..., 1:] - cheek_l[1:], axis=2) < 15
    outer &= np.linalg.norm(lab_r[..., 1:] - cheek_r[1:], axis=2) < 20
    if inner.sum() >= 30 and outer.sum() >= 30:
        neck_k = np.median(lab_r[outer], axis=0)
        shift = np.median(lab_l[inner], axis=0) - neck_k
    else:  # sin piel a los dos lados (cuello tapado...): la de las caras
        neck_k = cheek_r
        shift = cheek_l - cheek_r
    shift = np.clip(shift, [-30, -15, -15], [30, 15, 15])
    if np.abs(shift).max() < 2:
        return out
    lab_o = cv2.cvtColor(out.clip(0, 255).astype(np.uint8), cv2.COLOR_RGB2LAB).astype(np.float32)
    # piel de Kontext: color parecido al de su cuello (la ropa, el pelo y el
    # fondo no se tocan)
    chroma = np.linalg.norm(lab_r[..., 1:] - neck_k[1:], axis=2)
    light = np.abs(lab_r[..., 0] - neck_k[0])
    weight = np.clip(1.5 - chroma / 8, 0, 1) * np.clip(1.5 - light / 40, 0, 1)
    weight[:int(max(0, y + fh * 0.5))] = 0  # de la boca para abajo: cuello, escote, brazos
    weight *= person  # solo la persona: no la mesa o una pared de color piel
    weight = cv2.GaussianBlur(weight.astype(np.float32), (0, 0), max(1.5, fw * 0.02))
    if weight.max() < 0.05:
        return out
    lab_o = lab_o + weight[..., None] * shift
    # y la textura: la piel de Kontext es lisa (y ampliada, algo borrosa) y la
    # de la cara original tiene poros y pecas; junto a ella parecia de plastico.
    # Se le añade el detalle fino que le falta, con la fuerza del de la cara.
    s = max(0.8, fw * 0.004)

    def detail(lab_l):
        return lab_l - cv2.GaussianBlur(lab_l, (0, 0), s)

    have_mask = weight > 0.6
    if have_mask.sum() > 100:
        target = _cheek_std(detail(lab_l[..., 0]), face)
        have = float(np.std(detail(lab_o[..., 0])[have_mask]))
        # la mitad como mucho: con toda, el escote parecia papel de lija
        need = min(np.sqrt(max(0.0, target ** 2 - have ** 2)), 0.5 * target)
        if need > 0.3:
            rng = np.random.default_rng(1)
            noise = cv2.GaussianBlur(rng.normal(0, 1, weight.shape).astype(np.float32), (0, 0), s)
            noise = detail(noise)
            noise *= need / max(float(noise.std()), 1e-6)
            lab_o[..., 0] += noise * weight
    new = cv2.cvtColor(lab_o.clip(0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB).astype(np.float32)
    # solo donde hay piel: el ida y vuelta a Lab cambia unos niveles el resto
    touched = np.clip(weight * 20, 0, 1)[..., None]
    return new * touched + out.astype(np.float32) * (1 - touched)


def _cheek_std(img: np.ndarray, face: np.ndarray) -> float:
    x, y, fw, fh = (float(v) for v in face[:4])
    h, w = img.shape[:2]
    vals = []
    for cx in (x + fw * 0.27, x + fw * 0.73):
        y0, y1 = int(max(0, y + fh * 0.48)), int(min(h, y + fh * 0.66))
        x0, x1 = int(max(0, cx - fw * 0.1)), int(min(w, cx + fw * 0.1))
        if y1 > y0 and x1 > x0:
            vals.append(img[y0:y1, x0:x1].ravel())
    return float(np.std(np.concatenate(vals))) if vals else 0.0


def _multiband(top: np.ndarray, base: np.ndarray, alpha: np.ndarray, face_w: float) -> np.ndarray:
    """`top` sobre `base` con mezcla por bandas (piramide de Laplace): el tono
    y la luz se funden en una franja ancha y el detalle (poros, pelo) en una
    estrecha. Con una sola mezcla suave se notaba el escalon de color en el
    cuello entre la cara original y el cuello de Kontext (Sergio, 2026-10-07).
    Solo en el recuadro de la cara, para que no tarde en fotos de 12 MP."""
    levels = int(np.clip(np.log2(max(face_w, 16) / 6), 2, 6))
    ys, xs = np.nonzero(alpha > 0.002)
    if not len(ys):
        return base
    h, w = alpha.shape
    pad = int(face_w * 0.5)
    y0, y1 = max(0, ys.min() - pad), min(h, ys.max() + pad + 1)
    x0, x1 = max(0, xs.min() - pad), min(w, xs.max() + pad + 1)
    step = 2 ** levels
    # tamaño multiplo de 2^niveles, repitiendo el borde
    ph, pw = -(-(y1 - y0) // step) * step, -(-(x1 - x0) // step) * step

    def prep(img):
        crop = img[y0:y1, x0:x1].astype(np.float32)
        return cv2.copyMakeBorder(crop, 0, ph - crop.shape[0], 0, pw - crop.shape[1], cv2.BORDER_REPLICATE)

    a, b, m = prep(top), prep(base), prep(alpha)
    ga, gb, gm = [a], [b], [m]
    for _ in range(levels):
        ga.append(cv2.pyrDown(ga[-1]))
        gb.append(cv2.pyrDown(gb[-1]))
        gm.append(cv2.pyrDown(gm[-1]))
    blended = ga[-1] * gm[-1][..., None] + gb[-1] * (1 - gm[-1][..., None])
    for i in range(levels - 1, -1, -1):
        size = (ga[i].shape[1], ga[i].shape[0])
        la = ga[i] - cv2.pyrUp(ga[i + 1], dstsize=size)
        lb = gb[i] - cv2.pyrUp(gb[i + 1], dstsize=size)
        mi = gm[i][..., None]
        blended = cv2.pyrUp(blended, dstsize=size) + la * mi + lb * (1 - mi)
    out = base.astype(np.float32).copy()
    region = blended[:y1 - y0, :x1 - x0]
    # fuera de la zona (alpha 0 y lejos) se queda base exacta
    reach = cv2.GaussianBlur((alpha[y0:y1, x0:x1] > 0.002).astype(np.float32), (0, 0), max(2.0, face_w * 0.15))
    reach = np.clip(reach * 4, 0, 1)[..., None]
    out[y0:y1, x0:x1] = region * reach + out[y0:y1, x0:x1] * (1 - reach)
    return out


def is_clothes_only(step, request: str) -> bool:
    """El paso solo cambia ropa del cuerpo (poner, cambiar o quitar una
    prenda): ni postura, ni fondo, ni la cara, ni algo en la cabeza
    (sombrero, gafas...)."""
    change = _KEEP.sub("", step.instruction or "")
    return (step.mode == "local" and bool(_CLOTHES_EN.search(change)) and not is_pose_change(step.instruction)
            and wants_face_kept(request) and wants_hair_kept(request)
            and not re.search(r"\b(background|scene|sky|beach|place|location|people|person)\b", change,
                              re.IGNORECASE))


# Reconocer la ropa y las partes del cuerpo, pixel a pixel (SegFormer B2
# "clothes", entrenado con el conjunto ATR; licencia "other": uso personal
# sin problema, revisar antes de repartirlo en un instalador). Adivinar la
# ropa con figuras alrededor de la cara no valia para cualquier foto: en un
# primer plano se quedaba medio hombro sin cambiar (Sergio, 2026-10-07).
PARSE_MODEL = MODELS_DIR / "img" / "parsing" / "segformer_b2_clothes.onnx"
_parse_session = None
# etiquetas del modelo
BACKGROUND, HAT, HAIR, SUNGLASSES, UPPER, SKIRT, PANTS, DRESS, BELT = range(9)
LEFT_SHOE, RIGHT_SHOE, FACE, LEFT_LEG, RIGHT_LEG, LEFT_ARM, RIGHT_ARM, BAG, SCARF = range(9, 18)
_UPPER_PARTS = {UPPER, SCARF, LEFT_ARM, RIGHT_ARM}
_LOWER_PARTS = {SKIRT, PANTS, BELT, LEFT_LEG, RIGHT_LEG}
_WHOLE_PARTS = _UPPER_PARTS | _LOWER_PARTS | {DRESS}
_UPPER_WORDS = re.compile(
    r"\b(shirt|t-shirt|blouse|sweater|jumper|hoodie|sweatshirt|jacket|coat|blazer|cardigan|vest|top|tank top|"
    r"polo|turtleneck|bra|bikini top)(?:e?s)?\b", re.IGNORECASE)  # tambien en plural: "coats"
_LOWER_WORDS = re.compile(r"\b(pants|trousers|jeans|shorts|skirt|leggings|bikini bottom|belt)(?:e?s)?\b",
                          re.IGNORECASE)
_WHOLE_WORDS = re.compile(
    r"\b(dress|gown|jumpsuit|suit|tuxedo|swimsuit|outfit|clothes|clothing|uniform|bikini|pajamas|robe|"
    r"overalls|dungarees)(?:e?s)?\b", re.IGNORECASE)


def parse_people(rgb: np.ndarray) -> np.ndarray | None:
    """Que es cada pixel (etiquetas de arriba), al tamaño de la foto. None si
    no esta el modelo."""
    global _parse_session
    if not PARSE_MODEL.exists():
        return None
    if _parse_session is None:
        _parse_session = ort.InferenceSession(str(PARSE_MODEL), providers=["CPUExecutionProvider"])
    h, w = rgb.shape[:2]
    x = cv2.resize(rgb, (512, 512), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
    logits = _parse_session.run(None, {_parse_session.get_inputs()[0].name: x.transpose(2, 0, 1)[None]})[0][0]
    # las probabilidades a tamaño completo antes de elegir: bordes suaves y bien puestos
    up = np.stack([cv2.resize(c, (w, h), interpolation=cv2.INTER_LINEAR) for c in logits])
    return up.argmax(axis=0).astype(np.uint8)


def _parts_to_change(instruction: str) -> set[int]:
    """Que partes cambian segun la prenda pedida: arriba (y brazos, por si
    cambia la manga), abajo (y piernas) o todo."""
    change = _KEEP.sub("", instruction or "")
    # "Dress both people in coats": ahi "dress" es vestir, no un vestido
    change = re.sub(r"\bdress(es|ed|ing)?\s+(him|her|them|both|the|all|everyone|each)\b", " ", change,
                    flags=re.IGNORECASE)
    whole = bool(_WHOLE_WORDS.search(change))
    upper, lower = bool(_UPPER_WORDS.search(change)), bool(_LOWER_WORDS.search(change))
    if whole or (upper and lower) or not (upper or lower):
        return _WHOLE_PARTS
    return _UPPER_PARTS if upper else _LOWER_PARTS


def clothes_mask(rgb: np.ndarray, instruction: str = "") -> np.ndarray | None:
    """Donde puede ir la ropa nueva (0..1): la prenda que se cambia, con
    margen por si la nueva es mas ancha o tiene otro escote. La cara, el
    pelo, las gafas y el fondo quedan FUERA: Kontext genera solo eso y la
    cabeza real no se toca. Con el modelo de ropa (parse_people); sin el, o
    si no reconoce ninguna prenda, a ojo alrededor de la cara."""
    labels = parse_people(rgb)
    if labels is not None:
        h, w = rgb.shape[:2]
        change = np.isin(labels, list(_parts_to_change(instruction))).astype(np.uint8)
        if change.mean() > 0.005:
            k = max(3, int(min(h, w) * 0.03)) | 1
            mask = cv2.dilate(change, np.ones((k, k), np.uint8))
            # la cara y el pelo, encogidos un poco por el borde: en el limite el
            # modelo se lleva unos pixeles de la prenda vieja (el borde del cuello
            # de la camiseta) y quedaban como un hilo sobre la piel nueva
            keep = np.isin(labels, [FACE, HAIR, SUNGLASSES, HAT, BAG]).astype(np.uint8)
            # el modelo llama "cara" tambien al cuello: protegido, su borde era
            # el del cuello de la camiseta vieja y quedaba una linea como un
            # collar sobre el pecho nuevo (2026-10-07). La cara, hasta la barbilla.
            faces = _faces(rgb)
            if faces:
                below_chin = np.ones((h, w), bool)
                for f in faces:
                    x, y, fw, fh = (float(v) for v in f[:4])
                    below_chin[:int(y + fh * 0.98), int(max(0, x - fw * 0.3)):int(min(w, x + fw * 1.3))] = False
                keep[(labels == FACE) & below_chin] = 0
            e = max(3, int(min(h, w) * 0.008)) | 1
            mask[cv2.erode(keep, np.ones((e, e), np.uint8)) > 0] = 0
            # brazos y manos: nunca, ni por el margen. Al cambiar una camisa se
            # redibujaba la mano con el vaso (Sergio, 2026-10-07). Solo con
            # manga larga entra el brazo pegado a la prenda, sin la punta.
            arms = np.isin(labels, [LEFT_ARM, RIGHT_ARM])
            mask[arms] = 0
            long_sleeves = bool(_LONG_SLEEVES.search(_KEEP.sub("", instruction or "")))
            mask[_sleeve_zone(labels, change, rgb, long_sleeves) > 0] = 1
            # las manos (y lo que sostengan), siempre fuera: con el detector de
            # manos, no adivinandolas por la forma del brazo
            # (solo lo que no es ropa: la camiseta de alrededor si cambia)
            garment = np.isin(labels, [UPPER, SKIRT, PANTS, DRESS, BELT, SCARF])
            mask[(_hands_mask(rgb) > 0) & ~garment] = 0
            return mask.astype(np.float32)
    return _clothes_mask_around_face(rgb)


_LONG_SLEEVES = re.compile(
    r"\b(long[- ]sleeved?|sweaters?|jumpers?|hoodies?|sweatshirts?|jackets?|coats?|blazers?|cardigans?|"
    r"turtlenecks?|suits?|tuxedos?|parkas?|raincoats?)\b", re.IGNORECASE)


# Detector de manos (palmas) de MediaPipe, version ONNX de opencv_zoo (Apache
# 2.0): el modelo de ropa no separa la mano del brazo y adivinarla por la
# forma del brazo fallaba (la mano del vaso se redibujaba; Sergio, 2026-10-07).
HANDS_MODEL = MODELS_DIR / "img" / "hands" / "palm_detection_mediapipe_2023feb.onnx"
_hands_session = None
_PALM_ANCHORS = None


def _palm_anchors() -> np.ndarray:
    """Las 2016 anclas del detector (SSD de MediaPipe: 24x24x2 + 12x12x6)."""
    global _PALM_ANCHORS
    if _PALM_ANCHORS is None:
        anchors = []
        for size, per_cell in ((24, 2), (12, 6)):
            for y in range(size):
                for x in range(size):
                    anchors += [((x + 0.5) / size, (y + 0.5) / size)] * per_cell
        _PALM_ANCHORS = np.array(anchors, np.float32)
    return _PALM_ANCHORS


def detect_hands(rgb: np.ndarray, threshold: float = 0.35) -> list[tuple[float, float, float]]:
    """Manos de la foto: (x, y, tamaño de la palma) en pixeles. [] sin modelo."""
    global _hands_session
    if not HANDS_MODEL.exists():
        return []
    if _hands_session is None:
        _hands_session = ort.InferenceSession(str(HANDS_MODEL), providers=["CPUExecutionProvider"])
    h, w = rgb.shape[:2]
    side = max(h, w)  # cuadrada, con relleno, como la espera el modelo
    square = np.zeros((side, side, 3), np.uint8)
    square[:h, :w] = rgb
    x = cv2.resize(square, (192, 192), interpolation=cv2.INTER_AREA).astype(np.float32)[None] / 255.0
    boxes, scores = _hands_session.run(None, {_hands_session.get_inputs()[0].name: x})
    boxes, scores = boxes[0], 1 / (1 + np.exp(-np.clip(scores[0, :, 0], -50, 50)))
    anchors = _palm_anchors()
    keep = scores > threshold
    if not keep.any():
        return []
    cx = boxes[keep, 0] / 192 + anchors[keep, 0]
    cy = boxes[keep, 1] / 192 + anchors[keep, 1]
    bw, bh = boxes[keep, 2] / 192, boxes[keep, 3] / 192
    rects = [[float((a - c / 2) * side), float((b - d / 2) * side), float(c * side), float(d * side)]
             for a, b, c, d in zip(cx, cy, bw, bh)]
    picked = cv2.dnn.NMSBoxes(rects, scores[keep].tolist(), threshold, 0.3)
    hands = []
    for i in np.array(picked).flatten():
        rx, ry, rw, rh = rects[int(i)]
        hands.append((rx + rw / 2, ry + rh / 2, max(rw, rh)))
    return hands


def _hands_mask(rgb: np.ndarray) -> np.ndarray:
    """Las manos enteras (dedos incluidos) y lo que tengan cogido justo
    alrededor: un circulo bastante mas grande que la palma."""
    h, w = rgb.shape[:2]
    out = np.zeros((h, w), np.uint8)
    for x, y, size in detect_hands(rgb):
        cv2.circle(out, (int(x), int(y)), int(1.4 * size), 1, -1)
    return out


def _sleeve_zone(labels: np.ndarray, garment: np.ndarray, rgb: np.ndarray, long_sleeves: bool) -> np.ndarray:
    """Que parte del brazo que sale de la prenda (no el de otra persona) se
    puede redibujar, medido A LO LARGO del brazo desde la prenda (en linea
    recta, un brazo doblado engañaba: el final del antebrazo quedaba "cerca"
    de la camiseta y salia una manga azul suelta en la muñeca, 2026-10-07):
    - siempre un tramo pegado a la prenda, para que la piel nueva y la
      original se fundan (si no, quedaba una linea donde acababa la manga);
    - con manga larga, todo el brazo menos la mano (la punta: lo ultimo, salvo
      que el brazo salga cortado por el borde de la foto)."""
    h, w = labels.shape
    zone = np.zeros((h, w), np.uint8)
    if not garment.any():
        return zone
    faces = _faces(rgb)
    face_w = float(np.median([f[2] for f in faces])) if faces else 0.12 * min(h, w)
    touching = cv2.dilate(garment, np.ones((5, 5), np.uint8)) > 0
    edge = max(2, int(0.01 * min(h, w)))
    border = np.zeros((h, w), bool)
    border[:edge], border[-edge:], border[:, :edge], border[:, -edge:] = True, True, True, True
    for arm_label in (LEFT_ARM, RIGHT_ARM):
        n, comps = cv2.connectedComponents((labels == arm_label).astype(np.uint8))
        for i in range(1, n):
            comp = comps == i
            contact = comp & touching
            if not contact.any():
                continue  # el brazo de otra persona
            # se mide desde el hombro: el contacto con la prenda mas cerca de la
            # cara (una mano apoyada delante de la camisa tambien la "toca")
            if faces:
                fx, fy = (float(v) for v in np.mean([[f[0] + f[2] / 2, f[1] + f[3] / 2] for f in faces], axis=0))
                ys, xs = np.nonzero(contact)
                d = np.hypot(xs - fx, ys - fy)
                keep = d <= d.min() + 0.6 * face_w
                contact = np.zeros_like(contact)
                contact[ys[keep], xs[keep]] = True
            along = _distance_along(comp, contact)
            reach = float(along[comp].max())
            # siempre: un tramo junto a la prenda para fundir la piel
            zone[comp & (along <= 0.45 * face_w)] = 1
            if long_sleeves:
                hand_cut = border[comp & (along >= 0.9 * reach)].any()  # la punta sale de la foto
                limit = reach if hand_cut else max(0.0, reach - 0.9 * face_w)  # sin la mano
                zone[comp & (along <= limit)] = 1
    return zone


def _distance_along(region: np.ndarray, start: np.ndarray) -> np.ndarray:
    """Distancia (en pixeles de la foto) desde `start` recorriendo solo por
    dentro de `region`, no en linea recta. Se calcula reducida (rapido)."""
    h, w = region.shape
    f = min(1.0, 256 / max(h, w))
    small_region = cv2.resize(region.astype(np.uint8), (max(1, int(w * f)), max(1, int(h * f))),
                              interpolation=cv2.INTER_NEAREST)
    small_start = cv2.resize(start.astype(np.uint8), small_region.shape[::-1], interpolation=cv2.INTER_NEAREST)
    small_start &= small_region
    dist = np.full(small_region.shape, np.inf, np.float32)
    frontier = small_start.astype(bool)
    dist[frontier] = 0
    kernel = np.ones((3, 3), np.uint8)
    step = 0
    while frontier.any():
        step += 1
        grown = cv2.dilate(frontier.astype(np.uint8), kernel).astype(bool) & small_region.astype(bool)
        new = grown & np.isinf(dist)
        dist[new] = step
        frontier = new
    dist[np.isinf(dist)] = 0
    return cv2.resize(dist, (w, h), interpolation=cv2.INTER_NEAREST) / f


def _clothes_mask_around_face(rgb: np.ndarray) -> np.ndarray | None:
    """Respaldo sin el modelo de ropa: las personas menos la cabeza. Pegar la
    cara original sobre un cuerpo generado entero nunca casaba (otro tono,
    otro cuello, la cabeza "de lado" sobre un cuello recto; Sergio,
    2026-10-07). None sin caras."""
    faces = _faces(rgb)
    if not faces:
        return None
    h, w = rgb.shape[:2]
    people = (matte(rgb) > 0.2).astype(np.uint8)
    k = max(3, int(min(h, w) * 0.04)) | 1
    mask = cv2.dilate(people, np.ones((k, k), np.uint8)).astype(np.float32)
    for f in faces:
        mask[_head_shape(f, h, w) > 0] = 0  # la cabeza entera, pelo incluido
    if mask.mean() < 0.01:
        return None
    return mask


def _head_shape(face: np.ndarray, h: int, w: int) -> np.ndarray:
    """La cabeza (con el pelo de arriba y de los lados) con su forma: la cara
    hasta la barbilla y, por encima de la frente, el pelo. Antes era un
    rectangulo a lo ancho de la foto hasta la altura de la barbilla: en un
    primer plano la barbilla queda a la altura de los hombros y estos se
    quedaban sin cambiar: media camisa hawaiana y media camiseta (Sergio,
    2026-10-07)."""
    x, y, fw, fh = (float(v) for v in face[:4])
    cx = int(x + fw / 2)
    head = np.zeros((h, w), np.uint8)
    # la cara hasta la barbilla. Aproximado: con la cabeza muy ladeada puede
    # quedar un trozo de ropa junto al cuello sin cambiar (pendiente de un
    # modelo que reconozca la ropa)
    cv2.ellipse(head, (cx, int(y + fh * 0.44)), (int(fw * 0.58), int(fh * 0.56)), 0, 0, 360, 1, -1)
    # el pelo de encima de la frente (los lados no: en un primer plano ya son
    # los hombros)
    top = int(y + fh * 0.3)
    head[:max(0, top), int(max(0, x - fw * 0.35)):int(min(w, x + fw * 1.35))] = 1
    return head


def mask_png_for_kontext(mask: np.ndarray) -> bytes:
    w, h = kontext_size(mask.shape[1], mask.shape[0])
    small = cv2.resize((mask * 255).astype(np.uint8), (w, h), interpolation=cv2.INTER_AREA)
    return to_png(np.dstack([small] * 3))


def compose_masked(orig: np.ndarray, edited_small: np.ndarray, mask: np.ndarray) -> np.ndarray:
    """Lo generado dentro de la mascara, la foto original exacta fuera. Con
    la mascara Kontext no reencuadra (el latente de fuera no cambia), asi que
    no hace falta alinear."""
    h, w = orig.shape[:2]
    edited = cv2.resize(edited_small, (w, h), interpolation=cv2.INTER_LANCZOS4)
    edited = add_grain(edited, grain_sigma(orig))
    for face in _faces(orig):
        edited = _junction_tone(orig, edited, mask, face)
    alpha = cv2.GaussianBlur(mask.astype(np.float32), (0, 0), max(1.5, min(h, w) * 0.004))[..., None]
    return (orig * (1 - alpha) + edited * alpha).clip(0, 255).astype(np.uint8)


def _junction_tone(orig: np.ndarray, edited: np.ndarray, mask: np.ndarray, face: np.ndarray) -> np.ndarray:
    """Solo COLOR, ni forma ni textura: la piel generada (cuello, escote)
    toma el tono de la piel original justo encima de la union (la barbilla),
    del todo pegado a ella y cada vez menos al alejarse. El cuello generado
    empezaba mas claro y rosado que la barbilla y se notaba el salto (Sergio,
    2026-10-07; un degradado de la mascara le borraba el cuello)."""
    h, w = orig.shape[:2]
    x, y, fw, fh = (float(v) for v in face[:4])
    chin = int(min(h, y + fh))
    d = max(4, int(fh * 0.1))
    x0, x1 = int(max(0, x - fw * 0.2)), int(min(w, x + fw * 1.2))
    if chin - d < 0 or chin + d > h or x1 <= x0:
        return edited
    lab_o = cv2.cvtColor(orig, cv2.COLOR_RGB2LAB).astype(np.float32)
    lab_e = cv2.cvtColor(edited, cv2.COLOR_RGB2LAB).astype(np.float32)
    skin = _cheeks(lab_o, face)

    def skin_like(lab):
        return (np.linalg.norm(lab[..., 1:] - skin[1:], axis=2) < 18) & (np.abs(lab[..., 0] - skin[0]) < 45)

    above = np.zeros((h, w), bool)
    above[chin - d:chin, x0:x1] = True
    above &= (mask < 0.1) & skin_like(lab_o)
    below = np.zeros((h, w), bool)
    below[chin:chin + d, x0:x1] = True
    below &= (mask > 0.9) & skin_like(lab_e)
    if above.sum() < 30 or below.sum() < 30:
        return edited
    neck = np.median(lab_e[below], axis=0)
    shift = np.clip(np.median(lab_o[above], axis=0) - neck, [-30, -15, -15], [30, 15, 15])
    if np.abs(shift).max() < 1.5:
        return edited
    # la piel generada con el color de ese cuello (no la ropa, el pelo...)
    weight = (np.clip(1.5 - np.linalg.norm(lab_e[..., 1:] - neck[1:], axis=2) / 10, 0, 1)
              * np.clip(1.5 - np.abs(lab_e[..., 0] - neck[0]) / 35, 0, 1) * (mask > 0.5))
    # entera junto a la union y menos al alejarse (a un alto de cara, la mitad)
    yy = np.arange(h, dtype=np.float32)[:, None]
    fade = np.where(yy < chin, 1.0, np.exp(-(yy - chin) / (fh * 1.44)))
    weight = cv2.GaussianBlur((weight * fade).astype(np.float32), (0, 0), max(1.5, fw * 0.03))
    lab_e = lab_e + weight[..., None] * shift
    out = cv2.cvtColor(lab_e.clip(0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB)
    touched = np.clip(weight * 20, 0, 1)[..., None]
    return (out * touched + edited * (1 - touched)).astype(np.uint8)


def finish(orig: np.ndarray, edited_small: np.ndarray, mode: str) -> np.ndarray:
    """`edited_small`: lo que devolvio Kontext (o ya reescalado), se lleva a la
    resolucion de la original antes de componer."""
    h, w = orig.shape[:2]
    edited = cv2.resize(edited_small, (w, h), interpolation=cv2.INTER_LANCZOS4)
    # lo de Kontext sale liso; junto a la original con su grano (o con la cara
    # original encima) se notaba pegado (Sergio, 2026-10-07)
    edited = add_grain(edited, grain_sigma(orig))
    if mode == "entera":
        return edited  # cambio de postura: ver is_pose_change
    if mode == "fondo":
        return compose_background(orig, edited)
    return compose_local(orig, edited)


_POSE = re.compile(
    r"\b(zoom(ed)? out|kneel\w*|lie|lying|lies|lay\w*|sit\w*|seated|stand\w*|lean\w*|bend\w*|crouch\w*|squat\w*|"
    r"hug\w*|embrac\w*|danc\w*|twirl\w*|hold(s|ing)? hands|hand in hand|kiss\w*|wav\w*|rais\w*|jump\w*|walk\w*|"
    r"arms? crossed|cross(es|ed)? (his|her|their) arms|hands? on (his|her|their) (hips|waist)|pose|posing)\b",
    re.IGNORECASE)


def is_pose_change(instruction: str) -> bool:
    """La instruccion cambia la postura (solo la parte que pide, no el "Keep
    ... pose" del final). Ahi no se vuelve a pegar la foto de partida en lo que
    parece igual: quedaba un fantasma translucido del brazo levantado de antes
    en el cielo (2026-10-06). Se usa la de Kontext entera y luego la cara."""
    return bool(_POSE.search(_KEEP.sub("", instruction or "")))


def needs_upscale(orig: np.ndarray, edited_small: np.ndarray) -> bool:
    """ESRGAN solo si hay que ampliar mas del doble: deja la piel como de
    plastico y el pelo crujiente, y hasta 2x el reescalado normal (con el
    grano de la foto, ver finish) se integra mejor (2026-10-07; antes 1,4x)."""
    return max(orig.shape[:2]) > 2.0 * max(edited_small.shape[:2])


def grain_sigma(rgb: np.ndarray) -> float:
    """Ruido de la foto (desviacion tipica, niveles 0-255) en las zonas lisas:
    el residuo tras un desenfoque fino, con la mediana para que los bordes y
    la textura no cuenten."""
    h, w = rgb.shape[:2]
    if max(h, w) > 2000:  # mas rapido con un trozo del centro, a la misma escala
        y, x = max(0, (h - 2000) // 2), max(0, (w - 2000) // 2)
        rgb = rgb[y:y + 2000, x:x + 2000]
    g = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY).astype(np.float32)
    res = g - cv2.GaussianBlur(g, (0, 0), 1.2)
    grad = cv2.GaussianBlur(np.abs(cv2.Sobel(cv2.GaussianBlur(g, (0, 0), 2), cv2.CV_32F, 1, 1)), (0, 0), 3)
    flat = grad < np.percentile(grad, 40)
    return float(1.4826 * np.median(np.abs(res[flat])))


def add_grain(img: np.ndarray, target: float, seed: int = 0) -> np.ndarray:
    """Añade el grano que le falta a `img` para llegar a `target`."""
    need = np.sqrt(max(0.0, target ** 2 - grain_sigma(img) ** 2))
    if need < 0.5:
        return img
    h, w = img.shape[:2]
    noise = cv2.GaussianBlur(np.random.default_rng(seed).normal(0, 1, (h, w)).astype(np.float32), (0, 0), 0.6)
    measured = 1.4826 * np.median(np.abs(noise - cv2.GaussianBlur(noise, (0, 0), 1.2)))
    return (img.astype(np.float32) + (noise * (need / measured))[..., None]).clip(0, 255).astype(np.uint8)

