import contextlib
import json
import re
import secrets
import subprocess
import sys
import threading
import time
import uuid

import requests
from functools import partial
from pathlib import Path

import yaml
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

import auth
import auth_sessions
import face_detect
import access
import agent_attachments
import job_search
import shopping
import web_tools
import deep_search
import llm_proxy
import logging_setup
import media_store
import model_registry
import paths
import persona_trainer
import routes_apps
import routes_auth
import security
import users

from agents import ollama_client as ollama_client_module
from agents.ollama_client import OllamaClient
from agents.llm_agent import LLMAgent
from agents.image_agent import ImageAgent
from agents.video_agent import VideoAgent
from agents.voice_agent import VoiceAgent
from agents import comfyui_client
from agents.comfyui_client import GenerationCancelled, interrupt as comfyui_interrupt
from router import Router
from verifier import Verifier
import people
import profile_store
import memory
import file_access
import finetune_export
import metrics
import model_updates
import opencode_client
import plans
import session_docs
import tools
from rag import EMBED_MODEL, KnowledgeBase

log = logging_setup.setup()

# junto a este archivo, no en la carpeta desde la que se arranque
with open(Path(__file__).parent / "config.yaml", "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

# se genera aqui (no de forma perezosa en el propio endpoint) para que el
# archivo exista desde el primer arranque y el admin pueda consultarlo antes
# de que nadie intente registrarse - mismo patron que auth.API_KEY.
users.get_or_create_registration_key()

ollama = OllamaClient(CONFIG["ollama"]["base_url"])
# sin "pensar en voz alta" tambien en el chat, el router y el verificador, no solo en el agente
ollama_client_module.NO_THINK_MODELS.update(CONFIG["opencode"].get("no_think_models", []))

text_agent = LLMAgent("text", CONFIG["agents"]["text"]["model"], ollama)
code_agent = LLMAgent("code", CONFIG["agents"]["code"]["model"], ollama)
def _vision_model() -> str:
    """La variante GPU de vision si esta instalada (mucho mas rapida), si no la de CPU."""
    cfg = CONFIG["agents"]["vision"]
    gpu = cfg.get("gpu_model")
    if gpu and tuple(gpu.split(":", 1)) in set(model_updates.installed_official_models()):
        return gpu
    return cfg["model"]


vision_agent = LLMAgent("vision", _vision_model(), ollama)
opencode_client.WORKDIR = agent_attachments.AGENT_DIR
image_agent = ImageAgent(CONFIG["comfyui"]["base_url"])
video_agent = VideoAgent(CONFIG["comfyui"]["base_url"])
voice_agent = VoiceAgent()
router = Router(ollama, routing_model=CONFIG["router"]["model"])
verifier = Verifier(
    ollama,
    model=CONFIG["verifier"]["model"],
    strict=CONFIG["verifier"]["strict"],
)
knowledge_base = KnowledgeBase(ollama)

AGENTS = {"text": text_agent, "code": code_agent}

# --- Politica de "un solo modelo de texto pesado en RAM a la vez" (ver
# ROADMAP.md, punto 14, pedido explicito por Sergio) ---
# CONFIG["router"]["model"] (qwen3:8b desde 2026-09-29) vive en VRAM, no en RAM del
# sistema, y se usa en cada mensaje sin importar el perfil (router +
# verificador + perfil "rapido" lo comparten) - se deja siempre cargado, no
# es el problema. Los demas modelos de texto/codigo/vision (perfiles
# "Mejor calidad"/"Uncensored" y vision, todos CPU pura) SI compiten por la
# RAM real: con 32GB fisicos, tener varios a la vez la agota y causa
# lentitud severa por intercambio a disco - confirmado en vivo el
# 2026-09-24 (37.2GB de modelos cargados a la vez en una maquina con
# 31.7GB de RAM, un "hola" tardando 174s solo en decidir si usar una
# herramienta). Se fuerza que solo haya un modelo "pesado" cargado en cada
# momento, descargando el anterior antes de usar uno distinto - a cambio de
# una recarga de 18-20s+ al cambiar de perfil, que es el trade-off que
# Sergio eligio explicitamente (velocidad de cambio de perfil sacrificada a
# cambio de que nunca se agote la RAM).
_LIGHT_MODEL = CONFIG["router"]["model"]
_active_heavy_model: str | None = None
_heavy_model_lock = threading.Lock()


def _ensure_active_model(model: str) -> None:
    """Llamar justo antes de generar con `model`. Si `model` es "pesado"
    (distinto del modelo ligero compartido) y distinto del que ya estaba
    activo, descarga el anterior primero. Si `model` es el ligero, libera
    cualquier modelo pesado que hubiera quedado cargado de antes (ya no
    hace falta)."""
    global _active_heavy_model
    new_heavy = model if model != _LIGHT_MODEL else None
    with _heavy_model_lock:
        old_heavy = _active_heavy_model
        if old_heavy and old_heavy != new_heavy:
            ollama.unload(old_heavy)
        _active_heavy_model = new_heavy


def _loading_event(model: str) -> str:
    return json.dumps({"type": "status",
                       "text": f"Cargando el modelo {model}… la primera respuesta tarda más"}) + "\n"


def _index_in_background(session_id: str, message: str, answer: str, user_id: str | None,
                         dek: bytes | None, key_generation: int | None) -> None:
    """Indexar el intercambio en la memoria larga necesita embeddings (~2s
    medido) - antes se hacia ANTES de mandar la respuesta y el usuario lo
    esperaba en cada mensaje. No hace falta: solo se usa en mensajes futuros."""
    if not user_id:
        # invitado: sin memoria larga (se guardaba sin dueño y quedaba al
        # alcance de las busquedas "de todos", auditoria 2026-09-29)
        return

    def work():
        try:
            knowledge_base.add_conversation(session_id, message, answer, user_id=user_id,
                                            dek=dek, key_generation=key_generation)
        except Exception as exc:
            # la respuesta ya se entrego; perder este recuerdo no debe romper nada
            log.warning("No se pudo indexar la conversacion en la memoria larga: %s", exc)
    threading.Thread(target=work, daemon=True).start()


def _agent_models() -> set[str]:
    return {CONFIG["opencode"]["model"], CONFIG["opencode"]["model_potente"]}


def _free_memory_for_generation() -> None:
    """Antes de cada imagen/video: descarga TODOS los modelos de Ollama.
    Medido en vivo el 2026-09-25: con qwen2.5:7b (router + perfil rapido)
    ocupando 4.9 de los 8GB de VRAM, y los modelos -cpu llenando la RAM
    (~37GB cargados en un equipo de 32GB), una imagen FLUX de una bicicleta
    llevaba ~400s en vez de menos de un minuto. Vuelven a cargarse solos en
    el siguiente mensaje de chat (unos segundos, estan en cache de disco).
    Excepcion: el modelo del agente si hay una tarea en marcha - descargarlo
    a mitad obliga a recargar 26GB en su siguiente paso."""
    global _active_heavy_model
    keep = set()
    if opencode_client.busy_session_ids(CONFIG["opencode"]["base_url"]):
        keep |= _agent_models()
    with _heavy_model_lock:
        for model in ollama.running_models():
            if model not in keep:
                ollama.unload(model)
        _active_heavy_model = None


def _agent_model(agent: str | None, model: str | None = None) -> str:
    """El modelo con el que va a trabajar el agente: el elegido para la tarea
    ("Rapido" en modo Codigo) o el suyo. Los de modo Codigo usan el potente."""
    if model:
        return model
    oc = CONFIG["opencode"]
    heavy = {oc["agent_potente"], oc.get("agent_code"), oc.get("agent_code_plan")}
    return oc["model_potente"] if agent in heavy else oc["model"]


def _free_memory_for_agent(agent: str | None, model: str | None = None, wait: bool = False) -> None:
    """Antes de una tarea del agente, en segundo plano (OpenCode tarda
    igualmente en pedir el modelo), deja sitio a su modelo:
    - agente rapido (qwen3:8b, ~7,6GB de GPU con su contexto): no cabe junto
      al modelo del chat ni a lo que retenga ComfyUI - se descargan.
    - agente potente (qwen3-coder, ~26GB de RAM): medido el 2026-09-25, con
      el modelo de vision tambien cargado y ComfyUI reteniendo la ultima
      imagen, cargarlo tardo 425s de los 477s de una tarea "crea hello.txt"."""
    model = _agent_model(agent, model)
    potente = model == CONFIG["opencode"]["model_potente"]

    def work():
        # el potente va en CPU: el ligero del chat (GPU) puede quedarse
        keep = {model, EMBED_MODEL} | ({_LIGHT_MODEL} if potente else set())
        _keep_only_ollama(keep, heavy=model)
        comfyui_client.free_memory(CONFIG["comfyui"]["base_url"])
        # si se cargo cuando la GPU estaba ocupada, Ollama lo dejo en CPU y
        # ahi se queda aunque se libere (medido el 2026-09-29: 60s por
        # respuesta en vez de 7s) - se descarga para que vuelva a la GPU
        share = ollama.gpu_share(model)
        if not potente and share is not None and share < 0.5 \
                and not opencode_client.busy_session_ids(CONFIG["opencode"]["base_url"]):
            ollama.unload(model)

    if wait:  # las apps (empleo, compras) usan el modelo nada mas volver
        work()
    else:
        threading.Thread(target=work, daemon=True).start()


comfyui_client.before_submit = _free_memory_for_generation
opencode_client.before_task = _free_memory_for_agent

OUTPUT_DIR = paths.OUTPUT_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

@contextlib.asynccontextmanager
async def _lifespan(_app):
    removed = media_store.cleanup_tmp()
    if removed:
        log.info("Temporales huerfanos borrados: %d", removed)
    purged = memory.purge_guest_messages(older_than_hours=24)
    if purged:
        log.info("Mensajes de invitado antiguos borrados: %d", purged)
    log.info("Orquestador arrancado")
    yield


app = FastAPI(title="IA personal - orquestador", lifespan=_lifespan)


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    """Un fallo no previsto: queda en el log con su traza (sin datos del
    usuario) y el usuario ve un mensaje claro en vez de un 500 vacio."""
    log.exception("Error no controlado en %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Error interno. Esta anotado en el registro de Chati."}, status_code=500)



class SessionAuthMiddleware(BaseHTTPMiddleware):
    """Sustituye a la clave API estatica compartida (ver ROADMAP.md, punto 0,
    fase 4): cada peticion necesita un token de sesion valido, obtenido via
    /auth/login, /auth/guest o /auth/register. La clave API vieja
    (auth.API_KEY) queda sin usar - no se borra, solo deja de comprobarse."""
    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS" or auth.is_public_path(request.url.path):
            return await call_next(request)
        token = _session_token(request)
        session = auth_sessions.get_session(token) if token else None
        if session is None:
            return JSONResponse({"detail": "No autorizado. Inicia sesion."}, status_code=401)
        path = request.url.path
        if session["role"] == "guest" and not access.guest_may_use(path):
            return access.guest_blocked()
        if path.startswith(access.PC_PATH_PREFIXES) and not access.can_use_pc(session):
            return access.pc_blocked()
        request.state.session = session
        _mark_activity(True, request.method)
        try:
            return await call_next(request)
        finally:
            _mark_activity(False, request.method)


app.add_middleware(SessionAuthMiddleware)
# la ultima en añadirse es la primera en ejecutarse: Host/Origin antes que nada
app.add_middleware(security.LocalOnlyMiddleware)


# --- Sistema de usuarios (rutas en routes_auth.py, politica en access.py) ---

_session_token = access.session_token
_current_session = access.current_session
_guest_blocked = access.guest_blocked
_can_use_pc = access.can_use_pc


def _on_login(login: dict) -> None:
    try:  # documentos subidos antes de que se cifraran los originales
        knowledge_base.encrypt_plain_originals(login["id"], login["dek"])
    except OSError:
        pass


def _delete_user_data(user_id: str) -> None:
    memory.delete_user_messages(user_id)
    people.delete_all_for_user(user_id)
    profile_store.delete_all_for_user(user_id)
    knowledge_base.delete_all_for_user(user_id)
    session_docs.delete_all_for_user(user_id)
    persona_trainer.delete_all_for_user(user_id)


routes_auth.configure(on_login=_on_login, delete_user_data=_delete_user_data)
app.include_router(routes_auth.router)


class ChatRequest(BaseModel):
    message: str
    agent: str | None = None  # si se omite, el router decide
    session_id: str | None = None  # si se omite, se crea una sesion nueva
    model_profile: str | None = None  # rapido/bueno/seguridad, ver ROADMAP.md punto 5c
    image_base64: str | None = None  # si se manda, se comenta con el modelo de vision (no se genera nada)
    verify: bool = True  # False salta el verificador anti-alucinacion para este mensaje - eleccion explicita del usuario, ver ROADMAP.md
    image_model: str | None = None  # id de model_registry.py (p.ej. "flux:flux1-schnell-Q4_K_S"), None = automatico
    video_model: str | None = None  # id de model_registry.py (p.ej. "ltxv:ltxv-2b-0.9.8-distilled-fp8"), None = automatico


