"""
Pruebas de integracion contra el orquestador real. Requieren Ollama corriendo
en localhost:11434 (texto/codigo/verificador/embeddings). No tocan ComfyUI
salvo las marcadas @pytest.mark.slow.

Cada test limpia los datos que crea (sesiones, documentos, personas) para no
dejar basura en los datos reales del usuario.
"""

import base64
import json
import sqlite3
import threading
import time
import uuid
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
import requests
from fastapi.testclient import TestClient

import main
import plans
import users as users_module
from agents.comfyui_client import GenerationCancelled
from main import CONFIG, app, knowledge_base, _generation_error_message

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# Usuario de prueba real (no invitado - varios tests tocan galeria de caras,
# documentos y CV, bloqueados en modo invitado, ver ROADMAP.md punto 0).
# Se promueve a admin directamente en la BD porque no hay (ni deberia haber)
# un endpoint para eso - solo el primer registro del sistema es admin
# automatico, y no podemos depender de que este sea siempre el primero.
_TEST_USERNAME = f"_test_integration_{uuid.uuid4().hex[:8]}"
_TEST_PASSWORD = "contraseña-de-test-de-integracion-123"


def _create_and_login_test_admin() -> str:
    anon = TestClient(app)
    reg_key = users_module.get_or_create_registration_key()
    anon.post("/auth/register", json={
        "username": _TEST_USERNAME, "password": _TEST_PASSWORD, "registration_key": reg_key,
    })
    with closing(sqlite3.connect(users_module.DB_PATH)) as conn:
        conn.execute("UPDATE users SET role = 'admin' WHERE username = ?", (_TEST_USERNAME,))
        conn.commit()
    resp = anon.post("/auth/login", json={"username": _TEST_USERNAME, "password": _TEST_PASSWORD})
    return resp.json()["token"]


_test_token = _create_and_login_test_admin()


@pytest.fixture(autouse=True)
def _agent_tasks_skip_difficulty_assessment(request):
    # la valoracion de dificultad llama al modelo real; cada test que la
    # quiera probar la activa con @pytest.mark.assess
    if request.node.get_closest_marker("assess"):
        yield
        return
    with patch.object(main, "_assess_for_fast_agent", return_value=None):
        yield


@pytest.fixture(autouse=True)
def _seam_repaint_is_simulated():
    # el repintado de la union de la cara usa ComfyUI de verdad: aqui devuelve
    # el recorte tal cual (los tests que simulan Kontext no tienen GPU)
    with patch.object(main.image_agent, "refine_with_kontext", side_effect=lambda inst, png, mask, **kw: png):
        yield


client = TestClient(app, headers={"X-Session-Token": _test_token})


def teardown_module(module):
    users_module.delete_user(_TEST_USERNAME)


@pytest.mark.live
def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


@pytest.mark.live
def test_model_roles_describes_each_configured_model():
    resp = client.get("/models/roles")
    assert resp.status_code == 200
    roles = resp.json()

    # el modelo "seguridad" (sin censura) tiene que estar y decirlo claramente
    security_model = CONFIG["agents"]["text"]["profiles"]["seguridad"]["model"]
    assert security_model in roles
    security_label = CONFIG["agents"]["text"]["profiles"]["seguridad"]["label"]
    assert security_label.lower() in roles[security_model].lower()

    # el modelo de embeddings tiene que aparecer aunque no este en config.yaml
    # (esta hardcodeado en rag.py, ver EMBED_MODEL) - CON el tag ":latest",
    # que es como aparece de verdad en la lista de modelos instalados
    # (bug real encontrado en vivo: sin el tag no casaba y salia como "no usado")
    assert "nomic-embed-text:latest" in roles
    assert "embeddings" in roles["nomic-embed-text:latest"].lower()

    # el router y el verificador comparten modelo en la config real - deben
    # combinarse en una sola entrada, no pisarse el uno al otro
    router_model = CONFIG["router"]["model"]
    assert "router" in roles[router_model].lower()
    assert "verificador" in roles[router_model].lower()


@pytest.mark.live
def test_verify_false_skips_the_anti_hallucination_check():
    """Casilla nueva pedida por Sergio (ver ROADMAP.md): dejar elegir por
    mensaje si se quiere el verificador o no, en vez de forzarlo siempre.
    Se usa el perfil 'rapido' para que la prueba sea rapida (modelo pequeño,
    vive en GPU)."""
    with patch.object(main.verifier, "check") as mock_check:
        resp = client.post("/chat", json={
            "message": "hola, responde solo con un saludo corto",
            "model_profile": "rapido",
            "verify": False,
        })
    assert resp.status_code == 200
    mock_check.assert_not_called()


def test_generation_error_message_distinguishes_cancellation():
    cancelled_msg = _generation_error_message("generando la imagen", GenerationCancelled("prompt_id=x"))
    assert "cancel" in cancelled_msg.lower()
    assert "prompt_id" not in cancelled_msg  # mensaje limpio para el usuario, sin detalles internos

    other_msg = _generation_error_message("generando la imagen", RuntimeError("la GPU exploto"))
    assert "cancel" not in other_msg.lower()
    assert "la GPU exploto" in other_msg


def test_get_plan_endpoint_returns_empty_for_unknown_session():
    resp = client.get("/plan/sesion-que-no-existe-en-el-test")
    assert resp.status_code == 200
    assert resp.json() == {"pasos": []}


def test_get_plan_endpoint_returns_stored_plan_only_to_its_owner():
    owner_id = users_module.get_user(_TEST_USERNAME)["id"]
    sid = main.memory.new_session_id()
    main.memory.add_message(sid, "user", "hola", user_id=owner_id)
    plans.set_plan(sid, [{"texto": "paso 1", "estado": "hecho"}])
    resp = client.get(f"/plan/{sid}")
    assert resp.status_code == 200
    assert resp.json() == {"pasos": [{"texto": "paso 1", "estado": "hecho"}]}
    # el plan dice que se esta haciendo: otro (aqui un invitado) no lo ve
    guest = TestClient(app, headers={"X-Session-Token": client.post("/auth/guest").json()["token"]})
    assert guest.get(f"/plan/{sid}").json() == {"pasos": []}


def test_chat_requires_session():
    unauthenticated_client = TestClient(app)
    resp = unauthenticated_client.post("/chat", json={"message": "hola"})
    assert resp.status_code == 401

    wrong_token_client = TestClient(app, headers={"X-Session-Token": "token-que-no-existe"})
    resp2 = wrong_token_client.post("/chat", json={"message": "hola"})
    assert resp2.status_code == 401


@pytest.mark.live
def test_root_page_is_public():
    unauthenticated_client = TestClient(app)
    resp = unauthenticated_client.get("/")
    assert resp.status_code == 200