class ChatResponse(BaseModel):
    agent_used: str
    response: str
    verifier_gated: bool
    verifier_reason: str | None = None
    file_path: str | None = None
    file_url: str | None = None
    session_id: str | None = None
    sources: list[str] | None = None
    # tarea de OpenCode lanzada desde el chat - la interfaz pinta su tarjeta
    agent_task_id: str | None = None
    # tarea que parece compleja para el agente rapido: la interfaz pregunta
    # con que modelo hacerla ({"task", "reason"}), ver _assess_for_fast_agent
    agent_choice: dict | None = None


MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # fotos y audios: de sobra, y no se lee 1 GB entero en memoria
MAX_DOC_BYTES = 100 * 1024 * 1024  # documentos y adjuntos del agente


def _read_upload(upload: UploadFile, limit: int = MAX_UPLOAD_BYTES) -> bytes:
    data = upload.file.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"El archivo es demasiado grande (maximo {limit // (1024 * 1024)} MB).")
    return data


def _with_bytes(resp: ChatResponse, data: bytes, ext: str, session: dict) -> ChatResponse:
    name = media_store.save(data, ext, session["dek"])
    resp.file_path = name
    resp.file_url = media_store.url_for(name)
    return resp


def _generation_error_message(action: str, exc: Exception) -> str:
    if isinstance(exc, GenerationCancelled):
        return "Generación cancelada."
    return f"Fallo {action}: {exc}"


# Actividad del usuario, para que la copia de seguridad (que reinicia el
# orquestador y cierra las sesiones) espere a un momento tranquilo
# (auditoria 2026-09-29: se hacia a cualquier hora y cortaba lo que hubiera).
_last_user_action = 0.0
_in_flight = 0
_activity_lock = threading.Lock()
IDLE_AFTER_SECONDS = 15 * 60


def _mark_activity(start: bool, method: str) -> None:
    global _last_user_action, _in_flight
    with _activity_lock:
        _in_flight += 1 if start else -1
        if method not in ("GET", "HEAD", "OPTIONS"):  # los GET son sobre todo consultas de estado
            _last_user_action = time.time()


def _is_busy() -> bool:
    if _in_flight > 0 or time.time() - _last_user_action < IDLE_AFTER_SECONDS:
        return True
    for module in (job_search, shopping, deep_search):
        if any(run.get("running") for run in list(module._runs.values())):
            return True
    try:
        if opencode_client.busy_session_ids(CONFIG["opencode"]["base_url"]):
            return True
        if sum(comfyui_client.user_queue(CONFIG["comfyui"]["base_url"])):
            return True
    except Exception:  # un servicio caido no esta ocupado
        pass
    return False


@app.get("/health")
def health(busy: bool = False):
    """Publica (la usa el vigilante cada 30 s: tiene que ser instantanea).
    Con ?busy=true dice ademas si hay algo en marcha (antes de la copia de
    seguridad), que consulta a OpenCode y ComfyUI y puede tardar."""
    return {"status": "ok", "busy": _is_busy()} if busy else {"status": "ok"}


# --- Pasarela OpenCode -> Ollama (ver llm_proxy.py): quita los parametros
# null que el modelo del agente pone en las herramientas. No lleva sesion de
# usuario: solo la usa OpenCode, que manda su contraseña como clave ("apiKey":
# "{env:OPENCODE_SERVER_PASSWORD}" en opencode.json). Antes era publica y
# cualquier programa o web podia usar los modelos por aqui (auditoria 2026-09-29).
@app.api_route("/llm/v1/{path:path}", methods=["GET", "POST"])
async def llm_proxy_route(path: str, request: Request):
    expected = f"Bearer {opencode_client.AUTH[1]}"
    if not secrets.compare_digest(request.headers.get("authorization", "").encode(), expected.encode()):
        return JSONResponse({"detail": "No autorizado."}, status_code=401)
    target = f"{CONFIG['ollama']['base_url']}/v1/{path}"
    body = llm_proxy.disable_thinking(await request.body(), set(CONFIG["opencode"].get("no_think_models", [])))
    headers = {"Content-Type": request.headers.get("content-type", "application/json")}
    wants_stream = b'"stream":true' in body.replace(b" ", b"")

    def forward():
        return requests.request(request.method, target, data=body or None, headers=headers,
                                stream=wants_stream, timeout=900)

    upstream = await run_in_threadpool(forward)
    if not wants_stream:
        content = upstream.content
        if upstream.ok and b"tool_calls" in content:
            try:
                content = json.dumps(llm_proxy.clean_completion(upstream.json()), ensure_ascii=False).encode("utf-8")
            except ValueError:
                pass
        return Response(content=content, status_code=upstream.status_code,
                        media_type=upstream.headers.get("content-type", "application/json"))

    def stream():
        try:
            for line in upstream.iter_lines():
                yield llm_proxy.clean_stream_line(line) + b"\n\n" if line else b""
        finally:
            upstream.close()
    return StreamingResponse(stream(), status_code=upstream.status_code,
                             media_type=upstream.headers.get("content-type", "text/event-stream"))


@app.get("/metrics/summary")
def metrics_summary(last_n: int = 2000):
    return metrics.summary(last_n=last_n)


@app.get("/plan/{session_id}")
def get_plan(session_id: str):
    return {"pasos": plans.get_plan(session_id)}


@app.get("/models/updates")
def get_model_updates(force: bool = False):
    """Solo comprueba (metadatos pequeños, nada de peso del modelo). Nunca
    descarga nada por su cuenta - eso requiere /models/update explicito."""
    return {"updates": model_updates.check_updates(force=force)}


@app.post("/models/update")
def trigger_model_update(model: str = Form(...), request: Request = None):
    """Descarga de verdad un modelo - solo se puede llamar sobre un modelo
    que YA esta instalado (no cualquier string arbitrario), y solo lo hace
    cuando el usuario pulsa el boton, nunca automaticamente. Solo admin: baja
    modelos compartidos por todo el sistema, no es un dato de un usuario."""
    if request.state.session["role"] != "admin":
        return JSONResponse({"detail": "Solo un administrador puede actualizar modelos."}, status_code=403)
    known = {r["model"] for r in model_updates.check_updates()}
    if model not in known:
        return JSONResponse({"detail": f"Modelo desconocido o no instalado: {model}"}, status_code=400)
    subprocess.Popen(["ollama", "pull", model], creationflags=subprocess.CREATE_NO_WINDOW)
    return {"ok": True, "message": f"Descarga de {model} iniciada en segundo plano."}


@app.delete("/models/text/{model}")
def delete_text_model(model: str, request: Request):
    """Borra un modelo de texto instalado que YA no usa ningun agente
    configurado - libera disco de verdad (varios GB por modelo). Solo admin.
    Se niega si el modelo esta en uso (ver /models/roles) para que no te
    puedas cargar por error el que usa el chat o el router."""
    if request.state.session["role"] != "admin":
        return JSONResponse({"detail": "Solo un administrador puede borrar modelos."}, status_code=403)
    known = {r["model"] for r in model_updates.check_updates()}
    if model not in known:
        return JSONResponse({"detail": f"Modelo desconocido o no instalado: {model}"}, status_code=400)
    if model in get_model_roles():
        return JSONResponse(
            {"detail": f"'{model}' esta en uso ahora mismo - no se puede borrar."}, status_code=400
        )
    resp = requests.delete(f"{CONFIG['ollama']['base_url']}/api/delete", json={"model": model}, timeout=30)
    if not resp.ok:
        return JSONResponse({"detail": f"Ollama no pudo borrar el modelo: {resp.text}"}, status_code=502)
    return {"ok": True, "message": f"'{model}' borrado."}


@app.post("/cancel")
def cancel_generation():
    """Interrumpe la generacion de imagen/video en curso en ComfyUI. Como
    solo hay una GPU y el sistema no lanza generaciones en paralelo, no hace
    falta identificar cual: siempre es 'lo que este corriendo ahora mismo'."""
    comfyui_interrupt(CONFIG["comfyui"]["base_url"])
    return {"ok": True}


# --- Modo "Agente": interfaz propia encima de OpenCode (ver opencode_client.py) ---
# Solo usuarios registrados: el agente puede leer/escribir archivos y ejecutar
# comandos en el equipo (siempre pidiendo permiso para lo que modifica algo).

class AgentTaskRequest(BaseModel):
    task: str
    potente: bool = False
    confirmed: bool = False  # el usuario ya eligio modelo: no volver a valorar
    attachments: list[dict] = []  # lo que devolvio /agent/attachments
    # modo Codigo: carpeta del proyecto, planificar (sin cambiar nada) o
    # construir, y "rapido" (qwen3:8b en GPU en vez de qwen3-coder en CPU)
    project: str | None = None
    plan: bool = False
    rapido: bool = False


IMAGE_DESCRIBE_PROMPT = ("Describe en una sola frase, en español, que se ve en esta imagen, para poder "
                         "clasificarla o ponerle nombre. Si hay texto importante (un titulo, un total, una "
                         "fecha), incluyelo.")


@app.post("/agent/attachments")
def agent_upload_attachments(request: Request, files: list[UploadFile] = File(...)):
    """Adjuntos de una tarea del agente: se copian a Documentos/Chati/adjuntos
    y las imagenes se describen con el modelo de vision (el del agente no ve
    imagenes). Devuelve la lista para mandarla luego con la tarea."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    try:
        folder, saved = agent_attachments.save_batch([(f.filename, _read_upload(f, MAX_DOC_BYTES)) for f in files])
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    if any(Path(i["path"]).suffix.lower() in agent_attachments.IMAGE_SUFFIXES for i in saved):
        _ensure_active_model(vision_agent.model)
        agent_attachments.describe_images(
            saved, lambda b64: "".join(vision_agent.respond_with_image_stream(IMAGE_DESCRIBE_PROMPT, b64)))
    return {"folder": folder.as_posix(), "files": saved}


def _assess_for_fast_agent(task: str) -> dict | None:
    """{"task", "reason"} si la tarea parece demasiado compleja para el
    agente rapido (se recomienda el potente), None si no. Si la valoracion
    falla, None: nunca debe impedir lanzar una tarea."""
    try:
        verdict = router.assess_agent_task(task)
    except Exception:
        return None
    return {"task": task, "reason": verdict["reason"]} if verdict["complex"] else None


class AgentQuestionReply(BaseModel):
    answers: list[list[str]]


class AgentPermissionReply(BaseModel):
    reply: str


def _opencode_call(fn, *args):
    try:
        return fn(CONFIG["opencode"]["base_url"], *args)
    except requests.RequestException as exc:
        return JSONResponse(
            {"detail": f"No se pudo contactar con el agente de codigo (¿esta arrancado?): {exc}"},
            status_code=502)


@app.get("/agent/tasks")
def agent_list_tasks(request: Request):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    return _opencode_call(opencode_client.list_tasks)


@app.post("/agent/tasks")
def agent_start_task(request: Request, req: AgentTaskRequest):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    if not req.task.strip():
        return JSONResponse({"detail": "Describe la tarea."}, status_code=400)
    task = req.task.strip()
    if req.project:
        project = Path(req.project)
        if not project.is_dir():
            return JSONResponse({"detail": f"No existe la carpeta {req.project}."}, status_code=400)
        agent = CONFIG["opencode"]["agent_code_plan" if req.plan else "agent_code"]
        model = CONFIG["opencode"]["model"] if req.rapido else None
        result = _opencode_call(opencode_client.start_task, task, agent, str(project), model)
        return result if isinstance(result, JSONResponse) else {"session_id": result}
    if not req.potente and not req.confirmed:
        choice = _assess_for_fast_agent(task)
        if choice:
            return {"needs_choice": True, **choice, "attachments": req.attachments}
    agent = CONFIG["opencode"]["agent_potente" if req.potente else "agent"]
    note = agent_attachments.task_note(req.attachments)
    result = _opencode_call(opencode_client.start_task, f"{task}\n\n{note}" if note else task, agent)
    return result if isinstance(result, JSONResponse) else {"session_id": result}


@app.get("/agent/tasks/{session_id}")
def agent_task_view(request: Request, session_id: str):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    return _opencode_call(_task_view, session_id)


def _task_view(base_url: str, session_id: str) -> dict:
    view = opencode_client.get_task_view(base_url, session_id)
    # trabajando pero con su modelo aun sin cargar en Ollama: esta cargandolo
    task_model = view.get("model") or CONFIG["opencode"]["model"]
    view["model_loading"] = view["status"] != "idle" and task_model not in ollama.running_models()
    return view


# eventos que cambian lo que se ve pero llegan en rafaga (texto palabra a
# palabra): como mucho uno cada LIVE_THROTTLE segundos; el resto, al momento
LIVE_THROTTLE = 0.3


@app.get("/agent/tasks/{session_id}/live")
def agent_task_live(request: Request, session_id: str):
    """La tarea en directo (SSE): cada vez que OpenCode avisa de algo en ESTA
    tarea (texto nuevo, una herramienta, un permiso...) se manda su estado
    completo. Sustituye a preguntar cada 2,5s. Si OpenCode se cae, se cierra
    con un evento "end" y la interfaz vuelve a preguntar por su cuenta."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    base = CONFIG["opencode"]["base_url"]
    marker = session_id.encode()

    # OpenCode no guarda el texto mientras se escribe: lo manda palabra a
    # palabra en eventos "message.part.delta" y solo lo guarda al acabar esa
    # parte. Se van juntando aqui para enseñarlo a medio escribir.
    writing = {"part": None, "text": ""}

    def snapshot() -> str:
        view = _task_view(base, session_id)
        if writing["text"] and view["status"] != "idle":
            view["streaming_text"] = writing["text"]
        return f"data: {json.dumps(view, ensure_ascii=False)}\n\n"

    def track_text(event: dict) -> None:
        props = event.get("properties") or {}
        if event.get("type") == "message.part.delta" and props.get("field") == "text":
            if props.get("partID") != writing["part"]:
                writing.update(part=props.get("partID"), text="")
            writing["text"] += props.get("delta", "")
        elif event.get("type") == "message.part.updated":
            part = props.get("part") or {}
            if part.get("id") == writing["part"] and part.get("text"):
                writing.update(part=None, text="")  # ya guardada: sale en los pasos normales

    def events():
        try:
            yield snapshot()
            last = 0.0
            with requests.get(f"{base}/event", stream=True, timeout=(5, 60), auth=opencode_client.AUTH,
                              **opencode_client._sparams(base, session_id)) as upstream:
                for line in upstream.iter_lines():
                    if not line.startswith(b"data:"):
                        continue
                    if marker not in line:
                        # los latidos de OpenCode (~10s) sirven para notar si el
                        # navegador se ha ido: al escribir, falla y se cierra
                        if b"server.heartbeat" in line:
                            yield ": ping\n\n"
                        continue
                    try:
                        track_text(json.loads(line[5:]))
                    except ValueError:
                        pass
                    # rafagas: texto palabra a palabra y salida de comandos linea a linea
                    bursty = b"message.part.delta" in line or (
                        b"message.part.updated" in line and b'"status":"running"' in line)
                    if bursty and time.time() - last < LIVE_THROTTLE:
                        continue
                    last = time.time()
                    yield snapshot()
        except requests.RequestException:
            pass
        yield "event: end\ndata: {}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.get("/agent/tasks/{session_id}/changes")
def agent_task_changes(request: Request, session_id: str):
    """Ver cambios: que archivos cambio la tarea en la carpeta de trabajo."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    return _opencode_call(opencode_client.get_changes, session_id)


@app.post("/agent/tasks/{session_id}/revert")
def agent_task_revert(request: Request, session_id: str):
    """Deshacer lo que la tarea cambio en la carpeta de trabajo."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    if not opencode_client.workdir_undoable():
        return JSONResponse({"detail": "No se puede deshacer: falta git en este equipo."}, status_code=400)
    result = _opencode_call(opencode_client.revert_task, session_id)
    return result if isinstance(result, JSONResponse) else {"ok": True}


@app.post("/agent/tasks/{session_id}/unrevert")
def agent_task_unrevert(request: Request, session_id: str):
    """Rehacer: vuelve a poner lo que se deshizo."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    result = _opencode_call(opencode_client.unrevert_task, session_id)
    return result if isinstance(result, JSONResponse) else {"ok": True}


@app.post("/agent/tasks/{session_id}/abort")
def agent_abort(request: Request, session_id: str):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    result = _opencode_call(opencode_client.abort, session_id)
    return result if isinstance(result, JSONResponse) else {"ok": True}


class AgentShortcutsRequest(BaseModel):
    shortcuts: list[dict]


@app.get("/agent/shortcuts")
def agent_get_shortcuts(request: Request):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    return profile_store.get_agent_shortcuts(session["user_id"], session["dek"], session["key_generation"])


@app.put("/agent/shortcuts")
def agent_save_shortcuts(request: Request, req: AgentShortcutsRequest):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    try:
        return profile_store.save_agent_shortcuts(req.shortcuts, session["user_id"], session["dek"],
                                                  session["key_generation"])
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


# --- Mis apps: empleo, compras y busqueda profunda (rutas en routes_apps.py) ---

# Contexto de las apps del navegador (empleo, compras, busqueda profunda): sus
# peticiones caben en 8K. Con los 16K del agente, qwen3:8b no cabe entero en
# 8GB de VRAM (75% en GPU, 27 tok/s); con 8K, 92% y 47 tok/s (medido el
# 2026-09-29).
APPS_NUM_CTX = 8192
# Tope de lo que escribe en cada llamada de las apps: a veces se enrollaba en una
# pagina hasta llenar el contexto (~3 min en vez de 20s, 2026-09-29). Sobra para
# 15 resultados, una carta o un informe.
APPS_NUM_PREDICT = 2000


def _job_chat(prompt: str) -> str:
    # el mismo modelo que el agente rapido (qwen3:8b), sin razonamiento previo
    return ollama.chat(CONFIG["opencode"]["model"], [{"role": "user", "content": prompt}],
                       temperature=0.2, think=False, num_ctx=APPS_NUM_CTX, num_predict=APPS_NUM_PREDICT)


def _describe_product_image(b64: str) -> str:
    _ensure_active_model(vision_agent.model)
    try:
        return "".join(vision_agent.respond_with_image_stream(shopping.IMAGE_PROMPT, b64))
    finally:
        # fuera de la GPU: el resto de la busqueda la hace qwen3:8b, que no
        # cabe junto a el (si no, Ollama lo cargaria en CPU, 8 veces mas lento)
        ollama.unload(vision_agent.model)


routes_apps.configure(chat=_job_chat,
                      before=lambda: _free_memory_for_agent(CONFIG["opencode"]["agent"], wait=True),
                      describe_image=_describe_product_image)
app.include_router(routes_apps.router)


# --- Modo Codigo: proyectos del usuario ---

_code_store = web_tools.EncryptedStore("code_meta.json")
MAX_RECENT_PROJECTS = 10

# El selector de carpetas de Windows, en un proceso aparte (tkinter no puede
# vivir en los hilos del servidor). Sale en el escritorio del usuario tanto si
# usa la app como el navegador.
_PICK_FOLDER_SCRIPT = (
    "import tkinter as tk\nfrom tkinter import filedialog\n"
    "root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)\n"
    "print(filedialog.askdirectory(title='Elige la carpeta del proyecto', mustexist=True) or '')\n")


class CodeProjectRequest(BaseModel):
    path: str


def _project_info(path: str) -> dict:
    p = Path(path)
    if not p.is_dir():
        return {"path": path, "exists": False}
    try:
        entries = sorted((e.name + ("/" if e.is_dir() else "") for e in p.iterdir()
                          if not e.name.startswith(".")), key=str.lower)
    except OSError:
        entries = []
    return {"path": str(p), "name": p.name or str(p), "exists": True, "git": (p / ".git").exists(),
            "agents_md": (p / "AGENTS.md").exists(), "entries": entries[:60], "total": len(entries)}


@app.post("/code/pick_folder")
def code_pick_folder(request: Request):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    try:
        out = subprocess.run([sys.executable, "-c", _PICK_FOLDER_SCRIPT], capture_output=True, text=True,
                             timeout=600, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        return {"path": None}
    path = out.stdout.strip()
    return {"path": str(Path(path)) if path else None}


@app.get("/code/projects")
def code_recent_projects(request: Request):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    recent = _code_store.load("code_projects", session["user_id"], session["dek"], session["key_generation"], [])
    return {"projects": [_project_info(p) for p in recent]}


@app.post("/code/projects")
def code_open_project(request: Request, req: CodeProjectRequest):
    """Abre un proyecto: comprueba la carpeta y la pone la primera de las recientes."""
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    info = _project_info(req.path.strip())
    if not info["exists"]:
        return JSONResponse({"detail": "Esa carpeta no existe."}, status_code=400)
    uid, dek, gen = session["user_id"], session["dek"], session["key_generation"]
    recent = [p for p in _code_store.load("code_projects", uid, dek, gen, []) if p != info["path"]]
    _code_store.save("code_projects", ([info["path"]] + recent)[:MAX_RECENT_PROJECTS], uid, dek, gen)
    return info


@app.post("/code/git_init")
def code_git_init(request: Request, req: CodeProjectRequest):
    """Convierte el proyecto en repositorio git para poder ver y deshacer los
    cambios del agente (OpenCode solo los registra con git). Solo si el
    usuario lo pide desde la interfaz."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    p = Path(req.path)
    if not p.is_dir():
        return JSONResponse({"detail": "Esa carpeta no existe."}, status_code=400)
    if not (p / ".git").exists():
        out = subprocess.run(["git", "init", "-q", str(p)], capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if out.returncode != 0:
            return JSONResponse({"detail": f"git no pudo: {out.stderr.strip()}"}, status_code=400)
    return _project_info(str(p))


@app.get("/code/tasks")
def code_tasks(request: Request, path: str):
    """Conversaciones anteriores en este proyecto."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    return _opencode_call(opencode_client.list_tasks, 20, path)


class AgentFollowUp(BaseModel):
    text: str
    # modo Codigo: "plan" o "build" para cambiar de agente al seguir ("adelante")
    code_mode: str | None = None


@app.post("/agent/tasks/{session_id}/message")
def agent_continue(request: Request, session_id: str, req: AgentFollowUp):
    """Seguir la misma tarea ("corregir o continuar"), sin empezar de cero."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    if not req.text.strip():
        return JSONResponse({"detail": "Escribe que quieres que haga."}, status_code=400)
    agent = None
    if req.code_mode in ("plan", "build"):
        agent = CONFIG["opencode"]["agent_code_plan" if req.code_mode == "plan" else "agent_code"]
    result = _opencode_call(opencode_client.continue_task, session_id, req.text.strip(), agent)
    return result if isinstance(result, JSONResponse) else {"ok": True}


@app.post("/agent/questions/{request_id}")
def agent_reply_question(request: Request, request_id: str, req: AgentQuestionReply):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    result = _opencode_call(opencode_client.reply_question, request_id, req.answers)
    return result if isinstance(result, JSONResponse) else {"ok": True}


@app.post("/agent/questions/{request_id}/reject")
def agent_reject_question(request: Request, request_id: str):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    result = _opencode_call(opencode_client.reject_question, request_id)
    return result if isinstance(result, JSONResponse) else {"ok": True}


@app.post("/agent/permissions/{request_id}")
def agent_reply_permission(request: Request, request_id: str, req: AgentPermissionReply):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    if req.reply not in ("once", "always", "reject"):
        return JSONResponse({"detail": "Respuesta invalida."}, status_code=400)
    result = _opencode_call(opencode_client.reply_permission, request_id, req.reply)
    return result if isinstance(result, JSONResponse) else {"ok": True}


def _source_image_bytes(file_path: str | None, image: UploadFile | None, session: dict) -> bytes:
    """Una imagen ya generada (por su nombre en /media, descifrada con la
    clave de este usuario: la de otro no se puede abrir) o una subida nueva."""
    if file_path:
        data = media_store.load(Path(file_path).name, session["dek"])
        if data is None:
            raise ValueError("Imagen no valida.")
        return data
    if image is not None and image.filename:
        return _read_upload(image)
    raise ValueError("Hay que indicar una imagen (subida o ya generada).")


@app.post("/image/upscale", response_model=ChatResponse)
def image_upscale(request: Request, file_path: str | None = Form(None), image: UploadFile | None = File(None)):
    """Escala x4 una imagen (ya generada, referenciada por su nombre, o una
    subida nueva) sin volver a generarla desde cero."""
    start = time.perf_counter()
    session = request.state.session
    try:
        src_bytes = _source_image_bytes(file_path, image, session)
    except ValueError as exc:
        return ChatResponse(agent_used="image_upscale", response=str(exc), verifier_gated=False)

    try:
        with media_store.plain_copy(src_bytes, ".png") as src:
            img_bytes = image_agent.upscale(str(src))
    except Exception as exc:
        metrics.log_event("image_upscale", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(agent_used="image_upscale",
                             response=_generation_error_message("escalando la imagen", exc),
                             verifier_gated=False)

    metrics.log_event("image_upscale", (time.perf_counter() - start) * 1000)
    return _with_bytes(
        ChatResponse(agent_used="image_upscale", response="Imagen escalada x4.", verifier_gated=False),
        img_bytes, ".png", session,
    )


@app.post("/image/inpaint", response_model=ChatResponse)
def image_inpaint(request: Request, prompt: str = Form(...), mask: UploadFile = File(...),
                   file_path: str | None = Form(None), image: UploadFile | None = File(None),
                   denoise: float = Form(1.0)):
    """Repinta solo la zona marcada en blanco en la mascara (dibujada en la
    interfaz), dejando el resto de la imagen intacto."""
    start = time.perf_counter()
    session = request.state.session
    try:
        src_bytes = _source_image_bytes(file_path, image, session)
        mask_bytes = _read_upload(mask)
    except ValueError as exc:
        return ChatResponse(agent_used="image_inpaint", response=str(exc), verifier_gated=False)

    try:
        with media_store.plain_copy(src_bytes, ".png") as src, media_store.plain_copy(mask_bytes, ".png") as mask_path:
            img_bytes = image_agent.inpaint(prompt, str(src), str(mask_path), denoise=denoise)
    except Exception as exc:
        metrics.log_event("image_inpaint", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(agent_used="image_inpaint",
                             response=_generation_error_message("repintando la imagen", exc),
                             verifier_gated=False)

    metrics.log_event("image_inpaint", (time.perf_counter() - start) * 1000)
    return _with_bytes(
        ChatResponse(agent_used="image_inpaint", response="Zona seleccionada repintada.", verifier_gated=False),
        img_bytes, ".png", session,
    )


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


@app.get("/media/{name}")
def get_media(name: str, request: Request):
    """Imagen/video/audio generado, descifrado con la clave de quien lo pide:
    el de otro usuario no se puede abrir (404, igual que si no existiera)."""
    data = media_store.load(name, request.state.session["dek"])
    if data is None:
        return JSONResponse({"detail": "No encontrado."}, status_code=404)
    headers = {"Cache-Control": "private, max-age=3600", "Accept-Ranges": "bytes"}
    size = len(data)
    rng = re.fullmatch(r"bytes=(\d*)-(\d*)", request.headers.get("range", ""))
    if rng and (rng.group(1) or rng.group(2)):
        # el reproductor de video pide trozos para poder saltar
        if rng.group(1):
            first = int(rng.group(1))
            last = min(int(rng.group(2)), size - 1) if rng.group(2) else size - 1
        else:
            first, last = max(0, size - int(rng.group(2))), size - 1
        if first >= size or first > last:
            return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
        headers["Content-Range"] = f"bytes {first}-{last}/{size}"
        return Response(data[first:last + 1], status_code=206, media_type=media_store.media_type(name),
                        headers=headers)
    return Response(data, media_type=media_store.media_type(name), headers=headers)


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/people")
def get_people(request: Request):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    return people.list_people(user_id=session["user_id"])


@app.post("/people")
def add_person(request: Request, name: str = Form(...), image: UploadFile = File(...)):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    ext = Path(image.filename or "").suffix or ".jpg"
    try:
        people.save_person(name, _read_upload(image), ext, user_id=session["user_id"],
                            dek=session["dek"], key_generation=session["key_generation"])
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "name": name}


@app.delete("/people/{name}")
def remove_person(name: str, request: Request):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    ok = people.delete_person(name, user_id=session["user_id"])
    return {"ok": ok}


@app.get("/people_photo/{name}")
def people_photo(name: str, request: Request):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    image_bytes = people.get_reference_bytes(name, user_id=session["user_id"],
                                              dek=session["dek"], key_generation=session["key_generation"])
    if image_bytes is None:
        return JSONResponse({"error": "no encontrado"}, status_code=404)
    return Response(content=image_bytes, media_type="image/jpeg")


@app.get("/cv")
def get_cv_status(request: Request):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    filename = profile_store.cv_filename(user_id=session["user_id"])
    return {"has_cv": filename is not None, "filename": filename}


@app.post("/cv")
def upload_cv(request: Request, file: UploadFile = File(...)):
    """Sube un CV nuevo; sustituye automaticamente al anterior (solo hay uno activo)."""
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    try:
        content = _read_upload(file, MAX_DOC_BYTES)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=413)
    saved = profile_store.save_cv(file.filename or "cv.pdf", content, user_id=session["user_id"],
                                   dek=session["dek"], key_generation=session["key_generation"])
    return {"ok": True, "filename": saved.name.removesuffix(".enc")}


@app.get("/profile/avatar")
def get_avatar(request: Request):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    data = profile_store.get_avatar_bytes(session["user_id"], session["dek"], session["key_generation"])
    if data is None:
        return JSONResponse({"detail": "Sin avatar."}, status_code=404)
    return Response(content=data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.post("/profile/avatar")
def upload_avatar(request: Request, file: UploadFile = File(...)):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    try:
        profile_store.save_avatar(_read_upload(file), session["user_id"], session["dek"], session["key_generation"])
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"ok": True}


@app.delete("/profile/avatar")
def delete_avatar(request: Request):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    profile_store.delete_avatar(session["user_id"])
    return {"ok": True}


@app.get("/knowledge")
def list_knowledge(request: Request):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    return knowledge_base.list_documents(user_id=session["user_id"])


@app.post("/knowledge")
def add_knowledge(request: Request, file: UploadFile = File(...)):
    """Sube un documento (pdf/docx/txt/md) a la base de conocimiento propia.
    Si ya existia un documento con el mismo nombre, se sustituye."""
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    filename = file.filename or f"{uuid.uuid4().hex}.txt"
    try:
        n_chunks = knowledge_base.add_document(filename, _read_upload(file, MAX_DOC_BYTES), user_id=session["user_id"],
                                                dek=session["dek"], key_generation=session["key_generation"])
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "filename": filename, "chunks": n_chunks}