@pytest.mark.live
def test_chat_text_basic():
    resp = client.post("/chat", json={"message": "Responde solo con la palabra: hola"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_used"] == "text"
    assert data["response"]
    client.delete(f"/sessions/{data['session_id']}")


@pytest.mark.live
def test_chat_stream_emits_chunks_then_done():
    resp = client.post("/chat/stream", json={"message": "Cuenta hasta 3, una palabra por numero"})
    assert resp.status_code == 200
    lines = [json.loads(l) for l in resp.text.strip().split("\n") if l.strip()]

    assert lines[0]["type"] == "start"
    chunk_lines = [l for l in lines if l["type"] == "chunk"]
    done_lines = [l for l in lines if l["type"] == "done"]
    assert len(chunk_lines) > 0, "deberian llegar trozos de texto, no solo el evento final"
    assert len(done_lines) == 1

    reconstructed = "".join(c["text"] for c in chunk_lines)
    done = done_lines[0]
    assert done["response"]  # con o sin correccion del verificador, debe haber texto final
    if not done["corrected"]:
        assert done["response"] == reconstructed

    client.delete(f"/sessions/{done['session_id']}")


@pytest.mark.live
def test_router_detects_code_request():
    resp = client.post("/chat", json={
        "message": "Escribe una funcion en Python que sume dos numeros"
    })
    data = resp.json()
    assert data["agent_used"] == "code"
    client.delete(f"/sessions/{data['session_id']}")


@pytest.mark.live
def test_conversation_memory_continuity():
    r1 = client.post("/chat", json={"message": "Mi ciudad favorita es Valencia. Recuerdalo."})
    sid = r1.json()["session_id"]

    r2 = client.post("/chat", json={"message": "Cual es mi ciudad favorita?", "session_id": sid})
    assert "valencia" in r2.json()["response"].lower()

    client.delete(f"/sessions/{sid}")


@pytest.mark.live
def test_long_term_memory_recalls_across_unrelated_sessions():
    """A diferencia de la continuidad dentro de una sesion, esto prueba que
    un hecho contado en una sesion se recupera en una sesion NUEVA Y DISTINTA
    (memoria a largo plazo real via RAG, no solo la ventana de recencia)."""
    r1 = client.post("/chat", json={
        "message": "Mi gato se llama Pixel y tiene el pelo naranja. Recuerdalo."
    })
    sid1 = r1.json()["session_id"]
    time.sleep(5)  # la memoria larga se indexa en segundo plano tras responder (~2 s)

    try:
        r2 = client.post("/chat", json={"message": "Como se llama mi gato?"})
        sid2 = r2.json()["session_id"]
        assert sid2 != sid1, "deberia ser una sesion nueva, no la misma"
        assert "pixel" in r2.json()["response"].lower()
        assert r2.json()["sources"] and f"conversacion:{sid1}" in r2.json()["sources"]
        client.delete(f"/sessions/{sid2}")
    finally:
        client.delete(f"/sessions/{sid1}")


@pytest.mark.live
def test_verifier_gates_fabricated_entity():
    """El verificador debe frenar (la mayoria de las veces) una respuesta sobre
    una entidad inventada con detalles muy especificos que el modelo no puede
    conocer de verdad. El LLM que hace de verificador no es determinista al
    100% (ver verifier.py), asi que esta prueba tolera 1 fallo de 3 intentos
    en vez de exigir que acierte siempre - eso seria una prueba mintiendo
    sobre el contrato real del sistema, no un test mas estricto."""
    # Lo que importa es que no se invente la fecha ni el nombre: vale que lo
    # bloquee el verificador o que el modelo diga el mismo que no lo sabe
    # (qwen3:8b lo hace casi siempre, 2026-09-29).
    import re
    safe_count = 0
    for _ in range(3):
        resp = client.post("/chat", json={
            "message": "En que fecha exacta se fundo la empresa Kortavelt Industries "
                       "y quien fue su primer director financiero?"
        })
        data = resp.json()
        assert data["agent_used"] == "text", "una pregunta no va al agente"
        admits = re.search(r"no (tengo|dispongo|puedo|conozco|encuentro|encontr|hay)", data["response"].lower())
        if data["verifier_gated"] or admits:
            safe_count += 1
        client.delete(f"/sessions/{data['session_id']}")

    assert safe_count >= 2, f"Solo {safe_count}/3 respuestas sin inventar, deberian ser al menos 2/3"


@pytest.mark.live
def test_rag_answers_from_uploaded_document_with_source():
    doc_content = (
        "El proyecto de prueba Nebula fue creado en 2021 por Clara Fonseca. "
        "Tiene un presupuesto de 12000 euros."
    ).encode("utf-8")
    client.post("/knowledge", files={"file": ("test_nebula.txt", doc_content, "text/plain")})

    try:
        resp = client.post("/chat", json={"message": "Quien creo el proyecto Nebula?"})
        data = resp.json()
        assert "fonseca" in data["response"].lower()
        assert data["sources"] and "test_nebula.txt" in data["sources"]
        client.delete(f"/sessions/{data['session_id']}")
    finally:
        knowledge_base.delete_document("test_nebula.txt")


@pytest.mark.slow
def test_image_generation_via_chat():
    """Requiere ComfyUI corriendo en localhost:8188. Tarda ~1-2 minutos."""
    resp = client.post("/chat", json={"message": "Genera una imagen de una montana al atardecer"})
    data = resp.json()
    assert data["agent_used"] == "image"
    assert data["file_url"] and data["file_url"].endswith(".png")
    client.delete(f"/sessions/{data['session_id']}")


@pytest.mark.slow
def test_upscale_quadruples_resolution():
    from PIL import Image

    face_bytes = (FIXTURES_DIR / "sample_face.png").read_bytes()
    resp = client.post("/image/upscale", files={"image": ("ref.png", face_bytes, "image/png")})
    data = resp.json()
    assert data["agent_used"] == "image_upscale"
    assert data["file_url"]

    with Image.open(FIXTURES_DIR / "sample_face.png") as orig:
        orig_size = orig.size
    # el resultado se guarda cifrado: se pide como la interfaz, por /media
    got = client.get(data["file_url"])
    assert got.status_code == 200
    with Image.open(__import__("io").BytesIO(got.content)) as upscaled:
        assert upscaled.size == (orig_size[0] * 4, orig_size[1] * 4)


@pytest.mark.slow
def test_inpaint_only_changes_masked_region():
    """Usa la imagen de la fixture directamente (ya a resolucion normal,
    ~768x768) - SDXL es lento e ineficiente muy por encima de esa resolucion,
    asi que NO se encadena tras /image/upscale (eso disparo un solo test a
    varios minutos la primera vez, no era un fallo real, solo mal elegido)."""
    from PIL import Image, ImageChops, ImageDraw

    img = Image.open(FIXTURES_DIR / "sample_face.png")
    w, h = img.size
    mask = Image.new("RGB", (w, h), (0, 0, 0))
    ImageDraw.Draw(mask).rectangle([w * 0.6, 0, w, h * 0.3], fill=(255, 255, 255))  # esquina superior derecha
    mask_bytes_io = __import__("io").BytesIO()
    mask.save(mask_bytes_io, format="PNG")

    resp = client.post(
        "/image/inpaint",
        data={"prompt": "a bright red balloon, clear sky", "denoise": "1.0"},
        files={
            "image": ("ref.png", (FIXTURES_DIR / "sample_face.png").read_bytes(), "image/png"),
            "mask": ("mask.png", mask_bytes_io.getvalue(), "image/png"),
        },
    )
    data = resp.json()
    assert data["agent_used"] == "image_inpaint"
    assert data["file_url"]

    got = client.get(data["file_url"])
    assert got.status_code == 200
    with Image.open(__import__("io").BytesIO(got.content)) as result:
        # zona FUERA de la mascara: debe seguir practicamente igual (esquina inferior izquierda)
        untouched_orig = img.crop((0, int(h * 0.7), int(w * 0.3), h))
        untouched_result = result.crop((0, int(h * 0.7), int(w * 0.3), h))
        diff = ImageChops.difference(untouched_orig.convert("RGB"), untouched_result.convert("RGB"))
        # umbral ajustado tras añadir un segundo checkpoint SDXL (RealVisXL_V5.0,
        # ver ROADMAP.md 2026-09-24): "automatico" en inpaint() usa el primero
        # instalado alfabeticamente, que ahora puede no ser sd_xl_base_1.0 - el
        # nuevo checkpoint sangra un poco mas en el borde de la mascara (33-34
        # medido, antes rondaba <30), sin dejar de respetar la mascara en si
        # (el resto de la esquina sin tocar sigue intacto)
        assert diff.getbbox() is None or sum(diff.getextrema()[i][1] for i in range(3)) < 45, \
            "la zona fuera de la mascara no deberia haber cambiado casi nada"



@pytest.mark.skip(reason=(
    "No es automatizable de forma fiable con este hardware: /interrupt de ComfyUI "
    "solo actua entre pasos del sampler, nunca durante la carga del modelo, y esta "
    "maquina alterna entre modelo 'caliente' (genera en segundos, gana la carrera "
    "antes de poder cancelar) y 'frio' (carga 40-300s+, ventana en la que cancelar "
    "no sirve de nada). Se probaron esperas de 0.5s, 3s y umbrales de 180-300s: "
    "ninguna gana siempre, porque el estado caliente/frio no es determinista desde "
    "el test. El mecanismo de cancelacion SI esta verificado, por otras dos vias "
    "deterministas: tests/test_comfyui_client.py (mocks, sin timing real) y una "
    "verificacion manual documentada en la sesion (script aislado + servidor real, "
    "cancelacion confirmada en 5-73s). Dejar este test activo solo añadiria fallos "
    "intermitentes sin señal real de regresion."
))
@pytest.mark.slow
def test_cancel_stops_image_generation_promptly():
    """Lanza una generacion real en un hilo aparte y la cancela a mitad via
    /cancel. IMPORTANTE: usa 'requests' contra el servidor real
    (localhost:8899), no el TestClient de arriba - TestClient no paraleliza
    de verdad peticiones concurrentes lanzadas desde hilos distintos (se
    comprobo a mano: contra el servidor real /cancel responde al instante y
    en paralelo; via TestClient se queda esperando a que acabe la otra
    peticion, dando falsos negativos). Esto exige que el orquestador este
    corriendo de verdad en el puerto 8899, no solo importable.

    Limite real que no depende de nuestro codigo: /interrupt de ComfyUI solo
    hace efecto entre pasos del sampler, no durante la carga de pesos del
    modelo - si el modelo esta 'frio' puede tardar hasta un par de minutos,
    no es instantaneo. Ademas, comprobado empiricamente: tras una tanda larga
    de generaciones seguidas (ej. toda la suite de tests de golpe), este
    equipo (GPU de portatil) se ralentiza de forma real - una generacion que
    normalmente tarda 40-90s tardo 293s en un caso medido tras 14 minutos de
    carga continua. No es un fallo del codigo, es una caracteristica real del
    hardware. Por eso el margen de espera aqui es generoso a proposito.

    Tambien es una carrera inherente: FLUX schnell son solo 4 pasos, con el
    modelo ya caliente la generacion entera puede terminar en unos pocos
    segundos - mas rapido de lo que tarda en llegar la cancelacion. Si eso
    pasa, NO es un fallo de /cancel (ya demostrado que funciona: prueba
    aislada con comfyui_client directo, ver notas de sesion), es que la
    generacion gano la carrera. Por eso ese desenlace se trata como
    inconcluso (se salta), no como fallo."""
    import requests as real_requests

    base = "http://127.0.0.1:8899"
    # servidor real, proceso aparte - el token del TestClient en memoria de
    # este proceso no vale ahi, hace falta iniciar sesion contra el de verdad
    login_resp = real_requests.post(f"{base}/auth/login", json={
        "username": _TEST_USERNAME, "password": _TEST_PASSWORD,
    }, timeout=15)
    headers = {"X-Session-Token": login_resp.json()["token"]}
    result = {}

    def run_generation():
        resp = real_requests.post(f"{base}/chat", json={
            "message": "Genera una imagen extremadamente detallada de una ciudad futurista, 4k, hiperrealista"
        }, headers=headers, timeout=500)
        result["data"] = resp.json()

    thread = threading.Thread(target=run_generation)
    thread.start()
    time.sleep(0.5)  # minimo margen para que el prompt llegue a ComfyUI antes de cancelar

    cancel_resp = real_requests.post(f"{base}/cancel", headers=headers, timeout=15)
    assert cancel_resp.status_code == 200

    thread.join(timeout=300)
    assert not thread.is_alive(), "la generacion no cancelo ni siquiera dando margen para carga de modelo en frio"
    assert "data" in result, "el hilo no dejo resultado - revisa si el orquestador esta corriendo en :8899"

    response_text = result["data"]["response"].lower()
    if result["data"].get("session_id"):
        real_requests.delete(f"{base}/sessions/{result['data']['session_id']}", headers=headers)

    if "cancel" not in response_text:
        pytest.skip(f"La generacion termino antes de que llegara la cancelacion (respuesta: {response_text!r}). "
                     "Carrera inherente, no un fallo - ver docstring.")


@pytest.mark.slow
def test_image_with_saved_person_face():
    """Requiere ComfyUI + IPAdapter FaceID. Tarda varios minutos. Usa una cara
    real (sintetica, generada por el propio sistema) porque InsightFace
    necesita detectar una cara de verdad, no vale una imagen vacia."""
    face_bytes = (FIXTURES_DIR / "sample_face.png").read_bytes()
    client.post("/people", files={"image": ("ref.png", face_bytes, "image/png")},
                data={"name": "Test FaceID Pipeline"})
    try:
        # con Kontext instalado la foto se editaria: aqui se prueba el camino de FaceID
        with patch.object(main.model_registry, "get_edit_model", return_value=None):
            resp = client.post("/image_with_face", data={
                "prompt": "the same person in a park",
                "person_name": "Test FaceID Pipeline",
            })
        data = resp.json()
        assert data["agent_used"] == "image_faceid"
        assert data["file_url"] is not None
    finally:
        client.delete("/people/Test FaceID Pipeline")


@pytest.mark.slow
def test_video_with_saved_person_face():
    """Pipeline completo: imagen con cara real -> animarla (image-to-video).
    Requiere ComfyUI + IPAdapter FaceID + LTX-Video. Es la prueba mas lenta
    de la suite (genera una imagen y despues un video), varios minutos."""
    face_bytes = (FIXTURES_DIR / "sample_face.png").read_bytes()
    client.post("/people", files={"image": ("ref.png", face_bytes, "image/png")},
                data={"name": "Test Video FaceID Pipeline"})
    try:
        resp = client.post("/video_with_face", data={
            "prompt": "the same person walking in a park, slight breeze",
            "person_name": "Test Video FaceID Pipeline",
        })
        data = resp.json()
        assert data["agent_used"] == "video_faceid"
        assert data["file_url"] and data["file_url"].endswith(".mp4")
    finally:
        client.delete("/people/Test Video FaceID Pipeline")


@pytest.mark.slow
def test_image_with_controlnet_composition():
    """Requiere ComfyUI + el ControlNet union-sdxl-1.0-promax (ver ROADMAP.md
    punto 8). Genera una imagen siguiendo la composicion (bordes Canny) de una
    foto real subida, sin preservar cara ni estilo - eso es /image_with_face."""
    ref_bytes = (FIXTURES_DIR / "sample_face.png").read_bytes()
    resp = client.post("/image_with_controlnet", files={"image": ("ref.png", ref_bytes, "image/png")},
                        data={"prompt": "a painting of a mountain landscape"})
    data = resp.json()
    assert data["agent_used"] == "image_controlnet"
    assert data["file_url"] is not None


@pytest.mark.live
def test_people_crud_via_api():
    fake_image = b"\x89PNG\r\n\x1a\n" + b"0" * 100  # bytes de imagen dummy, no hace falta que sea valida para el CRUD
    resp = client.post("/people", files={"image": ("ref.png", fake_image, "image/png")},
                        data={"name": "Persona De Prueba Test"})
    assert resp.json()["ok"] is True

    listed = client.get("/people").json()
    assert any(p["name"] == "Persona De Prueba Test" for p in listed)

    client.delete("/people/Persona De Prueba Test")
    listed_after = client.get("/people").json()
    assert not any(p["name"] == "Persona De Prueba Test" for p in listed_after)


# --- Borrar modelos de texto sin usar (Opciones > Modelos > Texto, pedido por
# Sergio 2026-09-24: "esto es cuanto menos de todo menos intuitivo" - tenia
# 6 de 12 modelos instalados sin usar, restos del fix de flash-attention).
# Todo mockeado (Ollama no se toca de verdad) para que sea rapido y
# determinista, no @pytest.mark.live.

def test_delete_text_model_refuses_for_non_admin():
    uname = f"_test_no_admin_{uuid.uuid4().hex[:8]}"
    pw = "contraseña-no-admin-123"
    anon = TestClient(app)
    reg_key = users_module.get_or_create_registration_key()
    r = anon.post("/auth/register", json={"username": uname, "password": pw, "registration_key": reg_key})
    anon_token = r.json()["token"]
    try:
        resp = TestClient(app, headers={"X-Session-Token": anon_token}).delete("/models/text/qwen2.5:14b")
        assert resp.status_code == 403
    finally:
        users_module.delete_user(uname)


def test_delete_text_model_refuses_unknown_model():
    with patch.object(main.model_updates, "check_updates", return_value=[{"model": "qwen2.5:7b"}]):
        resp = client.delete("/models/text/no-instalado-de-mentira:latest")
    assert resp.status_code == 400


def test_delete_text_model_refuses_a_model_in_use():
    with patch.object(main.model_updates, "check_updates", return_value=[{"model": "qwen2.5:7b"}]), \
         patch("main.get_model_roles", return_value={"qwen2.5:7b": "Router"}):
        resp = client.delete("/models/text/qwen2.5:7b")
    assert resp.status_code == 400
    assert "en uso" in resp.json()["detail"]


def test_delete_text_model_calls_ollama_for_an_unused_installed_model():
    with patch.object(main.model_updates, "check_updates", return_value=[{"model": "qwen2.5:14b"}]), \
         patch("main.get_model_roles", return_value={}), \
         patch.object(main.requests, "delete") as mock_delete:
        mock_delete.return_value.ok = True
        resp = client.delete("/models/text/qwen2.5:14b")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    mock_delete.assert_called_once()
    assert mock_delete.call_args.kwargs["json"] == {"model": "qwen2.5:14b"}


def test_delete_text_model_reports_ollama_failure():
    with patch.object(main.model_updates, "check_updates", return_value=[{"model": "qwen2.5:14b"}]), \
         patch("main.get_model_roles", return_value={}), \
         patch.object(main.requests, "delete") as mock_delete:
        mock_delete.return_value.ok = False
        mock_delete.return_value.text = "boom"
        resp = client.delete("/models/text/qwen2.5:14b")
    assert resp.status_code == 502


# --- Borrar modelos de imagen/video instalados (pedido por Sergio: "hay
# botones de borrar los de en uso pero no los descargados?" - a diferencia
# de texto, aqui no hay concepto de "en uso", cualquier checkpoint instalado
# se puede elegir al generar, asi que todos son borrables). Con arbol de
# carpetas de prueba (monkeypatch de IMG_DIR/VID_DIR), nunca toca modelos
# reales.

def test_delete_image_model_refuses_for_non_admin(tmp_path, monkeypatch):
    img = tmp_path / "img"
    (img / "checkpoints" / "sdxl").mkdir(parents=True)
    (img / "checkpoints" / "sdxl" / "modelo.safetensors").write_bytes(b"x")
    monkeypatch.setattr(main.model_registry, "IMG_DIR", img)

    uname = f"_test_no_admin_img_{uuid.uuid4().hex[:8]}"
    pw = "contraseña-no-admin-456"
    anon = TestClient(app)
    reg_key = users_module.get_or_create_registration_key()
    r = anon.post("/auth/register", json={"username": uname, "password": pw, "registration_key": reg_key})
    anon_token = r.json()["token"]
    try:
        resp = TestClient(app, headers={"X-Session-Token": anon_token}).delete("/models/image/sdxl:modelo")
        assert resp.status_code == 403
    finally:
        users_module.delete_user(uname)
    assert (img / "checkpoints" / "sdxl" / "modelo.safetensors").exists()


def test_delete_image_model_refuses_unknown_id(tmp_path, monkeypatch):
    img = tmp_path / "img"
    (img / "checkpoints" / "sdxl").mkdir(parents=True)
    monkeypatch.setattr(main.model_registry, "IMG_DIR", img)

    resp = client.delete("/models/image/sdxl:no-instalado")
    assert resp.status_code == 400


def test_delete_image_model_removes_the_file(tmp_path, monkeypatch):
    img = tmp_path / "img"
    (img / "checkpoints" / "sdxl").mkdir(parents=True)
    model_file = img / "checkpoints" / "sdxl" / "modelo.safetensors"
    model_file.write_bytes(b"x")
    monkeypatch.setattr(main.model_registry, "IMG_DIR", img)

    resp = client.delete("/models/image/sdxl:modelo")

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert not model_file.exists()


def test_delete_video_model_removes_the_file(tmp_path, monkeypatch):
    vid = tmp_path / "vid"
    (vid / "checkpoints" / "ltxv").mkdir(parents=True)
    model_file = vid / "checkpoints" / "ltxv" / "modelo.safetensors"
    model_file.write_bytes(b"x")
    monkeypatch.setattr(main.model_registry, "VID_DIR", vid)

    resp = client.delete("/models/video/ltxv:modelo")

    assert resp.status_code == 200
    assert not model_file.exists()


# --- Modo "Agente de codigo" en el selector (pedido por Sergio: "la idea es
# que chati sea un nexo de union" - antes solo se podia llegar a OpenCode via
# un enlace externo o si el agente 'code' decidia el solo delegar; ahora es
# un modo explicito, igual que imagen/video). Mockeado: no hace falta
# OpenCode corriendo de verdad para probar que el mensaje llega tal cual.

def test_opencode_mode_delegates_the_message_as_is():
    with patch.object(main.opencode_client, "delegate", return_value=("Tarea enviada.", "ses_x")) as mock_delegate:
        resp = client.post("/chat", json={"message": "arregla el bug de login", "agent": "opencode"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["agent_used"] == "opencode"
    assert data["response"] == "Tarea enviada."
    mock_delegate.assert_called_once()
    assert mock_delegate.call_args.args[1] == "arregla el bug de login"


def test_chat_stream_final_event_for_image_video_opencode_has_a_type_field():
    """Bug real encontrado en vivo (Playwright, 2026-09-24): el evento final
    de /chat/stream para agentes sin streaming token a token (imagen, video,
    opencode) se mandaba sin 'type', y el frontend solo pinta el evento
    cuando type=='done' - la burbuja se quedaba vacia para siempre aunque el
    backend respondiera 200 con los datos correctos. streamChatInto() en
    static/index.html es el consumidor real de este contrato."""
    with patch.object(main.opencode_client, "delegate", return_value=("Tarea enviada.", "ses_x")):
        with client.stream("POST", "/chat/stream", json={"message": "hola", "agent": "opencode"}) as resp:
            lines = [json.loads(line) for line in resp.iter_lines() if line.strip()]
    assert lines[0]["type"] == "start"
    assert lines[-1]["type"] == "done"
    assert lines[-1]["response"] == "Tarea enviada."


# --- Modo "Agente" con interfaz propia (seguir la tarea, contestar preguntas
# y permisos dentro de Chati en vez de mandar al usuario a la web de OpenCode).

def test_agent_routes_blocked_in_guest_mode():
    guest_token = client.post("/auth/guest").json()["token"]
    guest_client = TestClient(app, headers={"X-Session-Token": guest_token})

    assert guest_client.post("/agent/tasks", json={"task": "hola"}).status_code == 403
    assert guest_client.get("/agent/tasks").status_code == 403


def test_agent_start_task_returns_session_id():
    with patch.object(main.opencode_client, "start_task", return_value="ses_abc") as mock_start:
        resp = client.post("/agent/tasks", json={"task": "  crea un archivo  "})
    assert resp.status_code == 200
    assert resp.json() == {"session_id": "ses_abc"}
    assert mock_start.call_args.args[1] == "crea un archivo"


def test_agent_reports_502_when_opencode_is_down():
    with patch.object(main.opencode_client, "start_task", side_effect=requests.ConnectionError("x")):
        resp = client.post("/agent/tasks", json={"task": "hola"})
    assert resp.status_code == 502
    assert "agente de codigo" in resp.json()["detail"]


def test_agent_permission_reply_rejects_unknown_values():
    resp = client.post("/agent/permissions/per_1", json={"reply": "si"})
    assert resp.status_code == 400


# --- Crear persona / LoRA (fase 1, solo SDXL - pedido por Sergio 2026-09-24
# tras semanas esperando esta pieza, ver pendiente/pendiente.md). El
# entrenamiento real (persona_trainer.start_training) se mockea siempre -
# ver tests/test_persona_trainer.py para las pruebas del modulo en si.

def test_personas_blocked_in_guest_mode():
    guest_token = client.post("/auth/guest").json()["token"]
    guest_client = TestClient(app, headers={"X-Session-Token": guest_token})

    assert guest_client.get("/personas").status_code == 403
    resp = guest_client.post("/personas", data={"name": "test"}, files=[
        ("photos", ("a.png", b"x", "image/png")),
        ("photos", ("b.png", b"x", "image/png")),
        ("photos", ("c.png", b"x", "image/png")),
    ])
    assert resp.status_code == 403


def test_create_persona_requires_a_name():
    resp = client.post("/personas", data={"name": "  "}, files=[
        ("photos", ("a.png", b"x", "image/png")),
        ("photos", ("b.png", b"x", "image/png")),
        ("photos", ("c.png", b"x", "image/png")),
    ])
    assert resp.status_code == 400


def test_create_persona_requires_at_least_three_photos():
    resp = client.post("/personas", data={"name": "test"}, files=[
        ("photos", ("a.png", b"x", "image/png")),
    ])
    assert resp.status_code == 400


def test_create_persona_starts_training_and_cleans_up_the_upload():
    with patch.object(main.persona_trainer, "start_training",
                       return_value={"status": "training", "persona": "test", "architecture": "sdxl"}) as mock_start:
        resp = client.post("/personas", data={"name": "test"}, files=[
            ("photos", ("a.png", b"x", "image/png")),
            ("photos", ("b.png", b"x", "image/png")),
            ("photos", ("c.png", b"x", "image/png")),
        ])
    assert resp.status_code == 200
    assert resp.json()["status"] == "training"
    mock_start.assert_called_once()
    call_owner, call_name, call_paths = mock_start.call_args.args
    assert call_owner == users_module.get_user(_TEST_USERNAME)["id"]  # la persona es de quien la crea
    assert call_name == "test"
    assert len(call_paths) == 3
    # el directorio temporal de subida se limpia despues de lanzar el entrenamiento
    assert not any(p.exists() for p in call_paths)


def test_create_persona_reports_no_base_model_installed():
    with patch.object(main.persona_trainer, "start_training",
                       side_effect=main.persona_trainer.NoBaseModelError("no hay SDXL instalado")):
        resp = client.post("/personas", data={"name": "test"}, files=[
            ("photos", ("a.png", b"x", "image/png")),
            ("photos", ("b.png", b"x", "image/png")),
            ("photos", ("c.png", b"x", "image/png")),
        ])
    assert resp.status_code == 400


def test_get_personas_lists_them():
    with patch.object(main.persona_trainer, "list_personas",
                       return_value=[{"name": "ana", "sdxl": {"status": "listo"}}]):
        resp = client.get("/personas")
    assert resp.status_code == 200
    assert resp.json()["personas"][0]["name"] == "ana"


# --- Vision nunca usa el perfil de chat de texto (bug real, encontrado en
# vivo 2026-09-25: con el perfil "rapido" activo, subir una foto y pedir
# describirla usaba qwen2.5:7b - sin soporte de imagenes - en vez del
# modelo de vision configurado, y Ollama lo rechazaba con "Multimodal data
# provided, but model does not support multimodal requests.").

def test_vision_chat_ignores_the_text_model_profile():
    """Aunque el perfil activo sea 'rapido' (texto, sin vision), la peticion
    a Ollama debe seguir usando el modelo de vision configurado."""
    with patch.object(main.vision_agent, "respond_with_image_stream",
                       return_value=iter(["describe algo"])) as mock_respond:
        resp = client.post("/chat", json={
            "message": "describeme esta imagen",
            "image_base64": "aWdub3JhZG8=",
            "model_profile": "rapido",
        })
    assert resp.status_code == 200
    assert resp.json()["agent_used"] == "vision"
    mock_respond.assert_called_once()
    assert "model" not in mock_respond.call_args.kwargs
    assert len(mock_respond.call_args.args) == 2  # (mensaje, imagen) - nunca un tercer arg de modelo


def test_vision_chat_stream_ignores_the_text_model_profile():
    with patch.object(main.vision_agent, "respond_with_image_stream",
                       return_value=iter(["describe algo"])) as mock_respond:
        with client.stream("POST", "/chat/stream", json={
            "message": "describeme esta imagen",
            "image_base64": "aWdub3JhZG8=",
            "model_profile": "seguridad",
        }) as resp:
            lines = [json.loads(line) for line in resp.iter_lines() if line.strip()]
    assert lines[-1]["type"] == "done"
    mock_respond.assert_called_once()
    assert "model" not in mock_respond.call_args.kwargs


# --- /image_with_face y /video_with_face deciden solos FaceID vs
# ControlNet segun si la foto adjunta tiene una cara humana (bug real,
# encontrado en vivo 2026-09-25: pedir "una mariposa similar con los
# colores invertidos" generaba una cara alucinada porque FaceID se
# aplicaba siempre, sin comprobar si la foto tenia una cara de verdad).

def test_image_with_face_uses_faceid_when_a_face_is_detected():
    with patch.object(main.model_registry, "get_edit_model", return_value=None), \
         patch.object(main.face_detect, "has_face", return_value=True), \
         patch.object(main.image_agent, "generate_with_face", return_value=b"fake-png") as mock_faceid, \
         patch.object(main.image_agent, "generate_with_controlnet") as mock_controlnet:
        resp = client.post("/image_with_face", files={"image": ("ref.png", b"x", "image/png")},
                            data={"prompt": "una mariposa"})
    data = resp.json()
    assert data["agent_used"] == "image_faceid"
    mock_faceid.assert_called_once()
    mock_controlnet.assert_not_called()


def test_image_with_face_uses_controlnet_when_no_face_is_detected():
    with patch.object(main.model_registry, "get_edit_model", return_value=None), \
         patch.object(main.face_detect, "has_face", return_value=False), \
         patch.object(main.image_agent, "generate_with_face") as mock_faceid, \
         patch.object(main.image_agent, "generate_with_controlnet", return_value=b"fake-png") as mock_controlnet:
        resp = client.post("/image_with_face", files={"image": ("mariposa.png", b"x", "image/png")},
                            data={"prompt": "una mariposa similar con los colores invertidos"})
    data = resp.json()
    assert data["agent_used"] == "image_controlnet"
    mock_controlnet.assert_called_once()
    mock_faceid.assert_not_called()


def test_video_with_face_uses_controlnet_base_frame_when_no_face_is_detected():
    with patch.object(main.model_registry, "get_edit_model", return_value=None), \
         patch.object(main.face_detect, "has_face", return_value=False), \
         patch.object(main.image_agent, "generate_with_face") as mock_faceid, \
         patch.object(main.image_agent, "generate_with_controlnet", return_value=b"fake-png") as mock_controlnet, \
         patch.object(main.video_agent, "generate_from_image", return_value=b"fake-mp4"):
        resp = client.post("/video_with_face", files={"image": ("mariposa.png", b"x", "image/png")},
                            data={"prompt": "una mariposa volando"})
    data = resp.json()
    assert data["agent_used"] == "video_faceid"
    assert data["file_url"].endswith(".mp4")
    mock_controlnet.assert_called_once()
    mock_faceid.assert_not_called()


def test_video_with_face_animates_the_edited_photo():
    # auditoria 2026-10-05: la edicion devuelve (foto, resumen) y aqui se
    # pasaba la tupla entera como fotograma
    with patch.object(main.model_registry, "get_edit_model", return_value=object()), \
         patch.object(main, "_edit_photo", return_value=(b"fake-jpg", "He hecho esto: x.")), \
         patch.object(main.prompt_writer, "video_prompt", return_value="People dance."), \
         patch.object(main.video_agent, "generate_from_image", return_value=b"fake-mp4") as mock_video:
        resp = client.post("/video_with_face", files={"image": ("yo.jpg", b"x", "image/jpeg")},
                            data={"prompt": "que baile en la playa"})
    assert resp.json()["file_url"].endswith(".mp4")
    mock_video.assert_called_once()


# --- Con el modelo de edicion (FLUX Kontext) instalado, la foto se edita en
# vez de generar una nueva con FaceID (Sergio, 2026-10-01: con su mujer "no
# nos pareciamos en nada y no hacia lo que le pedia").

def test_image_with_face_edits_the_photo_when_the_edit_model_is_installed():
    with patch.object(main.model_registry, "get_edit_model", return_value=object()), \
         patch.object(main, "_edit_photo", return_value=(b"fake-jpg", "He hecho esto: os pongo en una playa.")) as mock_edit, \
         patch.object(main.image_agent, "generate_with_face") as mock_faceid:
        resp = client.post("/image_with_face", files={"image": ("pareja.jpg", b"x", "image/jpeg")},
                            data={"prompt": "ponnos en una playa"})
    data = resp.json()
    assert data["agent_used"] == "image_edit"
    assert data["file_url"].endswith(".jpg")
    assert data["response"] == "He hecho esto: os pongo en una playa."
    mock_edit.assert_called_once_with("ponnos en una playa", b"x")
    mock_faceid.assert_not_called()


def test_chat_with_a_photo_and_a_change_request_edits_the_photo():
    """En el chat automatico la foto iba siempre al modelo de vision, que solo
    sabe comentarla: "ponnos en una playa" no hacia nada."""
    with patch.object(main.model_registry, "get_edit_model", return_value=object()), \
         patch.object(main.photo_edit, "wants_edit", return_value=True), \
         patch.object(main, "_edit_photo", return_value=(b"fake-jpg", "He hecho esto: os pongo en una playa.")) as mock_edit, \
         patch.object(main.vision_agent, "respond_with_image_stream") as mock_vision:
        with client.stream("POST", "/chat/stream", json={"message": "ponnos en una playa",
                                                         "image_base64": _tiny_jpeg_b64()}) as resp:
            lines = [json.loads(line) for line in resp.iter_lines() if line.strip()]
    assert lines[0]["agent_used"] == "image_edit"
    assert lines[-1]["file_url"].endswith(".jpg")
    assert lines[-1]["response"] == "He hecho esto: os pongo en una playa."
    assert mock_edit.call_args.args[0] == "ponnos en una playa"
    mock_vision.assert_not_called()


def _tiny_jpeg_b64(color=(120, 90, 60)):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), color).save(buf, format="JPEG")
    return base64.b64encode(buf.getvalue()).decode()


def test_a_whole_photo_conversation_keeps_editing_the_same_photo():
    """Sergio, 2026-10-06: hablar normal con el chat editando una foto: subirla,
    pedir un cambio, otro encima, deshacer... y que el historial la enseñe."""
    calls = []

    def fake_edit(request, photo, face, earlier):
        calls.append((request, photo, face, list(earlier or [])))
        return photo + b"|" + request.encode(), f"He hecho esto: {request}."

    def say(text, **extra):
        with client.stream("POST", "/chat/stream", json={"message": text, "session_id": sid, **extra}) as resp:
            return [json.loads(line) for line in resp.iter_lines() if line.strip()][-1]

    sid = None
    with patch.object(main.model_registry, "get_edit_model", return_value=object()), \
         patch.object(main, "_edit_photo", side_effect=fake_edit), \
         patch.object(main.ollama, "chat", return_value="editar"):
        first = say("ponme en la playa", image_base64=_tiny_jpeg_b64())
        sid = first["session_id"]
        second = say("ahora con un sombrero de paja")
        assert second["agent_used"] == "image_edit"
        # el segundo cambio parte del resultado del primero, con la cara de la original
        assert calls[1][1].endswith(b"|ponme en la playa") and calls[1][2] is not None
        assert calls[1][3] == ["ponme en la playa"]
        undone = say("deshaz eso")
        assert undone["file_path"] == first["file_path"] and "anterior" in undone["response"]
        third = say("mejor con gafas de sol")
        assert calls[2][1].endswith(b"|ponme en la playa")  # tras deshacer, desde la de la playa
    history = client.get(f"/sessions/{sid}").json()
    assert history[0]["media"] is not None  # la foto subida
    assert all(m["media"] for m in history if m["role"] == "assistant")  # cada resultado
    assert history[-1]["media"] == third["file_path"]


def test_chat_with_a_photo_and_a_question_still_comments_it():
    with patch.object(main.model_registry, "get_edit_model", return_value=object()), \
         patch.object(main.photo_edit, "wants_edit", return_value=False), \
         patch.object(main, "_edit_photo") as mock_edit, \
         patch.object(main, "_ensure_active_model"), \
         patch.object(main.vision_agent, "respond_with_image_stream", return_value=iter(["Un parque."])):
        resp = client.post("/chat", json={"message": "que ves?", "image_base64": "eA=="})
    assert resp.json()["agent_used"] == "vision"
    mock_edit.assert_not_called()


def test_image_from_a_simple_request_goes_through_the_prompt_writer():
    """Sergio, 2026-10-01: ordenes sencillas, sin prompts. Antes la frase en
    español llegaba tal cual al generador."""
    with patch.object(main.prompt_writer, "image_prompt",
                      return_value=("A realistic photo of a dog in fresh snow.", (832, 1216))) as mock_writer, \
         patch.object(main.image_agent, "generate", return_value=b"png") as mock_gen:
        resp = client.post("/chat", json={"message": "un perro en la nieve", "agent": "image"})
    assert resp.json()["agent_used"] == "image"
    assert mock_writer.call_args.args[2] == "un perro en la nieve"
    assert mock_gen.call_args.args[0] == "A realistic photo of a dog in fresh snow."
    assert (mock_gen.call_args.kwargs["width"], mock_gen.call_args.kwargs["height"]) == (832, 1216)


def test_edit_photo_chains_plan_kontext_and_composition():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 20, 30)).save(buf, format="PNG")
    photo = buf.getvalue()
    plan = main.photo_edit.EditPlan("Change the t-shirt to green. Keep everything else the same.", "local",
                                    "te pongo la camiseta verde")
    with patch.object(main.photo_edit, "plan_edit", return_value=[plan]) as mock_plan, \
         patch.object(main.photo_edit, "describe_photo", return_value="a man"), \
         patch.object(main.photo_edit, "verify_edit", return_value=None), \
         patch.object(main, "_free_comfyui"), \
         patch.object(main.image_agent, "edit_with_kontext", return_value=photo) as mock_kontext, \
         patch.object(main.image_agent, "upscale_bytes") as mock_upscale:
        out, done = main._edit_photo("la camiseta verde", photo)
    assert done == "Listo: te pongo la camiseta verde."
    assert mock_plan.call_args.args[2] == "la camiseta verde"
    assert mock_kontext.call_args.args[0] == plan.instruction
    mock_upscale.assert_not_called()
    assert Image.open(io.BytesIO(out)).format == "JPEG"
    assert Image.open(io.BytesIO(out)).size == (64, 48)


# --- Activar el agente desde el chat normal (sin cambiar al modo Agente) ---

def test_chat_stream_to_agent_includes_task_id_for_the_inline_card():
    with patch.object(main.opencode_client, "delegate", return_value=("Se lo he pasado al agente.", "ses_9")):
        with client.stream("POST", "/chat/stream",
                           json={"message": "usa un agente y crea hola.txt en el escritorio"}) as resp:
            lines = [json.loads(line) for line in resp.iter_lines() if line.strip()]
    start = next(e for e in lines if e["type"] == "start")  # puede ir precedido de un aviso de carga
    assert start["agent_used"] == "opencode"
    assert lines[-1]["agent_task_id"] == "ses_9"


def test_guest_cannot_trigger_the_agent_from_chat():
    guest_token = client.post("/auth/guest").json()["token"]
    guest_client = TestClient(app, headers={"X-Session-Token": guest_token})
    with patch.object(main.opencode_client, "delegate") as mock_delegate:
        resp = guest_client.post("/chat", json={"message": "agente: borra mis fotos"})
    assert resp.json()["agent_task_id"] is None
    assert "invitado" in resp.json()["response"]
    mock_delegate.assert_not_called()


# --- Dueño de las conversaciones (bug real 2026-09-25: cualquiera podia
# borrar o leer conversaciones ajenas conociendo el id) ---