@app.delete("/knowledge/{filename}")
def delete_knowledge(filename: str, request: Request):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    knowledge_base.delete_document(filename, user_id=session["user_id"])
    return {"ok": True}


@app.get("/memory/entries")
def list_memory_entries(request: Request):
    """Memoria a largo plazo real (RAG): notas manuales + intercambios de
    conversacion indexados - lo que Chati puede recuperar por relevancia en
    respuestas futuras. Ver ROADMAP.md."""
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    return knowledge_base.list_memory_entries(user_id=session["user_id"],
                                               dek=session["dek"], key_generation=session["key_generation"])


@app.post("/memory/entries")
def add_memory_entry(request: Request, text: str = Form(...)):
    """Contexto que el propio usuario escribe a mano para que se recuerde
    siempre (p.ej. su rol, sus preferencias), sin tener que decirlo en cada
    conversacion."""
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    text = text.strip()
    if not text:
        return JSONResponse({"detail": "El texto no puede estar vacio."}, status_code=400)
    entry_id = knowledge_base.add_context_note(text, user_id=session["user_id"],
                                                dek=session["dek"], key_generation=session["key_generation"])
    return {"ok": True, "id": entry_id}


@app.put("/memory/entries/{entry_id}")
def update_memory_entry(entry_id: str, request: Request, text: str = Form(...)):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    text = text.strip()
    if not text:
        return JSONResponse({"detail": "El texto no puede estar vacio."}, status_code=400)
    ok = knowledge_base.update_memory_entry(entry_id, text, user_id=session["user_id"],
                                             dek=session["dek"], key_generation=session["key_generation"])
    if not ok:
        return JSONResponse({"detail": "No se pudo editar (no existe, no es tuyo, o no es una nota manual)."},
                             status_code=404)
    return {"ok": True}


@app.delete("/memory/entries/{entry_id}")
def delete_memory_entry(entry_id: str, request: Request):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    ok = knowledge_base.delete_memory_entry(entry_id, user_id=session["user_id"])
    if not ok:
        return JSONResponse({"detail": "No se encontro esa entrada (o no es tuya)."}, status_code=404)
    return {"ok": True}


# herramientas que tocan el ordenador: solo con permiso (ver _can_use_pc)
PC_TOOLS = {"leer_archivo", "listar_carpeta", "ejecutar_python", "delegar_a_agente_de_codigo"}
# herramientas sobre los datos guardados del usuario: no aplican a invitados
OWN_DATA_TOOLS = {"buscar_en_memoria", "listar_documentos", "listar_personas"}


def _execute_tool(name: str, arguments: dict, session_id: str, user_id: str | None = None,
                   dek: bytes | None = None, key_generation: int | None = None,
                   pc_access: bool = False) -> tuple[str, list[dict] | None]:
    """Ejecuta una herramienta que el agente de texto ha pedido usar. Todas
    son consultas de solo lectura sobre datos que ya existen (memoria,
    documentos, personas guardadas, fecha real), salvo ejecutar_python, que
    corre en un proceso aparte con timeout y directorio temporal (ver
    tools.ejecutar_python) - pensado para verificar calculos, no para tocar
    archivos ni acceder a la red."""
    # el modelo puede pedir una herramienta que no se le ofrecio: se comprueba aqui tambien
    if name in PC_TOOLS and not pc_access:
        return "Esta cuenta no tiene permiso para usar el ordenador (archivos, codigo, agente).", None
    if name in OWN_DATA_TOOLS and not user_id:
        # sin usuario, las busquedas de rag/people devolvian lo de TODOS
        return "En modo invitado no hay memoria, documentos ni personas guardadas.", None
    if name == "actualizar_plan":
        pasos = (arguments or {}).get("pasos", []) if isinstance(arguments, dict) else []
        if not pasos:
            return "Falta la lista de pasos.", None
        plans.set_plan(session_id, pasos)
        text = "Plan actualizado:\n" + "\n".join(
            f"[{p.get('estado', 'pendiente')}] {p.get('texto', '')}" for p in pasos
        )
        return text, None  # no es evidencia factual, es solo estado de tarea

    if name == "fecha_actual":
        text = tools.fecha_actual()
        return text, [{"source": "sistema:fecha_actual", "text": text}]

    if name == "buscar_en_memoria":
        query = (arguments or {}).get("consulta", "") if isinstance(arguments, dict) else ""
        if not query:
            return "Falta indicar que buscar.", None
        results = knowledge_base.query(query, k=4, user_id=user_id, dek=dek, key_generation=key_generation)
        if not results:
            return "No se encontro nada relevante en la memoria ni en los documentos.", None
        text = "\n\n---\n\n".join(f"[{r['source']}] {r['text']}" for r in results)
        return text, results

    if name == "listar_documentos":
        docs = knowledge_base.list_documents(user_id=user_id)
        if not docs:
            return "No hay documentos subidos todavia.", [{"source": "sistema:listar_documentos", "text": "No hay documentos subidos."}]
        text = "Documentos: " + ", ".join(d["filename"] for d in docs)
        return text, [{"source": "sistema:listar_documentos", "text": text}]

    if name == "listar_personas":
        names = [p["name"] for p in people.list_people(user_id=user_id)]
        if not names:
            return "No hay personas guardadas todavia.", [{"source": "sistema:listar_personas", "text": "No hay personas guardadas."}]
        text = "Personas guardadas: " + ", ".join(names)
        return text, [{"source": "sistema:listar_personas", "text": text}]

    if name == "leer_archivo":
        ruta = (arguments or {}).get("ruta", "") if isinstance(arguments, dict) else ""
        if not ruta:
            return "Falta indicar la ruta.", None
        text = file_access.leer_archivo(ruta)
        return text, [{"source": f"archivo:{ruta}", "text": text}]

    if name == "listar_carpeta":
        ruta = (arguments or {}).get("ruta", "") if isinstance(arguments, dict) else ""
        if not ruta:
            return "Falta indicar la ruta.", None
        text = file_access.listar_carpeta(ruta)
        return text, [{"source": f"carpeta:{ruta}", "text": text}]

    if name == "ejecutar_python":
        codigo = (arguments or {}).get("codigo", "") if isinstance(arguments, dict) else ""
        if not codigo:
            return "Falta el codigo a ejecutar.", None
        text = tools.ejecutar_python(codigo)
        return text, [{"source": "sistema:ejecutar_python", "text": text}]

    if name == "delegar_a_agente_de_codigo":
        tarea = (arguments or {}).get("tarea", "") if isinstance(arguments, dict) else ""
        if not tarea:
            return "Falta describir la tarea.", None
        text, task_id = opencode_client.delegate(CONFIG["opencode"]["base_url"], tarea,
                                                 CONFIG["opencode"]["agent"])
        return text, [{"source": "sistema:delegar_a_agente_de_codigo", "text": text, "agent_task_id": task_id}]

    return f"Herramienta desconocida: {name}", None


def _delegated_task_id(evidence: list[dict] | None) -> str | None:
    """Si el modelo de chat uso la herramienta delegar_a_agente_de_codigo,
    el id de la tarea lanzada (para que la interfaz pinte su tarjeta)."""
    return next((e["agent_task_id"] for e in evidence or [] if e.get("agent_task_id")), None)