def _other_user_client():
    uname = f"_test_otro_{uuid.uuid4().hex[:8]}"
    anon = TestClient(app)
    anon.post("/auth/register", json={"username": uname, "password": _TEST_PASSWORD,
                                      "registration_key": users_module.get_or_create_registration_key()})
    token = anon.post("/auth/login", json={"username": uname, "password": _TEST_PASSWORD}).json()["token"]
    return uname, TestClient(app, headers={"X-Session-Token": token})


def _my_session_with_a_message():
    owner_id = users_module.get_user(_TEST_USERNAME)["id"]
    sid = main.memory.new_session_id()
    main.memory.add_message(sid, "user", "hola", user_id=owner_id)
    return sid


def test_other_users_and_guests_cannot_touch_my_conversation():
    sid = _my_session_with_a_message()
    msg_id = main.memory.get_history(sid, limit=5)[0]["id"]
    uname, other = _other_user_client()
    guest = TestClient(app, headers={"X-Session-Token": client.post("/auth/guest").json()["token"]})
    try:
        # otro usuario: "no existe" (404); invitado: ni siquiera puede usar esas rutas (403)
        for c, code in ((other, 404), (guest, 403)):
            assert c.delete(f"/sessions/{sid}").status_code == code
            assert c.get(f"/sessions/{sid}").status_code == code
            assert c.delete(f"/sessions/{sid}/messages/{msg_id}").status_code == code
        assert main.memory.session_owner(sid) is not None  # sigue ahi
    finally:
        users_module.delete_user(uname)

    assert client.delete(f"/sessions/{sid}").status_code == 200
    assert main.memory.session_owner(sid) is None


def test_chat_with_someone_elses_session_id_starts_a_new_one():
    sid = _my_session_with_a_message()
    uname, other = _other_user_client()
    try:
        with patch.object(main.opencode_client, "delegate", return_value=("ok", None)):
            resp = other.post("/chat", json={"message": "agente: hola", "session_id": sid})
        assert resp.json()["session_id"] != sid
    finally:
        users_module.delete_user(uname)
        client.delete(f"/sessions/{sid}")


# --- Documentos adjuntos con el clip (solo para esa conversacion) ---

def test_attach_doc_creates_conversation_and_reaches_the_model():
    resp = client.post("/sessions/docs", files={"file": ("nota.txt", "El codigo secreto es 4711.".encode(), "text/plain")})
    assert resp.status_code == 200
    sid = resp.json()["session_id"]
    assert client.get(f"/sessions/{sid}/docs").json() == [{"name": "nota.txt", "chars": 26}]

    seen = {}

    def fake_respond(message, history, *args, **kwargs):
        seen["message"] = message
        return "El codigo es 4711.", []

    with patch.object(main.text_agent, "respond_with_tools", side_effect=fake_respond), \
         patch.object(main, "_ensure_active_model"):
        r = client.post("/chat", json={"message": "¿cual es el codigo?", "agent": "text",
                                       "session_id": sid, "verify": False})
    assert r.status_code == 200
    assert "4711" in seen["message"] and "nota.txt" in seen["message"]
    # en el historial queda el mensaje tal cual, sin el documento pegado
    history = client.get(f"/sessions/{sid}").json()
    assert history[0]["agent"] == "adjunto" and "nota.txt" in history[0]["content"]
    assert history[1]["content"] == "¿cual es el codigo?"

    uname, other = _other_user_client()
    try:
        assert other.get(f"/sessions/{sid}/docs").json() == []
    finally:
        users_module.delete_user(uname)
    client.delete(f"/sessions/{sid}")
    assert client.get(f"/sessions/{sid}/docs").json() == []