def _resolve_agent(message: str, agent_override: str | None) -> tuple[str, bool]:
    """Devuelve (agente, es_factual). Una sola llamada al modelo hace las dos
    cosas quando no hay override (ver router.py)."""
    if agent_override in (AGENTS.keys() | {"image", "video", "opencode"}):
        return agent_override, True  # no importa el valor real: solo se usa is_factual para 'text'
    route = router.classify(message)
    return route["agent"], route["factual"]


def _tools_for(agent_name: str, pc_access: bool = True, guest: bool = False) -> list[dict]:
    """El chat de texto no delega en el agente de codigo: en modo Chat, al
    pedirle una imagen, se la pasaba al agente (que tampoco sabe hacerla,
    mejoras.md 2026-09-28). Esa herramienta es solo del agente de programacion.
    Sin permiso para usar el ordenador, fuera tambien archivos y ejecutar codigo."""
    defs = tools.TOOL_DEFS if agent_name == "code" else \
        [t for t in tools.TOOL_DEFS if t["function"]["name"] != "delegar_a_agente_de_codigo"]
    hidden = (set() if pc_access else PC_TOOLS) | (OWN_DATA_TOOLS if guest else set())
    return [t for t in defs if t["function"]["name"] not in hidden]


# Chino, japones o coreano: qwen2.5 a veces se pasa al chino a mitad de una
# respuesta (visto el 2026-09-28, mejoras.md).
_CJK = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]")


def _fix_language(answer: str, message: str, model: str) -> str:
    """Si la respuesta se ha pasado a otro alfabeto (y el usuario no escribio
    en el), el mismo modelo la reescribe entera en español. Si falla, se
    devuelve tal cual: nunca debe romper una respuesta."""
    if not _CJK.search(answer) or _CJK.search(message):
        return answer
    try:
        fixed = ollama.chat(model, [{"role": "user", "content": (
            "Este texto mezcla español con otro idioma. Reescribelo entero solo en español, sin "
            "cambiar el contenido ni el formato (markdown, listas, codigo). Devuelve solo el "
            "texto reescrito.\n\n" + answer)}], temperature=0.2, think=False)
    except Exception:
        return answer
    return fixed.strip() if fixed.strip() and not _CJK.search(fixed) else answer


def _resolve_model_profile(profile: str | None) -> str | None:
    """Convierte un nombre de perfil (rapido/bueno/seguridad) en el modelo
    real a usar, o None para dejar el default configurado del agente (ver
    ROADMAP.md, punto 5c). Perfil desconocido o ausente -> None."""
    profiles = CONFIG["agents"]["text"].get("profiles", {})
    if profile and profile in profiles:
        return profiles[profile]["model"]
    return None


def _resolve_think(profile: str | None) -> bool | None:
    """True/False si el perfil fija explicitamente el modo "pensamiento"
    (ver config.yaml, perfil "seguridad") o None para no forzar nada (deja
    el comportamiento por defecto del modelo)."""
    profiles = CONFIG["agents"]["text"].get("profiles", {})
    if profile and profile in profiles:
        return profiles[profile].get("think")
    return None


def _generate_response(message: str, agent_name: str, is_factual: bool, session_id: str,
                        auth_session: dict, model_profile: str | None = None,
                        verify: bool = True, image_model: str | None = None,
                        video_model: str | None = None) -> ChatResponse:
    """Genera la respuesta y la guarda en memoria (lado 'assistant'). Asume
    que el mensaje de usuario YA se guardo en memoria antes de llamar a esto.
    auth_session es la sesion de autenticacion (dek/key_generation/user_id) -
    ver ROADMAP.md, punto 0, fase 4. model_profile: rapido/bueno/seguridad,
    ver punto 5c. verify=False salta el verificador anti-alucinacion para
    este mensaje concreto - eleccion explicita del usuario (checkbox en la
    interfaz), no algo que decida el sistema por su cuenta. image_model: id
    de model_registry.py para el agente "image", None = automatico."""
    dek = auth_session["dek"]
    key_generation = auth_session["key_generation"]
    user_id = auth_session["user_id"]
    override_model = _resolve_model_profile(model_profile)
    think = _resolve_think(model_profile)

    def _save_assistant_message(content: str, agent: str) -> None:
        memory.add_message(session_id, "assistant", content, agent=agent,
                            dek=dek, key_generation=key_generation, user_id=user_id)

    if agent_name == "image":
        try:
            img_bytes = image_agent.generate(message, model_id=image_model)
        except Exception as exc:
            resp = ChatResponse(agent_used="image", response=_generation_error_message("generando la imagen", exc),
                                 verifier_gated=False, session_id=session_id)
            _save_assistant_message(resp.response, "image")
            return resp
        resp = _with_bytes(
            ChatResponse(agent_used="image", response="Imagen generada.", verifier_gated=False,
                         session_id=session_id),
            img_bytes, ".png", auth_session,
        )
        _save_assistant_message(resp.response, "image")
        return resp

    if agent_name == "video":
        try:
            vid_bytes = video_agent.generate(message, model_id=video_model)
        except Exception as exc:
            resp = ChatResponse(agent_used="video", response=_generation_error_message("generando el video", exc),
                                 verifier_gated=False, session_id=session_id)
            _save_assistant_message(resp.response, "video")
            return resp
        resp = _with_bytes(
            ChatResponse(agent_used="video", response="Video generado.", verifier_gated=False,
                         session_id=session_id),
            vid_bytes, ".mp4", auth_session,
        )
        _save_assistant_message(resp.response, "video")
        return resp

    if agent_name == "opencode":
        task_id, choice = None, None
        if auth_session.get("role") == "guest":
            # el agente toca archivos reales del equipo - igual que las rutas /agent/*
            text = "El agente solo esta disponible para usuarios registrados, no en modo invitado."
        elif not _can_use_pc(auth_session):
            text = "Tu cuenta no tiene permiso para usar el agente (toca archivos del ordenador). Pideselo al administrador."
        else:
            choice = _assess_for_fast_agent(message)
            if choice:
                text = ("Esta tarea parece compleja para el agente rapido: " + (choice["reason"] or "tiene varios pasos.")
                        + " Te recomiendo el modelo potente: se equivoca menos, pero tarda minutos en vez de segundos.")
            else:
                text, task_id = opencode_client.delegate(CONFIG["opencode"]["base_url"], message,
                                                         CONFIG["opencode"]["agent"])
        resp = ChatResponse(agent_used="opencode", response=text, verifier_gated=False,
                            session_id=session_id, agent_task_id=task_id,
                            agent_choice=choice)
        _save_assistant_message(resp.response, "opencode")
        return resp

    if agent_name not in AGENTS:
        resp = ChatResponse(
            agent_used=agent_name,
            response=f"El agente '{agent_name}' todavia no esta conectado al orquestador.",
            verifier_gated=False,
            session_id=session_id,
        )
        _save_assistant_message(resp.response, agent_name)
        return resp

    agent = AGENTS[agent_name]
    raw_history = memory.get_history(session_id, limit=12, dek=dek, key_generation=key_generation)[:-1]
    history = [{"role": h["role"], "content": h["content"]} for h in raw_history]

    if agent_name in ("text", "code"):
        _ensure_active_model(override_model or agent.model)
        pc_access = _can_use_pc(auth_session)
        tool_executor = partial(_execute_tool, session_id=session_id, user_id=user_id,
                                 dek=dek, key_generation=key_generation, pc_access=pc_access)
        docs_context, docs_evidence = _attached_docs(session_id, message, auth_session)
        raw_answer, evidence = agent.respond_with_tools(
            _with_docs(message, docs_context), history, _tools_for(agent_name, pc_access, not user_id), tool_executor,
            model=override_model, think=think)
        evidence = docs_evidence + (evidence or []) or None  # lista vacia -> None, mas explicito para lo que sigue
        raw_answer = _fix_language(raw_answer, message, override_model or agent.model)
    else:
        evidence = None
        raw_answer = agent.respond(message, history=history, context_chunks=None)

    if CONFIG["verifier"]["enabled"] and verify and agent_name in ("text", "code"):
        result = verifier.check(message, raw_answer, is_factual=is_factual, evidence=evidence)
        resp = ChatResponse(
            agent_used=agent_name,
            response=result.answer,
            verifier_gated=result.gated,
            verifier_reason=result.reason,
            session_id=session_id,
            sources=result.sources,
        )
    else:
        resp = ChatResponse(agent_used=agent_name, response=raw_answer, verifier_gated=False, session_id=session_id)
    resp.agent_task_id = _delegated_task_id(evidence)

    _save_assistant_message(resp.response, agent_name)
    if agent_name in ("text", "code") and not resp.verifier_gated:
        # memoria a largo plazo real: indexado para recuperar por relevancia mas adelante,
        # no solo los ultimos N mensajes. No se indexan respuestas bloqueadas (no hay nada que recordar).
        _index_in_background(session_id, message, resp.response, user_id, dek, key_generation)
    return resp


def _run_vision_chat(message: str, image_base64: str, session_id: str | None, auth_session: dict) -> ChatResponse:
    """Comenta una imagen subida - bypasea el router normal (ningun otro
    modelo sabe procesar imagenes), sin herramientas ni bucle. Ver
    ROADMAP.md, punto 5c. Sin model_profile: eso son los perfiles de TEXTO
    (rapido/bueno/seguridad) - aplicarlos aqui era un bug real (encontrado
    en vivo, 2026-09-25): si el perfil de chat activo era "rapido", vision
    intentaba usar qwen2.5:7b (sin soporte de imagenes) en vez del modelo
    de vision configurado, y Ollama lo rechazaba con un 400."""
    session_id = _own_session_id(session_id, auth_session)
    dek, key_generation, user_id = auth_session["dek"], auth_session["key_generation"], auth_session["user_id"]
    memory.add_message(session_id, "user", message or "[imagen adjunta]",
                        dek=dek, key_generation=key_generation, user_id=user_id)
    _ensure_active_model(vision_agent.model)
    start = time.perf_counter()
    try:
        full_text = "".join(vision_agent.respond_with_image_stream(message, image_base64))
    except Exception as exc:
        metrics.log_event("vision", (time.perf_counter() - start) * 1000, False, error=str(exc))
        full_text = f"Fallo analizando la imagen: {exc}"
        memory.add_message(session_id, "assistant", full_text, agent="vision",
                            dek=dek, key_generation=key_generation, user_id=user_id)
        return ChatResponse(agent_used="vision", response=full_text, verifier_gated=False, session_id=session_id)
    memory.add_message(session_id, "assistant", full_text, agent="vision",
                        dek=dek, key_generation=key_generation, user_id=user_id)
    metrics.log_event("vision", (time.perf_counter() - start) * 1000, False)
    return ChatResponse(agent_used="vision", response=full_text, verifier_gated=False, session_id=session_id)


def _stream_vision_chat(message: str, image_base64: str, session_id: str | None, auth_session: dict):
    """Sin model_profile a proposito - ver _run_vision_chat: los perfiles de
    chat (rapido/bueno/seguridad) son de texto, aplicarlos aqui rompia
    vision con un 400 de Ollama si el perfil activo no era el modelo de
    vision."""
    session_id = _own_session_id(session_id, auth_session)
    dek, key_generation, user_id = auth_session["dek"], auth_session["key_generation"], auth_session["user_id"]
    memory.add_message(session_id, "user", message or "[imagen adjunta]",
                        dek=dek, key_generation=key_generation, user_id=user_id)
    yield json.dumps({"type": "start", "agent_used": "vision", "session_id": session_id}) + "\n"
    _ensure_active_model(vision_agent.model)
    start = time.perf_counter()
    full_text = ""
    try:
        for chunk in vision_agent.respond_with_image_stream(message, image_base64):
            full_text += chunk
            yield json.dumps({"type": "chunk", "text": chunk}) + "\n"
    except Exception as exc:
        metrics.log_event("vision", (time.perf_counter() - start) * 1000, False, error=str(exc))
        yield json.dumps({"type": "done", "response": f"Fallo analizando la imagen: {exc}",
                           "verifier_gated": False, "session_id": session_id}) + "\n"
        return
    memory.add_message(session_id, "assistant", full_text, agent="vision",
                        dek=dek, key_generation=key_generation, user_id=user_id)
    metrics.log_event("vision", (time.perf_counter() - start) * 1000, False)
    yield json.dumps({
        "type": "done", "response": full_text, "verifier_gated": False, "verifier_reason": None,
        "sources": None, "session_id": session_id, "corrected": False,
    }) + "\n"