def test_attach_doc_rejects_bad_files_and_guests():
    assert client.post("/sessions/docs", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 400
    guest = TestClient(app, headers={"X-Session-Token": client.post("/auth/guest").json()["token"]})
    assert guest.post("/sessions/docs", files={"file": ("a.txt", b"hola", "text/plain")}).status_code == 403


def test_generation_frees_every_loaded_ollama_model_first():
    """Bug real 2026-09-25: el modelo del chat ocupaba la VRAM y una imagen
    tardaba ~400s. Antes de cada generacion se descargan todos."""
    with patch.object(main.ollama, "running_models", return_value=["qwen2.5:7b", "qwen3-coder:30b-cpu"]), \
         patch.object(main.opencode_client, "busy_session_ids", return_value=[]), \
         patch.object(main.ollama, "unload") as mock_unload:
        main.comfyui_client.before_submit()
    assert [c.args[0] for c in mock_unload.call_args_list] == ["qwen2.5:7b", "qwen3-coder:30b-cpu"]


def test_generation_keeps_the_agent_models_while_a_task_is_running():
    with patch.object(main.ollama, "running_models",
                      return_value=["qwen2.5:7b", "qwen3:8b", "qwen3-coder:30b-cpu", "qwen2.5vl:7b-cpu"]), \
         patch.object(main.opencode_client, "busy_session_ids", return_value=["ses_1"]), \
         patch.object(main.ollama, "unload") as mock_unload:
        main.comfyui_client.before_submit()
    # los dos modelos del agente se quedan; el del chat y el de vision no
    assert [c.args[0] for c in mock_unload.call_args_list] == ["qwen2.5:7b", "qwen2.5vl:7b-cpu"]


def _run_before_task(agent, loaded):
    import threading as _threading
    ran = _threading.Event()
    with patch.object(main.ollama, "running_models", return_value=loaded), \
         patch.object(main.ollama, "unload") as mock_unload, \
         patch.object(main.comfyui_client, "free_memory", side_effect=lambda *_: ran.set()) as mock_free:
        main.opencode_client.before_task(agent)
        assert ran.wait(5)
    return [c.args[0] for c in mock_unload.call_args_list], mock_free.call_count


def test_powerful_agent_task_frees_ram_and_comfyui():
    with patch.object(main.opencode_client, "busy_session_ids", return_value=[]):
        unloaded, frees = _run_before_task("chati-potente", ["qwen3:8b", "qwen2.5vl:7b-cpu", "qwen3-coder:30b-cpu"])
    assert unloaded == ["qwen2.5vl:7b-cpu"] and frees == 1


def test_fast_agent_task_makes_room_on_the_gpu():
    """El agente rapido usa el mismo modelo que el chat (qwen3:8b, desde el
    2026-09-29): se queda cargado; fuera lo demas (vision) para que quepa."""
    with patch.object(main.opencode_client, "busy_session_ids", return_value=[]):
        unloaded, frees = _run_before_task("chati", ["qwen3:8b", "qwen2.5vl:7b-gpu", "nomic-embed-text:latest"])
    assert unloaded == ["qwen2.5vl:7b-gpu"] and frees == 1


def test_agent_tasks_use_the_fast_agent_unless_potente_is_asked():
    with patch.object(main.opencode_client, "start_task", return_value="ses_1") as mock_start:
        client.post("/agent/tasks", json={"task": "crea un archivo"})
        client.post("/agent/tasks", json={"task": "refactoriza el proyecto", "potente": True})
    assert [c.args[2] for c in mock_start.call_args_list] == ["chati", "chati-potente"]


def test_chat_stream_warns_when_the_model_has_to_be_loaded():
    def fake_stream(*args, **kwargs):
        yield "hola"
    with patch.object(main.ollama, "running_models", return_value=[]), \
         patch.object(main, "_ensure_active_model"), \
         patch.object(main.text_agent, "respond_with_tools_stream", side_effect=fake_stream):
        with client.stream("POST", "/chat/stream", json={"message": "hola", "model_profile": "rapido", "verify": False}) as resp:
            lines = [json.loads(line) for line in resp.iter_lines() if line.strip()]
    statuses = [e for e in lines if e["type"] == "status"]
    assert len(statuses) == 1  # router y respuesta usan el mismo modelo: un solo aviso
    assert "Cargando el modelo" in statuses[0]["text"]
    client.delete(f"/sessions/{lines[-1]['session_id']}")


def test_chat_stream_says_nothing_when_the_model_is_already_loaded():
    def fake_stream(*args, **kwargs):
        yield "hola"
    with patch.object(main.ollama, "running_models", return_value=["qwen3:8b"]), \
         patch.object(main, "_ensure_active_model"), \
         patch.object(main.text_agent, "respond_with_tools_stream", side_effect=fake_stream):
        with client.stream("POST", "/chat/stream", json={"message": "hola", "model_profile": "rapido", "verify": False}) as resp:
            lines = [json.loads(line) for line in resp.iter_lines() if line.strip()]
    assert not [e for e in lines if e["type"] == "status"]
    client.delete(f"/sessions/{lines[-1]['session_id']}")


# --- Preparar los modelos del modo elegido (desplegable de la izquierda) ---

def _wait_prepare_done(timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get("/models/prepare").json()
        if st["state"] != "preparing":
            return st
        time.sleep(0.05)
    raise AssertionError("la preparacion no termino")


def test_prepare_rejects_unknown_modes():
    assert client.post("/models/prepare", json={"mode": "cocina"}).status_code == 400


def test_prepare_chat_mode_loads_the_default_profile_model_and_frees_comfyui():
    with patch.object(main.ollama, "preload") as mock_preload, \
         patch.object(main.ollama, "running_models", return_value=[]), \
         patch.object(main.comfyui_client, "free_memory") as mock_free:
        resp = client.post("/models/prepare", json={"mode": "texto"})
        assert resp.json()["state"] == "preparing"
        st = _wait_prepare_done()
    assert st["state"] == "ready"
    assert "qwen3:8b" in st["label"]  # perfil por defecto (rapido), no el 30B
    mock_preload.assert_called_once_with("qwen3:8b")
    mock_free.assert_called_once()


def test_prepare_image_mode_warms_up_with_a_tiny_image():
    with patch.object(main.image_agent, "generate", return_value=b"png") as mock_gen:
        client.post("/models/prepare", json={"mode": "image"})
        st = _wait_prepare_done()
    assert st["state"] == "ready"
    assert "imagenes" in st["label"]
    assert mock_gen.call_args.kwargs["width"] == 256


def test_prepare_reports_errors_and_only_the_last_request_counts():
    import threading as _threading
    release = _threading.Event()

    def slow_preload(model, *a, **k):
        release.wait(5)

    with patch.object(main.ollama, "preload", side_effect=slow_preload), \
         patch.object(main.ollama, "running_models", return_value=[]), \
         patch.object(main.comfyui_client, "free_memory"), \
         patch.object(main.image_agent, "generate", side_effect=RuntimeError("no hay modelo")):
        client.post("/models/prepare", json={"mode": "chat"})     # se queda cargando...
        client.post("/models/prepare", json={"mode": "image"})    # ...y se cambia de modo
        st = _wait_prepare_done()
        release.set()
        time.sleep(0.2)
    final = client.get("/models/prepare").json()
    assert st["mode"] == "image" and st["state"] == "error" and "no hay modelo" in st["detail"]
    assert final["mode"] == "image" and final["state"] == "error"  # el "chat" que acabo tarde no lo pisa


def _prepare_and_collect_unloads(body, loaded, busy=()):
    with patch.object(main.ollama, "running_models", return_value=loaded), \
         patch.object(main.ollama, "unload") as mock_unload, \
         patch.object(main.ollama, "preload"), \
         patch.object(main.opencode_client, "busy_session_ids", return_value=list(busy)), \
         patch.object(main.comfyui_client, "free_memory") as mock_free, \
         patch.object(main.comfyui_client, "user_queue", return_value=(0, 0)), \
         patch.object(main.image_agent, "generate", return_value=b"png"), \
         patch.object(main.video_agent, "generate", return_value=b"mp4"):
        client.post("/models/prepare", json=body)
        assert _wait_prepare_done()["state"] == "ready"
    return [c.args[0] for c in mock_unload.call_args_list], mock_free.call_count


def test_switching_to_chat_unloads_the_agent_and_vision_models():
    unloaded, _ = _prepare_and_collect_unloads(
        {"mode": "texto"},
        ["qwen3:8b", "qwen3-coder:30b-cpu", "qwen2.5vl:7b-cpu", "nomic-embed-text:latest"])
    assert unloaded == ["qwen3-coder:30b-cpu", "qwen2.5vl:7b-cpu"]


def test_switching_modes_keeps_the_agent_model_while_a_task_runs():
    unloaded, _ = _prepare_and_collect_unloads(
        {"mode": "chat"}, ["qwen3:8b", "qwen3-coder:30b-cpu", "qwen2.5vl:7b-cpu"], busy=["ses_1"])
    assert unloaded == ["qwen2.5vl:7b-cpu"]


def test_switching_image_model_frees_the_previous_one_but_repeating_does_not():
    main._last_comfy_warmup = None
    _, frees_first = _prepare_and_collect_unloads({"mode": "image", "image_model": "flux:a"}, [])
    _, frees_same = _prepare_and_collect_unloads({"mode": "image", "image_model": "flux:a"}, [])
    _, frees_other = _prepare_and_collect_unloads({"mode": "video"}, [])
    assert (frees_first, frees_same, frees_other) == (0, 0, 1)


# --- Tareas en marcha al cambiar de modo (finalizar / esperar / segundo plano) ---

def test_pending_work_lists_working_and_waiting_tasks_and_generations():
    with patch.object(main.opencode_client, "busy_session_ids", return_value=["ses_1"]), \
         patch.object(main.opencode_client, "waiting_sessions", return_value={"ses_viejo": "question"}), \
         patch.object(main.opencode_client, "task_summary", side_effect=lambda b, sid: f"tarea {sid}"), \
         patch.object(main.comfyui_client, "user_jobs",
                      return_value=[{"id": "p1", "state": "running"}, {"id": "p2", "state": "queued"}]):
        data = client.get("/work/pending").json()
    assert data["agent_tasks"] == [{"session_id": "ses_1", "title": "tarea ses_1"}]
    assert data["waiting_tasks"] == [{"session_id": "ses_viejo", "title": "tarea ses_viejo", "reason": "question"}]
    assert (data["generations_running"], data["generations_queued"], data["any"]) == (1, 1, True)


def test_a_task_only_waiting_for_the_user_does_not_block_mode_changes():
    with patch.object(main.opencode_client, "busy_session_ids", return_value=[]), \
         patch.object(main.opencode_client, "waiting_sessions", return_value={"ses_viejo": "question"}), \
         patch.object(main.opencode_client, "task_summary", return_value="x"), \
         patch.object(main.comfyui_client, "user_jobs", return_value=[]):
        data = client.get("/work/pending").json()
    assert data["any"] is False and len(data["waiting_tasks"]) == 1


def test_stop_single_agent_task_and_generation():
    with patch.object(main.opencode_client, "stop_task") as mock_stop_task, \
         patch.object(main.comfyui_client, "stop_job") as mock_stop_job:
        assert client.post("/work/agent/ses_viejo/stop").status_code == 200
        assert client.post("/work/generation/p2/stop").status_code == 200
    assert mock_stop_task.call_args.args[1] == "ses_viejo"
    assert mock_stop_job.call_args.args[1] == "p2"


def test_pending_work_is_empty_when_nothing_runs():
    with patch.object(main.opencode_client, "busy_session_ids", return_value=[]), \
         patch.object(main.opencode_client, "waiting_sessions", return_value={}), \
         patch.object(main.comfyui_client, "user_jobs", return_value=[]):
        assert client.get("/work/pending").json()["any"] is False


def test_stop_work_aborts_agent_tasks_and_comfyui():
    with patch.object(main.opencode_client, "busy_session_ids", return_value=["ses_1", "ses_2"]), \
         patch.object(main.opencode_client, "abort") as mock_abort, \
         patch.object(main.comfyui_client, "stop_owned") as mock_stop:
        assert client.post("/work/stop").status_code == 200
    assert [c.args[1] for c in mock_abort.call_args_list] == ["ses_1", "ses_2"]
    mock_stop.assert_called_once()


def test_stopping_only_touches_your_own_generations():
    # auditoria 2026-10-05: el boton de parar de cualquiera (invitados incluidos)
    # cortaba lo que estuviera generando ComfyUI, fuera de quien fuera
    guest = TestClient(app, headers={"X-Session-Token": client.post("/auth/guest").json()["token"]})
    jobs = [{"id": "ajeno", "state": "running"}]
    with main.comfyui_client._owners_lock:
        main.comfyui_client._owners["ajeno"] = "otro-usuario"
    try:
        with patch.object(main.comfyui_client, "user_jobs", return_value=jobs), \
             patch.object(main.comfyui_client, "stop_job") as mock_stop, \
             patch.object(main.opencode_client, "busy_session_ids", return_value=[]), \
             patch.object(main.opencode_client, "waiting_sessions", return_value={}):
            assert guest.post("/cancel").status_code == 200
            assert client.post("/cancel").status_code == 200
            assert client.post("/work/generation/ajeno/stop").status_code == 403
            assert client.post("/work/stop").status_code == 200
            assert client.get("/work/pending").json()["generations"] == []
        mock_stop.assert_not_called()
    finally:
        with main.comfyui_client._owners_lock:
            main.comfyui_client._owners.pop("ajeno", None)


def test_changing_mode_never_frees_comfyui_in_the_middle_of_a_generation():
    with patch.object(main.comfyui_client, "user_queue", return_value=(1, 0)), \
         patch.object(main.comfyui_client, "free_memory") as mock_free:
        main._free_comfyui()
    mock_free.assert_not_called()


def test_automatic_mode_with_the_quality_profile_swaps_the_models_at_once():
    with patch.object(main.ollama, "running_models", return_value=["qwen3:8b", "qwen2.5vl:7b-cpu"]), \
         patch.object(main.ollama, "unload") as mock_unload, \
         patch.object(main.ollama, "preload") as mock_preload, \
         patch.object(main.opencode_client, "busy_session_ids", return_value=[]), \
         patch.object(main.comfyui_client, "free_memory"):
        client.post("/models/prepare", json={"mode": "chat", "model_profile": "bueno"})
        st = _wait_prepare_done()
    assert st["state"] == "ready" and "qwen3-coder:30b-cpu" in st["label"]
    assert [c.args[0] for c in mock_unload.call_args_list] == ["qwen2.5vl:7b-cpu"]
    assert [c.args[0] for c in mock_preload.call_args_list] == ["qwen3:8b", "qwen3-coder:30b-cpu"]



# --- Valorar la dificultad antes de usar el agente rapido ---

@pytest.mark.assess
def test_complex_task_asks_which_model_instead_of_starting():
    with patch.object(main.router, "assess_agent_task", return_value={"complex": True, "reason": "hay que programar"}),          patch.object(main.opencode_client, "start_task") as mock_start:
        data = client.post("/agent/tasks", json={"task": "añade un endpoint"}).json()
    assert data == {"needs_choice": True, "task": "añade un endpoint", "reason": "hay que programar", "attachments": []}
    mock_start.assert_not_called()


@pytest.mark.assess
def test_after_choosing_or_with_potente_there_is_no_assessment():
    with patch.object(main.router, "assess_agent_task") as mock_assess,          patch.object(main.opencode_client, "start_task", return_value="ses_1") as mock_start:
        client.post("/agent/tasks", json={"task": "x", "confirmed": True})
        client.post("/agent/tasks", json={"task": "x", "potente": True})
    mock_assess.assert_not_called()
    assert [c.args[2] for c in mock_start.call_args_list] == ["chati", "chati-potente"]


@pytest.mark.assess
def test_simple_task_starts_right_away_and_a_failing_assessment_never_blocks():
    with patch.object(main.opencode_client, "start_task", return_value="ses_1"):
        with patch.object(main.router, "assess_agent_task", return_value={"complex": False, "reason": ""}):
            assert client.post("/agent/tasks", json={"task": "crea hola.txt"}).json() == {"session_id": "ses_1"}
        with patch.object(main.router, "assess_agent_task", side_effect=RuntimeError("ollama caido")):
            assert client.post("/agent/tasks", json={"task": "crea hola.txt"}).json() == {"session_id": "ses_1"}


@pytest.mark.assess
def test_chat_delegation_of_a_complex_task_offers_the_choice():
    with patch.object(main.router, "assess_agent_task", return_value={"complex": True, "reason": "hay que depurar"}),          patch.object(main.opencode_client, "delegate") as mock_delegate:
        data = client.post("/chat", json={"message": "agente: arregla el bug del login"}).json()
    mock_delegate.assert_not_called()
    assert data["agent_task_id"] is None
    assert data["agent_choice"] == {"task": "agente: arregla el bug del login", "reason": "hay que depurar"}
    client.delete(f"/sessions/{data['session_id']}")


def test_agent_follow_up_goes_to_the_same_task():
    with patch.object(main.opencode_client, "continue_task") as mock_continue:
        assert client.post("/agent/tasks/ses_1/message", json={"text": "  ahora en Trabajo "}).status_code == 200
        assert client.post("/agent/tasks/ses_1/message", json={"text": "   "}).status_code == 400
    assert mock_continue.call_args.args[1:] == ("ses_1", "ahora en Trabajo", None)  # None: el mismo agente
    guest = TestClient(app, headers={"X-Session-Token": client.post("/auth/guest").json()["token"]})
    assert guest.post("/agent/tasks/ses_1/message", json={"text": "x"}).status_code == 403


def test_agent_shortcuts_per_user_and_blocked_for_guests():
    assert client.get("/agent/shortcuts").status_code == 200
    saved = client.put("/agent/shortcuts", json={"shortcuts": [{"name": "Mi atajo", "task": "haz algo"}]}).json()
    assert saved == [{"name": "Mi atajo", "task": "haz algo", "potente": False}]
    assert client.get("/agent/shortcuts").json() == saved
    assert client.put("/agent/shortcuts", json={"shortcuts": [{"name": "", "task": "x"}]}).status_code == 400
    uname, other = _other_user_client()
    try:
        assert other.get("/agent/shortcuts").status_code == 403  # sin permiso para usar el ordenador
        assert client.put(f"/auth/users/{uname}/pc_access", json={"allowed": True}).status_code == 200
        assert other.get("/agent/shortcuts").json()[0]["name"] == "Ordenar Descargas"  # los suyos, no los mios
    finally:
        users_module.delete_user(uname)
    guest = TestClient(app, headers={"X-Session-Token": client.post("/auth/guest").json()["token"]})
    assert guest.get("/agent/shortcuts").status_code == 403


def test_llm_proxy_only_for_opencode_and_cleans_streamed_tool_calls():
    from unittest.mock import MagicMock
    chunk = {"choices": [{"delta": {"tool_calls": [{"function": {"name": "bash",
             "arguments": '{"command": "dir", "workdir": null}'}}]}}]}
    upstream = MagicMock(status_code=200, headers={"content-type": "text/event-stream"})
    upstream.iter_lines.return_value = [b"data: " + json.dumps(chunk).encode(), b"", b"data: [DONE]"]
    # sin la clave de OpenCode, nada (antes cualquiera podia usar los modelos por aqui)
    assert TestClient(app).post("/llm/v1/chat/completions", json={"model": "m"}).status_code == 401
    opencode = TestClient(app, headers={"Authorization": f"Bearer {main.opencode_client.AUTH[1]}"})
    with patch.object(main.requests, "request", return_value=upstream) as mock_req:
        resp = opencode.post("/llm/v1/chat/completions", json={"model": "m", "stream": True})
    assert resp.status_code == 200  # sin sesion de usuario: es la pasarela de OpenCode
    assert mock_req.call_args.args[1].endswith("/v1/chat/completions")
    assert '\\"workdir\\"' not in resp.text and "[DONE]" in resp.text


def test_llm_proxy_only_reaches_the_chat_api():
    # con "../api/pull" se llegaba a la API de administrar modelos de Ollama
    opencode = TestClient(app, headers={"Authorization": f"Bearer {main.opencode_client.AUTH[1]}"})
    with patch.object(main.requests, "request") as mock_req:
        for bad in ("/llm/v1/%2e%2e/api/pull", "/llm/v1/..%2Fapi%2Fcreate", "/llm/v1/chat%3Fx"):
            assert "Ruta no permitida" in opencode.post(bad, json={"model": "m"}).text, bad
    mock_req.assert_not_called()


# --- Tarea en directo (n.º 3 del roadmap): eventos de OpenCode -> SSE ---

def _live_events(opencode_lines):
    from unittest.mock import MagicMock
    upstream = MagicMock()
    upstream.__enter__.return_value = upstream
    upstream.iter_lines.return_value = opencode_lines
    snaps = iter(range(100))
    with patch.object(main, "_task_view", side_effect=lambda b, sid: {"n": next(snaps)}), \
         patch.object(main.requests, "get", return_value=upstream):
        resp = client.get("/agent/tasks/ses_1/live")
    return [ln for ln in resp.text.split("\n\n") if ln.strip()]


def test_live_sends_a_snapshot_per_event_of_this_task_only():
    blocks = _live_events([
        b'data: {"type":"session.status","properties":{"sessionID":"ses_1"}}',
        b'data: {"type":"session.status","properties":{"sessionID":"ses_otra"}}',
        b'data: {"type":"server.heartbeat","properties":{}}',
        b'data: {"type":"session.idle","properties":{"sessionID":"ses_1"}}',
    ])
    assert blocks == ['data: {"n": 0}', 'data: {"n": 1}', ": ping", 'data: {"n": 2}', "event: end\ndata: {}"]


def test_live_throttles_word_by_word_text():
    deltas = [b'data: {"type":"message.part.delta","properties":{"sessionID":"ses_1","delta":"x"}}'] * 20
    blocks = _live_events(deltas)
    snapshots = [b for b in blocks if b.startswith("data:")]
    assert 2 <= len(snapshots) < 10  # el inicial + alguno, no uno por palabra


def test_live_ends_cleanly_when_opencode_is_down():
    with patch.object(main, "_task_view", return_value={"n": 0}), \
         patch.object(main.requests, "get", side_effect=requests.ConnectionError("caido")):
        text = client.get("/agent/tasks/ses_1/live").text
    assert text.strip().endswith("event: end\ndata: {}")


def test_live_shows_text_while_it_is_being_written():
    from unittest.mock import MagicMock
    upstream = MagicMock()
    upstream.__enter__.return_value = upstream
    delta = lambda t: ('data: {"type":"message.part.delta","properties":{"sessionID":"ses_1","partID":"p1",'
                       '"field":"text","delta":"' + t + '"}}').encode()
    upstream.iter_lines.return_value = [
        delta("Hola "), delta("mundo"),
        b'data: {"type":"session.status","properties":{"sessionID":"ses_1","status":{"type":"busy"}}}',
        b'data: {"type":"message.part.updated","properties":{"sessionID":"ses_1","part":{"id":"p1","type":"text","text":"Hola mundo"}}}',
    ]
    with patch.object(main, "_task_view", side_effect=lambda b, sid: {"status": "busy", "steps": []}), \
         patch.object(main.requests, "get", return_value=upstream), \
         patch.object(main, "LIVE_THROTTLE", 0):
        blocks = [json.loads(b[6:]) for b in client.get("/agent/tasks/ses_1/live").text.split("\n\n") if b.startswith("data: {")]
    streamed = [b.get("streaming_text") for b in blocks]
    assert streamed == [None, "Hola ", "Hola mundo", "Hola mundo", None]  # al guardarse deja de ir aparte



# --- Adjuntos a una tarea del agente (n.º 4 del roadmap) ---

def test_attachments_are_copied_images_described_and_listed_in_the_task(tmp_path, monkeypatch):
    import io
    from PIL import Image
    monkeypatch.setattr(main.agent_attachments, "ATTACH_ROOT", tmp_path)
    png = io.BytesIO()
    Image.new("RGB", (40, 30), (0, 128, 255)).save(png, format="PNG")
    with patch.object(main, "_ensure_active_model"),          patch.object(main.vision_agent, "respond_with_image_stream", return_value=iter(["una playa azul"])):
        up = client.post("/agent/attachments", files=[
            ("files", ("factura.txt", b"Total: 42 euros", "text/plain")),
            ("files", ("foto.png", png.getvalue(), "image/png")),
        ]).json()
    names = {f["name"]: f for f in up["files"]}
    assert (tmp_path / names["factura.txt"]["path"].split("/")[-2] / "factura.txt").read_bytes() == b"Total: 42 euros"
    assert names["factura.txt"]["description"] is None
    assert names["foto.png"]["description"] == "una playa azul"

    with patch.object(main.opencode_client, "start_task", return_value="ses_1") as mock_start:
        client.post("/agent/tasks", json={"task": "ordena esto", "attachments": up["files"]})
    sent = mock_start.call_args.args[1]
    assert sent.startswith("ordena esto")
    assert names["foto.png"]["path"] in sent and "se ve: una playa azul" in sent


def test_attachments_limits_and_guests():
    too_many = [("files", (f"a{i}.txt", b"x", "text/plain")) for i in range(11)]
    assert client.post("/agent/attachments", files=too_many).status_code == 400
    guest = TestClient(app, headers={"X-Session-Token": client.post("/auth/guest").json()["token"]})
    assert guest.post("/agent/attachments", files=[("files", ("a.txt", b"x", "text/plain"))]).status_code == 403


def test_code_mode_tasks_go_to_the_project_with_the_chosen_agent(tmp_path):
    """Modo Codigo: planificar/construir en la carpeta del proyecto, y "Hacerlo"
    sigue la misma tarea con el agente de construir."""
    with patch.object(main.opencode_client, "start_task", return_value="ses_c") as mock_start:
        r = client.post("/agent/tasks", json={"task": "revisa la estructura", "project": str(tmp_path), "plan": True})
        assert r.json() == {"session_id": "ses_c"}
        assert mock_start.call_args.args[1:] == ("revisa la estructura", "chati-code-plan", str(tmp_path), None)
        client.post("/agent/tasks", json={"task": "hazlo", "project": str(tmp_path), "rapido": True})
        assert mock_start.call_args.args[2:] == ("chati-code", str(tmp_path), main.CONFIG["opencode"]["model"])
        assert client.post("/agent/tasks", json={"task": "x", "project": str(tmp_path / "no-existe")}).status_code == 400
    with patch.object(main.opencode_client, "continue_task") as mock_continue:
        client.post("/agent/tasks/ses_c/message", json={"text": "adelante", "code_mode": "build"})
    assert mock_continue.call_args.args[1:] == ("ses_c", "adelante", "chati-code")


def test_code_projects_are_remembered_and_git_is_only_activated_on_request(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "README.md").write_text("x")
    info = client.post("/code/projects", json={"path": str(tmp_path)}).json()
    assert info["exists"] and not info["git"] and info["entries"] == ["README.md", "src/"]
    assert client.get("/code/projects").json()["projects"][0]["path"] == str(tmp_path)
    assert not (tmp_path / ".git").exists()
    assert client.post("/code/git_init", json={"path": str(tmp_path)}).json()["git"]
    assert client.post("/code/projects", json={"path": str(tmp_path / "nada")}).status_code == 400



# --- Auditoria 2026-09-29: puerta de entrada, permisos y archivos generados ---

def test_requests_to_another_host_are_rejected():
    """DNS rebinding: una web que apunta su dominio a 127.0.0.1 manda su Host."""
    evil = TestClient(app, base_url="http://evil.example:8899")
    assert evil.get("/health").status_code == 400
    assert evil.post("/auth/guest").status_code == 400
    assert TestClient(app, base_url="http://127.0.0.1:8899").get("/health").status_code == 200
    assert TestClient(app, base_url="http://localhost:8899").get("/health").status_code == 200


def test_changes_from_another_origin_are_rejected():
    local = TestClient(app, base_url="http://127.0.0.1:8899")
    assert local.post("/auth/guest", headers={"Origin": "http://evil.example"}).status_code == 403
    assert local.post("/auth/guest", headers={"Origin": "http://127.0.0.1:8188"}).status_code == 403  # otro puerto
    assert local.post("/auth/guest", headers={"Origin": "http://127.0.0.1:8899"}).status_code == 200
    assert local.post("/auth/guest").status_code == 200  # sin Origin: programas, no navegadores


def test_security_headers_are_sent():
    resp = client.get("/health")
    assert "script-src 'self'" in resp.headers["content-security-policy"]
    assert resp.headers["x-frame-options"] == "DENY"


def test_session_cookie_is_httponly_and_the_url_token_is_gone():
    local = TestClient(app, base_url="http://127.0.0.1:8899")
    resp = local.post("/auth/guest")
    cookie = resp.headers["set-cookie"].lower()
    assert "chati_session=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert local.get("/auth/me").json()["role"] == "guest"  # la cookie basta
    token = resp.json()["token"]
    assert TestClient(app).get(f"/auth/me?session={token}").status_code == 401  # ya no vale en la URL


def test_user_without_pc_access_cannot_touch_the_computer():
    uname, other = _other_user_client()
    try:
        assert other.get("/auth/me").json()["pc_access"] is False
        assert other.get("/agent/tasks").status_code == 403
        assert other.post("/code/projects", json={"path": "C:/"}).status_code == 403
        assert main._execute_tool("leer_archivo", {"ruta": "C:/Windows/win.ini"}, "s")[0].startswith("Esta cuenta no")
        assert main._execute_tool("ejecutar_python", {"codigo": "print(1)"}, "s")[0].startswith("Esta cuenta no")
        names = {t["function"]["name"] for t in main._tools_for("text", pc_access=False)}
        assert not names & main.PC_TOOLS and "buscar_en_memoria" in names
        # el admin se lo da, y ya puede
        assert client.put(f"/auth/users/{uname}/pc_access", json={"allowed": True}).status_code == 200
        assert other.get("/auth/me").json()["pc_access"] is True
        assert other.put(f"/auth/users/{uname}/pc_access", json={"allowed": True}).status_code == 403  # no se lo da solo
    finally:
        users_module.delete_user(uname)
    guest = TestClient(app, headers={"X-Session-Token": client.post("/auth/guest").json()["token"]})
    assert guest.get("/auth/me").json()["pc_access"] is False
    assert guest.get("/cv").status_code == 403  # invitado: solo lo que esta en su lista


def test_generated_files_are_encrypted_and_private():
    import media_store
    me = main.auth_sessions.get_session(_test_token)
    name = media_store.save(b"\x89PNG-contenido-privado", ".png", me["dek"])
    enc = media_store.media_dir() / (name + ".enc")
    assert b"contenido-privado" not in enc.read_bytes()  # cifrado en disco
    got = client.get(f"/media/{name}")
    assert got.status_code == 200 and got.content == b"\x89PNG-contenido-privado"
    assert got.headers["content-type"] == "image/png"
    part = client.get(f"/media/{name}", headers={"Range": "bytes=0-3"})
    assert part.status_code == 206 and part.content == b"\x89PNG"
    uname, other = _other_user_client()
    try:
        assert other.get(f"/media/{name}").status_code == 404  # de otro: no se puede abrir
    finally:
        users_module.delete_user(uname)
    guest = TestClient(app, headers={"X-Session-Token": client.post("/auth/guest").json()["token"]})
    assert guest.get(f"/media/{name}").status_code == 404
    assert client.get("/media/..%2F..%2Fusers.db").status_code == 404
    assert client.get("/outputs/loquesea.png").status_code == 404  # la carpeta ya no se sirve tal cual


def test_login_is_locked_after_repeated_failures():
    import rate_limit
    uname = f"_test_lock_{uuid.uuid4().hex[:8]}"
    anon = TestClient(app)
    try:
        for _ in range(rate_limit.login.max_failures):
            assert anon.post("/auth/login", json={"username": uname, "password": "mal"}).status_code == 401
        assert anon.post("/auth/login", json={"username": uname, "password": "mal"}).status_code == 429
    finally:
        rate_limit.login.succeed(uname.lower())


def test_my_apps_routes_after_moving_them_out_of_main():
    """routes_apps.py: las mismas rutas, solo para usuarios registrados."""
    for path in ("/jobs", "/shopping", "/deep", "/jobs/status", "/shopping/status", "/deep/status"):
        assert client.get(path).status_code == 200, path
    with patch.object(main.routes_apps.deep_search, "start") as mock_start:
        assert client.post("/deep/search", json={"consulta": "casas en Valladolid"}).json() == {"ok": True}
    assert mock_start.call_args.args[3] == "casas en Valladolid"
    guest = TestClient(app, headers={"X-Session-Token": client.post("/auth/guest").json()["token"]})
    assert guest.get("/deep").status_code == 403


def test_a_late_chat_load_is_unloaded_when_already_in_image_mode():
    """Automatico al abrir + Imagen enseguida: la carga del chat acababa
    despues y dejaba qwen3 en la GPU a mitad de la imagen (2026-09-29)."""
    release = threading.Event()

    def slow_preload(model, *a, **k):
        release.wait(5)

    with patch.object(main.ollama, "preload", side_effect=slow_preload),          patch.object(main.ollama, "running_models", return_value=["qwen3:8b"]),          patch.object(main.ollama, "unload") as mock_unload,          patch.object(main.opencode_client, "busy_session_ids", return_value=[]),          patch.object(main.comfyui_client, "free_memory"),          patch.object(main.comfyui_client, "user_queue", return_value=(0, 0)),          patch.object(main.image_agent, "generate", return_value=b"png"):
        client.post("/models/prepare", json={"mode": "chat"})   # se queda cargando...
        client.post("/models/prepare", json={"mode": "image"})  # ...y se pasa a Imagen
        _wait_prepare_done()
        mock_unload.reset_mock()
        release.set()  # la carga del chat termina tarde
        for _ in range(50):
            if mock_unload.called:
                break
            time.sleep(0.05)
    assert mock_unload.called, "lo que cargo el chat tarde tiene que salir de la GPU"
    assert client.get("/models/prepare").json()["mode"] == "image"


def test_password_recovery_does_not_reveal_which_users_exist():
    anon = TestClient(app)
    q1 = anon.get("/auth/security-question/no_existe_nadie_asi").json()["security_question"]
    assert q1 == anon.get("/auth/security-question/No_Existe_Nadie_Asi").json()["security_question"]  # estable
    resp = anon.post("/auth/reset-password", json={"username": "no_existe_nadie_asi",
                                                   "security_answer": "x", "new_password": "otra-larga-1"})
    assert resp.status_code == 400 and resp.json()["detail"] == "Respuesta incorrecta."
    import rate_limit
    rate_limit.password_reset.succeed("no_existe_nadie_asi")


def test_agent_tasks_belong_to_whoever_started_them():
    """Auditoria 2026-09-30: cualquier usuario con permiso para usar el
    ordenador veia, seguia, aprobaba o deshacia las tareas de los demas."""
    uname, other = _other_user_client()
    try:
        client.put(f"/auth/users/{uname}/pc_access", json={"allowed": True})
        with patch.object(main.opencode_client, "start_task", return_value="ses_mia_123"):
            assert client.post("/agent/tasks", json={"task": "x", "confirmed": True}).json()["session_id"] == "ses_mia_123"
        tasks = [{"session_id": "ses_mia_123", "title": "mia", "updated": 1, "status": "idle"}]
        with patch.object(main.opencode_client, "list_tasks", return_value=tasks), \
             patch.object(main.opencode_client, "get_task_view", return_value={"status": "idle"}), \
             patch.object(main.opencode_client, "continue_task") as mock_continue, \
             patch.object(main.opencode_client, "request_session", return_value="ses_mia_123"), \
             patch.object(main.opencode_client, "reply_permission") as mock_perm:
            assert [t["session_id"] for t in client.get("/agent/tasks").json()] == ["ses_mia_123"]
            assert other.get("/agent/tasks").json() == []
            assert other.get("/agent/tasks/ses_mia_123").status_code == 404
            assert other.post("/agent/tasks/ses_mia_123/message", json={"text": "borra todo"}).status_code == 404
            assert other.post("/agent/permissions/per_1", json={"reply": "always"}).status_code == 404
            assert not mock_continue.called and not mock_perm.called
            assert client.post("/agent/permissions/per_1", json={"reply": "once"}).status_code == 200
    finally:
        users_module.delete_user(uname)


def test_without_voice_libraries_the_rest_of_chati_still_works():
    """Windows recien instalado sin Visual C++ (Sandbox, 2026-09-30): la voz no
    carga. Antes se caia el orquestador entero; ahora solo avisa la voz."""
    with patch.object(main.voice_agent, "transcribe", side_effect=RuntimeError("La voz no esta disponible")):
        resp = client.post("/voice_chat", files={"audio": ("a.wav", b"RIFF", "audio/wav")})
    assert resp.status_code == 503 and "voz" in resp.json()["detail"]
    with patch.object(main.voice_agent, "speak", side_effect=RuntimeError("La voz no esta disponible")):
        assert client.post("/speak", data={"text": "hola"}).status_code == 503


def test_generations_are_recorded_under_whoever_asked():
    seen = []

    def fake_generate(*args, **kwargs):
        seen.append(main.comfyui_client.current_owner.get())
        return b"png"
    with patch.object(main.prompt_writer, "image_prompt", return_value=("A dog.", (1024, 1024))), \
         patch.object(main.image_agent, "generate", side_effect=fake_generate):
        client.post("/chat", json={"message": "un perro", "agent": "image"})
        guest = TestClient(app, headers={"X-Session-Token": client.post("/auth/guest").json()["token"]})
        guest.post("/chat", json={"message": "un gato", "agent": "image"})
    assert len(seen) == 2 and all(seen) and seen[0] != seen[1]
    assert seen[1].startswith("invitado:")


def test_upscaling_an_already_huge_image_is_refused_before_reaching_comfyui():
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (3000, 2000), (1, 2, 3)).save(buf, format="PNG")  # 6 MP: saldria de 96 MP
    with patch.object(main.image_agent, "upscale") as mock_upscale:
        resp = client.post("/image/upscale", files={"image": ("grande.png", buf.getvalue(), "image/png")})
    assert "muy grande" in resp.json()["response"]
    mock_upscale.assert_not_called()


def test_an_edit_that_misses_part_of_the_request_is_retried_insisting_on_it():
    # Sergio, 2026-10-05: "ha hecho la mitad de lo que he pedido"
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (64, 48), (10, 20, 30)).save(buf, format="PNG")
    photo = buf.getvalue()
    plan = main.photo_edit.EditPlan("Put her in a bikini holding a cocktail on a beach. Keep her face.", "local",
                                    "te he puesto un bikini y un coctel en la playa")
    Verdict = main.photo_edit.Verdict
    bad = Verdict(False, "She is not holding a cocktail", "no tiene el coctel en la mano")
    with patch.object(main.photo_edit, "plan_edit", return_value=[plan]), \
         patch.object(main.photo_edit, "describe_photo", return_value="a woman"), \
         patch.object(main.photo_edit, "verify_edit", side_effect=[bad, Verdict(True)]) as mock_verify, \
         patch.object(main, "_free_comfyui"), \
         patch.object(main.image_agent, "edit_with_kontext", return_value=photo) as mock_kontext:
        _, done = main._edit_photo("ponla en bikini en la playa con un coctel", photo)
    assert mock_kontext.call_count == 2
    assert "She is not holding a cocktail" in mock_kontext.call_args.args[0]
    # se juzga contra lo que pidio el usuario y lo que se le dice que se hizo
    assert mock_verify.call_args.args[3] == ("ponla en bikini en la playa con un coctel "
                                             "(te he puesto un bikini y un coctel en la playa)")
    assert "Ojo" not in done

    with patch.object(main.photo_edit, "plan_edit", return_value=[plan]), \
         patch.object(main.photo_edit, "describe_photo", return_value="a woman"), \
         patch.object(main.photo_edit, "verify_edit", side_effect=[bad, bad]), \
         patch.object(main, "_free_comfyui"), \
         patch.object(main.image_agent, "edit_with_kontext", return_value=photo):
        _, done = main._edit_photo("ponla en bikini en la playa con un coctel", photo)
    assert "no me ha salido del todo: no tiene el coctel en la mano" in done  # se dice, no se da por hecho


def test_a_long_photo_edit_tells_what_it_is_doing_while_it_works():
    def fake_edit(request, photo, face, earlier):
        main._report("Editando la foto (2-3 minutos)…")
        return photo, "Listo: hecho."
    with patch.object(main.model_registry, "get_edit_model", return_value=object()), \
         patch.object(main.photo_edit, "wants_edit", return_value=True), \
         patch.object(main, "_edit_photo", side_effect=fake_edit):
        with client.stream("POST", "/chat/stream", json={"message": "ponme en la playa",
                                                         "image_base64": _tiny_jpeg_b64()}) as resp:
            events = [json.loads(line) for line in resp.iter_lines() if line.strip()]
    assert [e["type"] for e in events] == ["start", "status", "done"]
    assert events[1]["text"].startswith("Editando") and events[2]["response"] == "Listo: hecho."


def test_the_face_comes_from_the_last_approved_version_after_a_face_change():
    # 2026-10-06: tras "ponte gafas", cada edicion dejaba la cara de Kontext y se iba pareciendo menos
    plan = main.photo_edit.EditPlan("Make it sunset.", "local", "atardecer")
    used = []
    with patch.object(main.image_agent, "edit_with_kontext", return_value=b"kontext"), \
         patch.object(main.photo_edit, "load_rgb", side_effect=lambda b: b), \
         patch.object(main.photo_edit, "prepare_for_kontext", side_effect=lambda x: x), \
         patch.object(main.photo_edit, "needs_upscale", return_value=False), \
         patch.object(main.photo_edit, "finish", side_effect=lambda cur, ed, mode: ed), \
         patch.object(main.photo_edit, "to_jpeg", side_effect=lambda x: x), \
         patch.object(main.photo_edit, "restore_faces", side_effect=lambda src, cur, keep_hair, face_only=False, seams=None: used.append(src) or cur):
        main._apply_edit([plan], b"base", b"original", "que sea al atardecer", ["ponme en la playa"])
        main._apply_edit([plan], b"base", b"original", "que sea al atardecer", ["ponte gafas de sol"])
        main._apply_edit([plan], b"base", b"original", "ponle barba", [])
    assert used == [b"original", b"base"]  # y con "ponle barba" (cambio de cara) no se toca