def _run_chat(message: str, agent_override: str | None, session_id: str | None, auth_session: dict,
              model_profile: str | None = None, verify: bool = True,
              image_model: str | None = None, video_model: str | None = None) -> ChatResponse:
    session_id = _own_session_id(session_id, auth_session)
    memory.add_message(session_id, "user", message, dek=auth_session["dek"],
                        key_generation=auth_session["key_generation"], user_id=auth_session["user_id"])
    agent_name, is_factual = _resolve_agent(message, agent_override)
    start = time.perf_counter()
    resp = _generate_response(message, agent_name, is_factual, session_id, auth_session, model_profile,
                               verify, image_model, video_model)
    metrics.log_event(resp.agent_used, (time.perf_counter() - start) * 1000, resp.verifier_gated)
    return resp


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request):
    if req.image_base64:
        return _run_vision_chat(req.message, req.image_base64, req.session_id, request.state.session)
    return _run_chat(req.message, req.agent, req.session_id, request.state.session, req.model_profile, req.verify,
                      req.image_model, req.video_model)


def _stream_chat(message: str, agent_override: str | None, session_id: str | None, auth_session: dict,
                  model_profile: str | None = None, verify: bool = True, image_model: str | None = None,
                  video_model: str | None = None):
    """Generador para /chat/stream: emite lineas JSON. Para texto/codigo,
    transmite el texto segun se genera (menos espera percibida); para
    imagen/video no hay tokens que transmitir, asi que se hace el trabajo
    normal y se manda un solo evento final."""
    dek = auth_session["dek"]
    key_generation = auth_session["key_generation"]
    user_id = auth_session["user_id"]
    override_model = _resolve_model_profile(model_profile)
    think = _resolve_think(model_profile)

    session_id = _own_session_id(session_id, auth_session)
    memory.add_message(session_id, "user", message, dek=dek, key_generation=key_generation, user_id=user_id)

    # si hay que cargar un modelo, la primera respuesta tarda bastante mas
    # (medido: 17s solo en cargar qwen2.5:7b con la RAM llena) - se avisa
    # para que no parezca que la app se ha colgado
    loaded = set(ollama.running_models())
    announced = set()
    if agent_override is None and _LIGHT_MODEL not in loaded:
        announced.add(_LIGHT_MODEL)
        yield _loading_event(_LIGHT_MODEL)

    agent_name, is_factual = _resolve_agent(message, agent_override)
    yield json.dumps({"type": "start", "agent_used": agent_name, "session_id": session_id}) + "\n"
    stream_start = time.perf_counter()

    if agent_name in AGENTS:
        target_model = override_model or AGENTS[agent_name].model
        if target_model not in loaded and target_model not in announced:
            yield _loading_event(target_model)

    if agent_name not in AGENTS:
        # imagen, video, o un agente desconocido: sin streaming real, se reutiliza el pipeline normal
        resp = _generate_response(message, agent_name, is_factual, session_id, auth_session, model_profile,
                                   verify, image_model, video_model)
        metrics.log_event(resp.agent_used, (time.perf_counter() - stream_start) * 1000, resp.verifier_gated)
        yield json.dumps({"type": "done", **resp.model_dump()}) + "\n"
        return

    agent = AGENTS[agent_name]
    raw_history = memory.get_history(session_id, limit=12, dek=dek, key_generation=key_generation)[:-1]
    history = [{"role": h["role"], "content": h["content"]} for h in raw_history]

    docs_context, evidence = _attached_docs(session_id, message, auth_session)
    full_text = ""
    try:
        if agent_name in ("text", "code"):
            _ensure_active_model(override_model or agent.model)
            pc_access = _can_use_pc(auth_session)
            tool_executor = partial(_execute_tool, session_id=session_id, user_id=user_id,
                                     dek=dek, key_generation=key_generation, pc_access=pc_access)
            chunks = agent.respond_with_tools_stream(
                _with_docs(message, docs_context), history, _tools_for(agent_name, pc_access, not user_id), tool_executor, evidence,
                model=override_model, think=think)
        else:
            chunks = agent.respond_stream(message, history=history, context_chunks=None)
        for chunk in chunks:
            full_text += chunk
            yield json.dumps({"type": "chunk", "text": chunk}) + "\n"
    except Exception as exc:
        metrics.log_event(agent_name, (time.perf_counter() - stream_start) * 1000, False, error=str(exc))
        yield json.dumps({"type": "done", "response": f"Fallo generando la respuesta: {exc}",
                           "verifier_gated": False, "session_id": session_id}) + "\n"
        return

    final_response = full_text
    if agent_name in ("text", "code"):
        final_response = _fix_language(full_text, message, override_model or agent.model)
    verifier_gated = False
    verifier_reason = None
    sources = None

    if CONFIG["verifier"]["enabled"] and verify and agent_name in ("text", "code"):
        result = verifier.check(message, final_response, is_factual=is_factual, evidence=evidence)
        final_response = result.answer
        verifier_gated = result.gated
        verifier_reason = result.reason
        sources = result.sources

    memory.add_message(session_id, "assistant", final_response, agent=agent_name,
                        dek=dek, key_generation=key_generation, user_id=user_id)
    if agent_name in ("text", "code") and not verifier_gated:
        _index_in_background(session_id, message, final_response, user_id, dek, key_generation)
    metrics.log_event(agent_name, (time.perf_counter() - stream_start) * 1000, verifier_gated)
    yield json.dumps({
        "type": "done",
        "response": final_response,
        "verifier_gated": verifier_gated,
        "verifier_reason": verifier_reason,
        "sources": sources,
        "session_id": session_id,
        "corrected": verifier_gated or (final_response != full_text),
        "agent_task_id": _delegated_task_id(evidence),
    }) + "\n"


@app.post("/chat/stream")
def chat_stream(req: ChatRequest, request: Request):
    if req.image_base64:
        return StreamingResponse(
            _stream_vision_chat(req.message, req.image_base64, req.session_id, request.state.session),
            media_type="application/x-ndjson",
        )
    return StreamingResponse(
        _stream_chat(req.message, req.agent, req.session_id, request.state.session, req.model_profile, req.verify,
                     req.image_model, req.video_model),
        media_type="application/x-ndjson",
    )


@app.get("/models/image")
def get_image_models():
    models = model_registry.list_image_models()
    return {
        "models": [
            {"id": m.id, "architecture": m.architecture, "architecture_label": m.architecture_label, "label": m.label}
            for m in models
        ],
        "architectures": model_registry.list_image_architectures(),
        "base_folder": str(model_registry.IMG_DIR),
    }


@app.get("/models/video")
def get_video_models():
    models = model_registry.list_video_models()
    return {
        "models": [
            {"id": m.id, "architecture": m.architecture, "architecture_label": m.architecture_label, "label": m.label}
            for m in models
        ],
        "architectures": model_registry.list_video_architectures(),
        "base_folder": str(model_registry.VID_DIR),
    }


@app.get("/personas")
def get_personas(request: Request):
    """Personas de las que ya se pidio crear un LoRA - nombre + estado de
    cada arquitectura (fase 1: solo sdxl, ver ROADMAP.md). Bloqueado en modo
    invitado, igual que la galeria de caras: son fotos personales."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    return {"personas": persona_trainer.list_personas(request.state.session["user_id"])}


@app.post("/personas")
def create_persona(request: Request, name: str = Form(...), photos: list[UploadFile] = File(...)):
    """Crea una persona nueva y lanza el entrenamiento SDXL en segundo plano
    - no espera a que termine (puede tardar horas). Usa GET /personas para
    ver el progreso. Ver aviso de uso responsable en verifier.py/manual.md:
    solo para fotos propias o con consentimiento."""
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    if not name.strip():
        return JSONResponse({"detail": "Falta el nombre de la persona."}, status_code=400)
    if len(photos) < 3:
        return JSONResponse({"detail": "Hacen falta al menos 3 fotos para entrenar algo decente."}, status_code=400)

    tmp_dir = media_store.tmp_dir() / f"persona_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    try:
        for i, photo in enumerate(photos):
            ext = Path(photo.filename or "").suffix or ".jpg"
            dest = tmp_dir / f"{i:03d}{ext}"
            dest.write_bytes(_read_upload(photo))
            saved_paths.append(dest)
        # el entrenamiento necesita la GPU entera: fuera modelos de chat e imagen
        _keep_only_ollama(set(), heavy=None)
        comfyui_client.free_memory(CONFIG["comfyui"]["base_url"])
        result = persona_trainer.start_training(session["user_id"], name, saved_paths)
    except persona_trainer.NoBaseModelError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except persona_trainer.TrainingAlreadyRunningError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)
    finally:
        for p in saved_paths:
            p.unlink(missing_ok=True)
        tmp_dir.rmdir()
    return result


@app.delete("/personas/{name}")
def delete_persona(name: str, request: Request):
    """Borra una persona propia: el LoRA con su cara y lo que quede de sus fotos."""
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    try:
        deleted = persona_trainer.delete_persona(session["user_id"], name)
    except persona_trainer.TrainingAlreadyRunningError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)
    return {"ok": deleted}


@app.delete("/models/image/{model_id}")
def delete_image_model(model_id: str, request: Request):
    """Borra el archivo de un modelo de imagen instalado - libera disco de
    verdad (varios GB). A diferencia de los modelos de texto no hay concepto
    de "en uso": cualquier checkpoint instalado se puede elegir al generar,
    asi que todos son borrables, no solo un subconjunto. Solo admin."""
    if request.state.session["role"] != "admin":
        return JSONResponse({"detail": "Solo un administrador puede borrar modelos."}, status_code=403)
    path = model_registry.image_model_path(model_id)
    if path is None or not path.exists():
        return JSONResponse({"detail": f"Modelo desconocido o no instalado: {model_id}"}, status_code=400)
    try:
        path.unlink()
    except OSError as exc:
        return JSONResponse({"detail": f"No se pudo borrar el archivo: {exc}"}, status_code=502)
    return {"ok": True, "message": f"'{model_id}' borrado."}


@app.delete("/models/video/{model_id}")
def delete_video_model(model_id: str, request: Request):
    if request.state.session["role"] != "admin":
        return JSONResponse({"detail": "Solo un administrador puede borrar modelos."}, status_code=403)
    path = model_registry.video_model_path(model_id)
    if path is None or not path.exists():
        return JSONResponse({"detail": f"Modelo desconocido o no instalado: {model_id}"}, status_code=400)
    try:
        path.unlink()
    except OSError as exc:
        return JSONResponse({"detail": f"No se pudo borrar el archivo: {exc}"}, status_code=502)
    return {"ok": True, "message": f"'{model_id}' borrado."}


# --- Preparar los modelos del modo elegido (desplegable de la izquierda) ---
# Pedido por Sergio 2026-09-28: que cada modo tenga ya cargado lo suyo y
# libere lo de los demas - varias fotos seguidas sin pagar la carga de FLUX
# en la primera, o hablar sin esperar a que se cargue el modelo de texto.
# Se trabaja en segundo plano; si se cambia de modo a medio preparar, solo
# cuenta el ultimo (ticket).

PREPARE_MODES = {"chat", "texto", "image", "video", "agente", "codigo", "voice"}
_prepare_state = {"mode": None, "state": "idle", "detail": None, "label": None, "ticket": 0}
_prepare_lock = threading.Lock()


class PrepareRequest(BaseModel):
    mode: str
    potente: bool = False  # modo Agente con el modelo grande
    model_profile: str | None = None
    image_model: str | None = None
    video_model: str | None = None


def _chat_model_for(profile: str | None) -> str:
    # sin perfil (la interfaz aun no cargo la lista): el perfil por defecto de
    # config.yaml, no el modelo base del agente de texto (30B, mucho mas lento)
    default = CONFIG["agents"]["text"].get("default_profile")
    return _resolve_model_profile(profile or default) or text_agent.model


# lo ultimo que se precargo en ComfyUI: si el modo nuevo usa otro modelo, se
# libera antes - si no, ComfyUI se queda con los dos en RAM
_last_comfy_warmup: tuple[str, str | None] | None = None


def _same_model(loaded: str, wanted: str) -> bool:
    # Ollama lista "nomic-embed-text:latest" aunque se pidiera "nomic-embed-text"
    return loaded == wanted or loaded == f"{wanted}:latest"


def _keep_only_ollama(keep: set[str], heavy: str | None = None) -> None:
    """Descarga de Ollama todo lo que no este en `keep` (RAM y VRAM). El
    modelo del agente se respeta mientras tenga una tarea en marcha."""
    global _active_heavy_model
    keep = set(keep)
    if opencode_client.busy_session_ids(CONFIG["opencode"]["base_url"]):
        keep |= _agent_models()
    with _heavy_model_lock:
        for model in ollama.running_models():
            if not any(_same_model(model, k) for k in keep):
                ollama.unload(model)
        _active_heavy_model = heavy


def _free_comfyui() -> None:
    global _last_comfy_warmup
    running, _ = comfyui_client.user_queue(CONFIG["comfyui"]["base_url"])
    if running:
        return  # nunca a mitad de una imagen/video del usuario ("dejar en segundo plano")
    comfyui_client.free_memory(CONFIG["comfyui"]["base_url"])
    _last_comfy_warmup = None


def _prepare_work(req: PrepareRequest) -> None:
    """Regla: cada modo deja cargado SOLO lo que necesita y descarga el resto
    (RAM y VRAM) - antes, al pasar a Chat se quedaban en RAM el modelo del
    agente (26GB) o el de vision (11GB) si ya estaban cargados."""
    global _last_comfy_warmup
    mode = req.mode
    if mode == "chat":  # automatico: el router usa el modelo ligero, y la respuesta el del perfil
        _free_comfyui()
        target = _chat_model_for(req.model_profile)
        _keep_only_ollama({_LIGHT_MODEL, target, EMBED_MODEL},
                          heavy=target if target != _LIGHT_MODEL else None)
        ollama.preload(_LIGHT_MODEL)
        if target != _LIGHT_MODEL:
            ollama.preload(target)
    elif mode in ("texto", "voice"):
        _free_comfyui()
        target = _chat_model_for(req.model_profile)
        _keep_only_ollama({target, _LIGHT_MODEL, EMBED_MODEL},
                          heavy=target if target != _LIGHT_MODEL else None)
        ollama.preload(target)
    elif mode in ("image", "video"):
        model_id = req.image_model if mode == "image" else req.video_model
        if _last_comfy_warmup not in (None, (mode, model_id)):
            _free_comfyui()  # otro modelo de imagen/video: fuera el anterior
        # los modelos de texto los descarga comfyui_client.before_submit
        if mode == "image":
            # una imagen minima (256px, pocos pasos) deja cargado en la GPU
            # todo lo que usa el modelo elegido
            image_agent.generate(comfyui_client.WARMUP_PROMPT, width=256, height=256, model_id=model_id, timeout=600)
        else:
            video_agent.generate(comfyui_client.WARMUP_PROMPT, width=256, height=160, length=9, steps=2,
                                 model_id=model_id, timeout=600)
        _last_comfy_warmup = (mode, model_id)
    elif mode in ("agente", "codigo"):  # codigo: potente salvo con "Rapido"
        _free_comfyui()
        model = CONFIG["opencode"]["model_potente" if req.potente else "model"]
        # el agente rapido (GPU) no cabe junto al modelo del chat; el potente (CPU) si
        keep = {model, EMBED_MODEL} | ({_LIGHT_MODEL} if req.potente else set())
        _keep_only_ollama(keep, heavy=model)
        ollama.preload(model)


def _prepare_label(req: PrepareRequest) -> str:
    """Que se esta cargando, en palabras del usuario (para el aviso grande)."""
    if req.mode in ("chat", "texto", "voice"):
        return f"el modelo del chat ({_chat_model_for(req.model_profile)})"
    if req.mode == "image":
        entry = model_registry.get_image_model(req.image_model)
        return f"el generador de imagenes ({entry.label})" if entry else "el generador de imagenes"
    if req.mode == "video":
        entry = model_registry.get_video_model(req.video_model)
        return f"el generador de video ({entry.label})" if entry else "el generador de video"
    if req.potente:
        return f"el modelo potente del agente ({CONFIG['opencode']['model_potente']}, ~26 GB)"
    return f"el modelo del agente ({CONFIG['opencode']['model']})"


def _run_prepare(req: PrepareRequest, ticket: int) -> None:
    try:
        _prepare_work(req)
        state, detail = "ready", None
    except Exception as exc:
        state, detail = "error", str(exc)
    with _prepare_lock:
        if _prepare_state["ticket"] == ticket:
            _prepare_state.update(state=state, detail=detail)


@app.get("/work/pending")
def pending_work():
    """Lo que esta en marcha: lo usan el aviso al cambiar de modo (finalizar,
    esperar o segundo plano) y el panel "tareas en segundo plano" de la barra
    lateral, desde el que se puede cerrar cualquier cosa atascada.
    "any" solo cuenta lo que TRABAJA (lo que se veria afectado al cambiar de
    modo): una tarea del agente esperando una respuesta no consume nada."""
    base = CONFIG["opencode"]["base_url"]

    def summary(sid: str) -> str:
        try:
            return opencode_client.task_summary(base, sid)
        except requests.RequestException:
            return ""

    agent_tasks = [{"session_id": sid, "title": summary(sid)}
                   for sid in opencode_client.busy_session_ids(base)]
    waiting_tasks = [{"session_id": sid, "title": summary(sid), "reason": reason}
                     for sid, reason in opencode_client.waiting_sessions(base).items()]
    generations = comfyui_client.user_jobs(CONFIG["comfyui"]["base_url"])
    running = sum(g["state"] == "running" for g in generations)
    queued = sum(g["state"] == "queued" for g in generations)
    return {"agent_tasks": agent_tasks, "waiting_tasks": waiting_tasks, "generations": generations,
            "generations_running": running, "generations_queued": queued,
            "any": bool(agent_tasks or running or queued)}


@app.post("/work/agent/{session_id}/stop")
def stop_agent_task(session_id: str, request: Request):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    result = _opencode_call(opencode_client.stop_task, session_id)
    return result if isinstance(result, JSONResponse) else {"ok": True}


@app.post("/work/generation/{prompt_id}/stop")
def stop_generation(prompt_id: str, request: Request):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    try:
        comfyui_client.stop_job(CONFIG["comfyui"]["base_url"], prompt_id)
    except requests.RequestException as exc:
        return JSONResponse({"detail": f"ComfyUI no responde: {exc}"}, status_code=502)
    return {"ok": True}


@app.post("/work/stop")
def stop_work(request: Request):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    base = CONFIG["opencode"]["base_url"]
    for sid in opencode_client.busy_session_ids(base):
        try:
            opencode_client.abort(base, sid)
        except requests.RequestException:
            pass
    try:
        comfyui_client.stop_everything(CONFIG["comfyui"]["base_url"])
    except requests.RequestException:
        pass
    return {"ok": True}


@app.post("/models/prepare")
def prepare_models(req: PrepareRequest):
    if req.mode not in PREPARE_MODES:
        return JSONResponse({"detail": "Modo desconocido."}, status_code=400)
    with _prepare_lock:
        _prepare_state["ticket"] += 1
        ticket = _prepare_state["ticket"]
        _prepare_state.update(mode=req.mode, state="preparing", detail=None, label=_prepare_label(req))
    threading.Thread(target=_run_prepare, args=(req, ticket), daemon=True).start()
    return {"mode": req.mode, "state": "preparing"}


@app.get("/models/prepare")
def prepare_status():
    with _prepare_lock:
        return {k: v for k, v in _prepare_state.items() if k != "ticket"}


@app.get("/models/profiles")
def get_model_profiles():
    """Perfiles seleccionables para el chat (rapido/bueno/seguridad) - ver
    ROADMAP.md, punto 5c."""
    profiles = CONFIG["agents"]["text"].get("profiles", {})
    return {
        "profiles": [{"id": pid, "label": p["label"]} for pid, p in profiles.items()],
        "default": CONFIG["agents"]["text"].get("default_profile"),
    }


@app.get("/models/roles")
def get_model_roles():
    """Para que sirve cada modelo instalado - derivado de config.yaml (lo que
    realmente esta usando el sistema), no texto libre inventado. Se muestra
    junto a la lista de modelos en la interfaz para que se entienda de un
    vistazo cual es el 'rapido', cual el 'sin censura', etc."""
    text_cfg = CONFIG["agents"]["text"]
    profiles = text_cfg.get("profiles", {})
    roles: dict[str, list[str]] = {}

    def add(model: str | None, desc: str) -> None:
        if not model:
            return
        descs = roles.setdefault(model, [])
        if desc not in descs:
            descs.append(desc)

    add(CONFIG["router"]["model"], "Router (decide que agente atiende cada mensaje)")
    add(CONFIG["verifier"]["model"], "Verificador (revisa que las respuestas no inventen datos)")
    for profile in profiles.values():
        add(profile["model"], f'Chat, perfil "{profile["label"]}"')
    add(text_cfg.get("model"), "Chat de texto (modelo por defecto sin perfil)")
    add(CONFIG["agents"]["code"]["model"], "Programacion")
    add(vision_agent.model, "Vision (comentar fotos que subes)")
    # EMBED_MODEL se usa sin tag explicito (Ollama lo resuelve a ":latest" el
    # solo), pero la lista de modelos instalados si lleva el tag explicito -
    # hay que normalizar para que casen y no salga como "no usado".
    embed_model_id = EMBED_MODEL if ":" in EMBED_MODEL else f"{EMBED_MODEL}:latest"
    add(embed_model_id, "Embeddings (busqueda semantica interna: memoria y documentos)")

    return {model: " · ".join(descs) for model, descs in roles.items()}


@app.get("/models/status")
def get_models_status():
    """Passthrough del 'ollama ps' real - que modelo esta cargado ahora
    mismo, cuanto le queda de keep_alive. Ver ROADMAP.md, punto 5c."""
    try:
        resp = requests.get(f"{CONFIG['ollama']['base_url']}/api/ps", timeout=5)
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as exc:
        return JSONResponse({"detail": f"No se pudo consultar Ollama: {exc}"}, status_code=503)


@app.post("/finetune/export")
def export_finetune_data(request: Request):
    """Exporta el historial propio del usuario a JSONL en formato ChatML,
    listo para cargar en Unsloth Desktop y entrenar un LoRA con sus propias
    conversaciones (ver ROADMAP.md, punto 9). Bloqueado en modo invitado -
    no tiene historial guardado que exportar. Requiere la DEK de la sesion
    actual (zero-knowledge: nadie mas puede generar este export, ni el
    admin)."""
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()

    # tus conversaciones en claro: se mandan y se borran, no se quedan en disco
    out_path = media_store.new_tmp_path(".jsonl")
    try:
        count = finetune_export.export_user_conversations_chatml(
            user_id=session["user_id"], dek=session["dek"], key_generation=session["key_generation"],
            out_path=out_path,
        )
        data = out_path.read_bytes() if out_path.exists() else b""
    finally:
        out_path.unlink(missing_ok=True)
    return Response(data, media_type="application/x-ndjson", headers={
        "Content-Disposition": 'attachment; filename="chati_conversaciones.jsonl"',
        "X-Conversations-Exported": str(count)})


@app.get("/sessions")
def get_sessions(request: Request):
    session = request.state.session
    return memory.list_sessions(user_id=session["user_id"], dek=session["dek"], key_generation=session["key_generation"])


def _attached_docs(session_id: str, message: str, auth_session: dict) -> tuple[str, list[dict]]:
    """Documentos adjuntos con el clip a ESTA conversacion (ver session_docs.py)."""
    if not auth_session.get("user_id"):
        return "", []
    return session_docs.context_for(auth_session["user_id"], session_id, message,
                                    auth_session["dek"], auth_session["key_generation"])


def _with_docs(message: str, docs_context: str) -> str:
    # solo lo que se manda al modelo: en el historial queda el mensaje tal cual
    return f"{message}\n\n{docs_context}" if docs_context else message


def _own_session_id(session_id: str | None, auth_session: dict) -> str:
    """El id de conversacion que manda el cliente, salvo que no sea suyo:
    entonces se empieza una nueva en vez de escribir (y leer) en la ajena.
    Una conversacion sin dueño (de invitado, o antigua de antes de haber
    usuarios) solo la puede seguir la sesion que la empezo: antes cualquiera
    que supiera el id veia su historial (auditoria 2026-09-29)."""
    started = auth_session.setdefault("chat_sessions", set())
    if session_id:
        owner = memory.session_owner(session_id)
        if owner == auth_session["user_id"] and owner is not None:
            return session_id
        if owner is None and (session_id in started or not memory.session_exists(session_id)):
            started.add(session_id)
            return session_id
    new_id = memory.new_session_id()
    started.add(new_id)
    return new_id


def _owns_session(request: Request, session_id: str) -> bool:
    # Bug real (2026-09-25): estas rutas no comprobaban el dueño - cualquier
    # sesion iniciada (incluido invitado) podia borrar o leer conversaciones
    # ajenas conociendo el id.
    user_id = request.state.session["user_id"]
    return user_id is not None and memory.session_owner(session_id) == user_id


def _not_your_session() -> JSONResponse:
    return JSONResponse({"detail": "Conversacion no encontrada."}, status_code=404)


ATTACHED_DOC_PREFIX = "📄 Documento adjuntado: "


@app.post("/sessions/docs")
def attach_session_doc(request: Request, file: UploadFile = File(...), session_id: str | None = Form(None)):
    """Adjunta un documento a una conversacion (el clip). Sin session_id, o
    con el de una conversacion ajena, empieza una nueva - el cliente debe
    usar el session_id devuelto para los mensajes siguientes."""
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    session_id = _own_session_id(session_id, session)
    try:
        doc = session_docs.save(session["user_id"], session_id, file.filename or "documento.txt",
                                _read_upload(file, MAX_DOC_BYTES), session["dek"], session["key_generation"])
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    # queda en el hilo en el momento en que se adjunto (la interfaz lo pinta
    # como tarjeta, ver addLoadedMessage) y el modelo ve en el historial
    # cuando aparecio cada documento
    memory.add_message(session_id, "user", f"{ATTACHED_DOC_PREFIX}{doc['name']}", agent="adjunto",
                       dek=session["dek"], key_generation=session["key_generation"], user_id=session["user_id"])
    return {"session_id": session_id, "document": doc,
            "documents": session_docs.list_docs(session["user_id"], session_id)}


@app.get("/sessions/{session_id}/docs")
def list_session_docs(session_id: str, request: Request):
    session = request.state.session
    if session["role"] == "guest":
        return []
    # cada usuario solo tiene carpeta propia: no hace falta comprobar el dueño
    return session_docs.list_docs(session["user_id"], session_id)


@app.delete("/sessions/{session_id}/docs/{filename}")
def remove_session_doc(session_id: str, filename: str, request: Request):
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    session_docs.remove(session["user_id"], session_id, filename)
    return {"ok": True}


@app.post("/sessions/{session_id}/docs/{filename}/keep")
def keep_session_doc(session_id: str, filename: str, request: Request):
    """Pasa un adjunto de la conversacion a "Mis documentos" (permanente)."""
    session = request.state.session
    if session["role"] == "guest":
        return _guest_blocked()
    content = session_docs.get_original(session["user_id"], session_id, filename,
                                        session["dek"], session["key_generation"])
    if content is None:
        return JSONResponse({"detail": "Documento no encontrado."}, status_code=404)
    knowledge_base.add_document(filename, content, user_id=session["user_id"],
                                dek=session["dek"], key_generation=session["key_generation"])
    return {"ok": True}


@app.get("/sessions/{session_id}")
def get_session_history(session_id: str, request: Request):
    session = request.state.session
    if not _owns_session(request, session_id):
        return _not_your_session()
    return memory.get_history(session_id, limit=200, dek=session["dek"], key_generation=session["key_generation"])


@app.put("/sessions/{session_id}/title")
def rename_session(session_id: str, request: Request, title: str = Form(...)):
    session = request.state.session
    if not _owns_session(request, session_id):
        return _not_your_session()
    title = title.strip()[:80]  # nombres cortos - es una etiqueta para reconocer el hilo, no un resumen
    if not title:
        return JSONResponse({"detail": "El nombre no puede estar vacio."}, status_code=400)
    memory.set_session_title(session_id, title, user_id=session["user_id"],
                              dek=session["dek"], key_generation=session["key_generation"])
    return {"ok": True, "title": title}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str, request: Request):
    if not _owns_session(request, session_id):
        return _not_your_session()
    memory.clear_session(session_id)
    knowledge_base.delete_conversation(session_id)  # tambien borra su rastro de la memoria a largo plazo (RAG)
    session_docs.delete_session(request.state.session["user_id"], session_id)
    return {"ok": True}


@app.put("/sessions/{session_id}/messages/{message_id}")
def edit_message(session_id: str, message_id: int, request: Request, content: str = Form(...)):
    session = request.state.session
    if not _owns_session(request, session_id) or memory.message_session(message_id) != session_id:
        return _not_your_session()
    ok = memory.update_message(message_id, content, dek=session["dek"], key_generation=session["key_generation"])
    return {"ok": ok}


@app.delete("/sessions/{session_id}/messages/{message_id}")
def delete_message(session_id: str, message_id: int, request: Request):
    if not _owns_session(request, session_id) or memory.message_session(message_id) != session_id:
        return _not_your_session()
    ok = memory.delete_message(message_id)
    return {"ok": ok}


@app.post("/voice_chat")
def voice_chat(request: Request, audio: UploadFile = File(...), session_id: str | None = Form(None)):
    """Conversacion por voz: transcribe el audio, lo pasa por el pipeline de
    chat normal (router + verificador + memoria), y devuelve tanto el texto
    como un audio con la respuesta hablada."""
    try:
        audio_bytes = _read_upload(audio)
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=413)
    # lo que dijo el usuario: en claro solo mientras Whisper lo lee
    with media_store.plain_copy(audio_bytes, ".wav") as audio_path:
        transcript = voice_agent.transcribe(str(audio_path))
    if not transcript:
        return {"transcript": "", "agent_used": None, "response": "No se entendio ningun audio.",
                "verifier_gated": False, "file_url": None, "session_id": session_id}

    result = _run_chat(transcript, None, session_id, request.state.session)

    speech_path = media_store.new_tmp_path(".wav")
    voice_agent.speak(result.response, str(speech_path))
    speech_name = media_store.save_file(speech_path, request.state.session["dek"])

    return {
        "transcript": transcript,
        "agent_used": result.agent_used,
        "response": result.response,
        "verifier_gated": result.verifier_gated,
        "verifier_reason": result.verifier_reason,
        "file_url": media_store.url_for(speech_name),
        "session_id": result.session_id,
    }


@app.post("/speak")
def speak(request: Request, text: str = Form(...)):
    out_path = media_store.new_tmp_path(".wav")
    voice_agent.speak(text, str(out_path))
    return {"file_url": media_store.url_for(media_store.save_file(out_path, request.state.session["dek"]))}


def _resolve_reference_bytes(image: UploadFile | None, person_name: str | None, session: dict) -> bytes:
    """Usa la foto subida si la hay, si no busca la persona guardada por
    nombre (descifrando si hace falta con la dek de la sesion actual)."""
    if image is not None and image.filename:
        return _read_upload(image)
    if person_name:
        ref = people.get_reference_bytes(person_name, user_id=session["user_id"],
                                          dek=session["dek"], key_generation=session["key_generation"])
        if ref is None:
            raise ValueError(f"No hay ninguna persona guardada con el nombre '{person_name}'")
        return ref
    raise ValueError("Hay que subir una foto o indicar el nombre de una persona guardada")


@app.post("/image_with_face", response_model=ChatResponse)
def image_with_face(request: Request, prompt: str = Form(...), image: UploadFile | None = File(None),
                     person_name: str | None = Form(None), model_id: str | None = Form(None)):
    """Genera una imagen a partir de una foto adjunta - decide sola que
    tecnica usar segun si la foto tiene una cara humana o no (bug real
    encontrado en vivo, 2026-09-25: pedir "una mariposa similar con los
    colores invertidos" generaba una cara alucinada, porque antes se
    aplicaba FaceID siempre sin comprobar nada):
    - Con cara: preserva esa cara (IPAdapter FaceID). Ver aviso de uso
      responsable en verifier.py/manual.md: solo para fotos propias o con
      consentimiento.
    - Sin cara (mariposa, paisaje, objeto...): sigue la composicion/forma
      de la foto (ControlNet) y deja que el texto controle el color y el
      contenido - lo que de verdad se pedia en ese caso.
    model_id: checkpoint SDXL concreto (de model_registry.py), solo se usa
    en el camino de FaceID; None = el primero instalado."""
    start = time.perf_counter()
    session = request.state.session

    try:
        ref_bytes = _resolve_reference_bytes(image, person_name, session)
    except ValueError as exc:
        metrics.log_event("image_faceid", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(agent_used="image_faceid", response=str(exc), verifier_gated=False)

    if face_detect.has_face(ref_bytes):
        agent_used, response_text = "image_faceid", "Imagen generada preservando la cara de referencia."
        generate = lambda: image_agent.generate_with_face(prompt, ref_bytes, model_id=model_id)
        error_context = "generando la imagen con cara real"
    else:
        agent_used, response_text = "image_controlnet", "Imagen generada a partir de la composicion de la foto."
        generate = lambda: image_agent.generate_with_controlnet(prompt, ref_bytes)
        error_context = "generando la imagen a partir de la foto"

    try:
        img_bytes = generate()
    except Exception as exc:
        metrics.log_event(agent_used, (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(
            agent_used=agent_used,
            response=_generation_error_message(error_context, exc),
            verifier_gated=False,
        )

    metrics.log_event(agent_used, (time.perf_counter() - start) * 1000)
    return _with_bytes(
        ChatResponse(agent_used=agent_used, response=response_text, verifier_gated=False),
        img_bytes, ".png", session,
    )


@app.post("/image_with_persona", response_model=ChatResponse)
def image_with_persona(request: Request, prompt: str = Form(...), persona: str = Form(...),
                       model_id: str | None = Form(None)):
    """Imagen con una persona entrenada (LoRA, ver persona_trainer.py y
    Opciones > Personas). Solo para personas propias o con consentimiento."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    start = time.perf_counter()
    lora = persona_trainer.lora_for(request.state.session["user_id"], persona)
    if lora is None:
        return ChatResponse(agent_used="image_persona", verifier_gated=False,
                            response=f'La persona "{persona}" todavia no esta lista (sigue entrenando o fallo).')
    try:
        img_bytes = image_agent.generate_with_lora(prompt, lora[0], lora[1], model_id=model_id)
    except Exception as exc:
        metrics.log_event("image_persona", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(agent_used="image_persona", verifier_gated=False,
                            response=_generation_error_message("generando la imagen con la persona", exc))
    metrics.log_event("image_persona", (time.perf_counter() - start) * 1000)
    return _with_bytes(
        ChatResponse(agent_used="image_persona", response=f"Imagen generada con {persona}.", verifier_gated=False),
        img_bytes, ".png", request.state.session,
    )


@app.post("/image_with_controlnet", response_model=ChatResponse)
def image_with_controlnet(request: Request, prompt: str = Form(...), image: UploadFile = File(...),
                           control_type: str = Form("canny"), strength: float = Form(0.8)):
    """Genera una imagen nueva siguiendo la composicion/estructura de una
    imagen de referencia subida (ControlNet, no preserva cara ni estilo - ver
    /image_with_face para eso). Por ejemplo: misma pose o mismo encuadre que
    la foto subida, pero con el contenido que describa el prompt."""
    start = time.perf_counter()

    try:
        img_bytes = image_agent.generate_with_controlnet(
            prompt, _read_upload(image), control_type=control_type, strength=strength,
        )
    except ValueError as exc:
        metrics.log_event("image_controlnet", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(agent_used="image_controlnet", response=str(exc), verifier_gated=False)
    except Exception as exc:
        metrics.log_event("image_controlnet", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(
            agent_used="image_controlnet",
            response=_generation_error_message("generando la imagen con composicion guiada", exc),
            verifier_gated=False,
        )

    metrics.log_event("image_controlnet", (time.perf_counter() - start) * 1000)
    return _with_bytes(
        ChatResponse(agent_used="image_controlnet", response="Imagen generada siguiendo la composicion de la referencia.",
                     verifier_gated=False),
        img_bytes, ".png", request.state.session,
    )


@app.post("/video_with_face", response_model=ChatResponse)
def video_with_face(request: Request, prompt: str = Form(...), image: UploadFile | None = File(None),
                     person_name: str | None = Form(None), model_id: str | None = Form(None)):
    """Cadena completa: genera una escena nueva a partir de una foto
    adjunta y despues la anima (image-to-video, LTXV). Decide sola, igual
    que /image_with_face, si preservar una cara (con cara en la foto) o
    seguir su composicion (sin cara - mariposa, paisaje, objeto...). Solo
    para fotos propias o con consentimiento cuando hay cara de por medio.
    model_id: checkpoint SDXL concreto para el fotograma base (de
    model_registry.py), solo se usa en el camino de FaceID."""
    start = time.perf_counter()
    session = request.state.session

    try:
        ref_bytes = _resolve_reference_bytes(image, person_name, session)
    except ValueError as exc:
        metrics.log_event("video_faceid", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(agent_used="video_faceid", response=str(exc), verifier_gated=False)

    try:
        if face_detect.has_face(ref_bytes):
            img_bytes = image_agent.generate_with_face(prompt, ref_bytes, model_id=model_id)
        else:
            img_bytes = image_agent.generate_with_controlnet(prompt, ref_bytes)
    except Exception as exc:
        metrics.log_event("video_faceid", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(
            agent_used="video_faceid",
            response=_generation_error_message("generando la imagen base", exc),
            verifier_gated=False,
        )

    try:
        with media_store.plain_copy(img_bytes, ".png") as frame_path:
            vid_bytes = video_agent.generate_from_image(prompt, str(frame_path))
    except Exception as exc:
        metrics.log_event("video_faceid", (time.perf_counter() - start) * 1000, error=str(exc))
        return _with_bytes(
            ChatResponse(agent_used="video_faceid",
                         response=_generation_error_message("animando la imagen base ya generada", exc),
                         verifier_gated=False),
            img_bytes, ".png", session,
        )

    metrics.log_event("video_faceid", (time.perf_counter() - start) * 1000)
    return _with_bytes(
        ChatResponse(agent_used="video_faceid", response="Video generado preservando la cara de referencia.",
                     verifier_gated=False),
        vid_bytes, ".mp4", session,
    )