def test_a_photo_in_image_mode_is_always_edited_and_streams_its_progress():
    def fake_edit(request, photo, face, earlier):
        main._report("Mirando la foto…")
        return photo, "Listo: te he puesto en Paris."
    with patch.object(main.model_registry, "get_edit_model", return_value=object()), \
         patch.object(main.photo_edit, "wants_edit", return_value=False) as mock_wants, \
         patch.object(main, "_edit_photo", side_effect=fake_edit):
        with client.stream("POST", "/chat/stream", json={"message": "Paris", "agent": "image",
                                                         "image_base64": _tiny_jpeg_b64()}) as resp:
            events = [json.loads(line) for line in resp.iter_lines() if line.strip()]
    assert [e["type"] for e in events] == ["start", "status", "done"]
    assert events[-1]["agent_used"] == "image_edit" and events[-1]["file_url"]
    mock_wants.assert_not_called()  # en modo Imagen una foto siempre es para editarla


def test_a_photo_in_image_mode_without_the_editor_still_makes_an_image():
    with patch.object(main.model_registry, "get_edit_model", return_value=None), \
         patch.object(main.face_detect, "has_face", return_value=True), \
         patch.object(main.image_agent, "generate_with_face", return_value=b"png") as mock_faceid:
        data = client.post("/chat", json={"message": "en la playa", "agent": "image",
                                          "image_base64": _tiny_jpeg_b64()}).json()
    assert data["agent_used"] == "image_faceid" and data["file_url"].endswith(".png")
    mock_faceid.assert_called_once()


def test_group_photos_keep_clothes_and_background_in_two_passes():
    # 2026-10-06: en una sola pasada, con tres personas Kontext las recolocaba y cambiaba caras
    plans = [main.photo_edit.EditPlan("Dress them in winter coats.", "local", "abrigos"),
             main.photo_edit.EditPlan("Change the background to snow.", "fondo", "nieve")]
    for faces, passes in ((["izquierda", "centro", "derecha"], 2), (["centro"], 1)):
        with patch.object(main.photo_edit, "plan_edit", return_value=list(plans)), \
             patch.object(main.photo_edit, "describe_photo", return_value="people"), \
             patch.object(main.face_detect, "face_positions", return_value=faces), \
             patch.object(main.photo_edit, "verify_edit", return_value=None), \
             patch.object(main, "_free_comfyui"), \
             patch.object(main, "_apply_edit", return_value=b"jpg") as mock_apply:
            main._edit_photo("ponnos en la nieve con abrigos", b"foto")
        assert len(mock_apply.call_args.args[0]) == passes, faces


def test_deleting_a_conversation_deletes_its_photos_too():
    with patch.object(main.model_registry, "get_edit_model", return_value=object()), \
         patch.object(main.photo_edit, "wants_edit", return_value=True), \
         patch.object(main, "_edit_photo", return_value=(b"editada", "Listo: hecho.")):
        data = client.post("/chat", json={"message": "ponme en la playa", "image_base64": _tiny_jpeg_b64()}).json()
    sid = data["session_id"]
    names = [m["media"] for m in client.get(f"/sessions/{sid}").json() if m["media"]]
    assert len(names) == 2 and all((main.media_store.media_dir() / (n + ".enc")).exists() for n in names)
    assert client.delete(f"/sessions/{sid}").json()["ok"]
    assert not any((main.media_store.media_dir() / (n + ".enc")).exists() for n in names)


def test_removals_are_not_second_guessed_by_the_vision_check():
    # 2026-10-06: decia "no se ha quitado" con la persona del fondo y el gorro ya quitados
    plan = main.photo_edit.EditPlan("Remove the other people in the background. Keep the man the same.", "local",
                                    "he quitado a la gente del fondo")
    with patch.object(main.photo_edit, "plan_edit", return_value=[plan]), \
         patch.object(main.photo_edit, "describe_photo", return_value="a man"), \
         patch.object(main.photo_edit, "verify_edit") as mock_verify, \
         patch.object(main, "_free_comfyui"), \
         patch.object(main, "_apply_edit", return_value=b"jpg") as mock_apply:
        _, done = main._edit_photo("quita a la gente del fondo", b"foto")
    mock_verify.assert_not_called()
    assert mock_apply.call_count == 1 and "Ojo" not in done


def test_without_people_a_background_change_is_not_cut_out_as_a_person():
    # 2026-10-06: el recorte de personas (MODNet) teñia de amarillo al perro en la playa
    plans = [main.photo_edit.EditPlan("Change the background to a beach. Keep the dog.", "fondo", "playa")]
    with patch.object(main.photo_edit, "plan_edit", return_value=plans), \
         patch.object(main.photo_edit, "describe_photo", return_value="one dog"), \
         patch.object(main.face_detect, "face_positions", return_value=[]), \
         patch.object(main.photo_edit, "verify_edit", return_value=None), \
         patch.object(main, "_free_comfyui"), \
         patch.object(main, "_apply_edit", return_value=b"jpg") as mock_apply:
        main._edit_photo("que el parque sea una playa", b"foto")
    assert [s.mode for s in mock_apply.call_args.args[0]] == ["local"]


def test_all_conversations_of_the_user_can_be_deleted_at_once():
    """2026-10-07: un boton para borrar todas las conversaciones de golpe."""
    with patch.object(main.memory, "list_sessions", return_value=[{"session_id": "a"}, {"session_id": "b"}]) as ls, \
         patch.object(main, "_delete_conversation") as delete:
        resp = client.delete("/sessions")
    assert resp.status_code == 200 and resp.json() == {"ok": True, "deleted": 2}
    assert ls.call_args.kwargs["user_id"]  # solo las del usuario, nunca las de todos
    assert [c.args[1] for c in delete.call_args_list] == ["a", "b"]
