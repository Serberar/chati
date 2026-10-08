import base64
import contextlib
import contextvars
import io
import json
import queue
import re
import secrets
import shutil
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
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import BaseHTTPMiddleware

import auth
import devices
import auth_sessions
import face_detect
import computers
import face_swap
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
import photo_edit
import photo_session
import prompt_writer
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
from agents.comfyui_client import GenerationCancelled
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
import task_owners
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
# y mientras se genera, ningun modelo de Ollama entra en la GPU (chat en la CPU)
OllamaClient.gpu_busy = staticmethod(lambda: comfyui_client.gpu_busy(CONFIG["comfyui"]["base_url"]))
opencode_client.before_task = _free_memory_for_agent

OUTPUT_DIR = paths.OUTPUT_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

@contextlib.asynccontextmanager
async def _lifespan(_app):
    removed = media_store.cleanup_tmp()
    if removed:
        log.info("Temporales huerfanos borrados: %d", removed)
    old_media = media_store.cleanup_old_media()
    if old_media:
        log.info("Imagenes/videos generados de hace mas de %d dias borrados: %d",
                 media_store.MEDIA_MAX_AGE_DAYS, old_media)
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



def _owner_of(session: dict) -> str:
    """Dueño de las generaciones de ComfyUI: el usuario, o esta sesion de
    invitado concreta (los invitados no comparten nada entre ellos)."""
    return session.get("user_id") or f"invitado:{id(session)}"


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
        # un dispositivo vinculado solo lo usa la persona que lo vinculo (ni
        # otro usuario ni un invitado con ese movil)
        device = getattr(request.state, "device", None)
        if device is not None and device["user_id"] != session.get("user_id"):
            return JSONResponse({"detail": "Este dispositivo esta vinculado a otro usuario."}, status_code=403)
        path = request.url.path
        if session["role"] == "guest" and not access.guest_may_use(path):
            return access.guest_blocked()
        if path.startswith(access.PC_PATH_PREFIXES) and not access.can_use_pc(session):
            return access.pc_blocked()
        request.state.session = session
        comfyui_client.current_owner.set(_owner_of(session))  # de quien es lo que se genere
        _mark_activity(True, request.method)
        try:
            return await call_next(request)
        finally:
            _mark_activity(False, request.method)


app.add_middleware(SessionAuthMiddleware)
# la ultima en añadirse es la primera en ejecutarse: Host/Origin antes que nada
app.add_middleware(security.LocalOnlyMiddleware)


def _remote_hosts() -> set[str]:
    """Las direcciones por las que se puede entrar desde fuera (Tailscale):
    las de config.yaml y las de data/remote_hosts.txt; la de este equipo la
    pregunta tambien a Tailscale al arrancar (_add_own_tailscale_host)."""
    hosts = set((CONFIG.get("remote") or {}).get("hosts") or [])
    extra = paths.DATA_DIR / "remote_hosts.txt"
    if extra.exists():
        hosts |= {line.strip() for line in extra.read_text(encoding="utf-8").splitlines() if line.strip()}
    return {h.lower().rstrip(".") for h in hosts}


security.REMOTE_HOSTS |= _remote_hosts()


def _add_own_tailscale_host() -> None:
    # la de este equipo, preguntada a Tailscale (en segundo plano: tarda un
    # par de segundos y no debe retrasar el arranque)
    host = computers.this_host()
    if host:
        security.REMOTE_HOSTS.add(host)
        log.info("Acceso desde fuera por %s", host)


threading.Thread(target=_add_own_tailscale_host, daemon=True).start()


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
    task_owners.forget_user(user_id)


routes_auth.configure(on_login=_on_login, delete_user_data=_delete_user_data)
app.include_router(routes_auth.router)


class ChatRequest(BaseModel):
    # topes: mas que cualquier mensaje real, y la foto como mucho ~25 MB
    message: str = Field(max_length=100_000)
    agent: str | None = None  # si se omite, el router decide
    session_id: str | None = None  # si se omite, se crea una sesion nueva
    model_profile: str | None = None  # rapido/bueno/seguridad, ver ROADMAP.md punto 5c
    image_base64: str | None = Field(None, max_length=35_000_000)  # se comenta con el modelo de vision (no se genera nada)
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
MAX_PERSONA_PHOTOS = 40  # para un LoRA de persona sobran (con 15-20 basta)
MAX_SPEAK_CHARS = 8000  # ~8-10 min de audio: mas que cualquier respuesta


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
    """Lo que se le dice cuando una imagen o un video no sale. La interfaz
    reconoce estos comienzos y pone un boton para reintentar lo mismo sin
    volver a escribirlo (Sergio, 2026-10-07) - ver RETRYABLE en app.js.
    Todo fallo queda en el registro: antes se le decia al usuario y no se
    apuntaba en ningun sitio ("buscar un error a ciegas", 2026-10-07)."""
    if isinstance(exc, photo_edit.NeedsClarification):
        # no es un fallo: no ha entendido la peticion y lo pregunta
        log.info("Edicion: pregunta en vez de editar: %s", exc)
        return str(exc)
    if isinstance(exc, GenerationCancelled):
        log.warning("Generacion cancelada (%s): %s", action, exc)
    else:
        log.error("Fallo %s: %s", action, exc, exc_info=exc)
    if isinstance(exc, GenerationCancelled):
        return "Se ha cancelado la generación. Puedes reintentarlo con el botón."
    if "no termino" in str(exc):
        return ("No ha terminado a tiempo: el equipo estaba demasiado ocupado. "
                "Puedes reintentarlo con el botón.")
    return f"No se ha podido terminar ({action}): {exc}"


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


# Pantalla de inicio de la app del movil: elige en que ordenador entrar
# (C:/AI/inicio, publicada en GitHub Pages; Sergio, 2026-10-08)
LAUNCHER_ORIGIN = "https://serberar.github.io"


@app.get("/health")
def health(request: Request, busy: bool = False):
    """Publica (la usa el vigilante cada 30 s: tiene que ser instantanea, y
    la otra Chati por Tailscale para saber si esta esta encendida).
    Con ?busy=true dice ademas si hay algo en marcha (antes de la copia de
    seguridad), que consulta a OpenCode y ComfyUI y puede tardar: solo desde
    este ordenador."""
    if busy and security.is_local_host(request.headers.get("host", "")):
        return {"status": "ok", "busy": _is_busy()}
    response = JSONResponse({"status": "ok"})
    if request.headers.get("origin") == LAUNCHER_ORIGIN:
        # la pantalla de inicio del movil (Serberar.github.io) pregunta a
        # cada ordenador si esta encendido; solo ella y solo aqui
        response.headers["Access-Control-Allow-Origin"] = LAUNCHER_ORIGIN
    return response


@app.get("/computers")
def computers_list():
    """Los ordenadores con Chati (este y los otros de Tailscale) y si estan
    encendidos: para elegir en cual trabajar (ver computers.py)."""
    return computers.list_computers()


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
    # solo la API de chat (/v1/...): con "../api/pull" se llegaba a la de
    # administrar modelos de Ollama (auditoria 2026-10-05)
    if not re.fullmatch(r"[A-Za-z0-9_\-/.]*", path) or ".." in path or "//" in path:
        return JSONResponse({"detail": "Ruta no permitida."}, status_code=400)
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
def metrics_summary(request: Request, last_n: int = 2000):
    """Uso de todo el sistema (todos los usuarios): solo el administrador."""
    if request.state.session["role"] != "admin":
        return JSONResponse({"detail": "Solo un administrador puede ver esto."}, status_code=403)
    return metrics.summary(last_n=min(max(last_n, 1), 20000))


@app.get("/plan/{session_id}")
def get_plan(session_id: str, request: Request):
    # el plan dice que se esta haciendo: solo para quien lleva esa conversacion
    auth_session = request.state.session
    if not (_owns_session(request, session_id) or session_id in auth_session.get("chat_sessions", set())):
        return {"pasos": []}
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
def cancel_generation(request: Request):
    """Para las generaciones de imagen/video de quien lo pide (en marcha o en
    cola). Antes cortaba lo que estuviera corriendo en ComfyUI, fuera de quien
    fuera: un invitado podia parar las de los demas (auditoria 2026-10-05)."""
    n = comfyui_client.stop_owned(CONFIG["comfyui"]["base_url"], _owner_of(request.state.session))
    log.info("Detener: %d generacion(es) paradas, pedido desde %s", n, _where(request))
    return {"ok": True}


def _where(request: Request) -> str:
    """Desde donde llega una peticion, para el registro: el ordenador o el
    dispositivo vinculado. Sin esto no se podia saber quien paraba las
    ediciones (dos cortadas por "interrumpir", 2026-10-07)."""
    device = getattr(request.state, "device", None)
    return f"el dispositivo vinculado '{device['name']}'" if device else "el ordenador"


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


def _owns_task(request: Request, session_id: str) -> bool:
    s = request.state.session
    return task_owners.may_use(session_id, s.get("user_id"), s.get("role") == "admin")


def _owns_request(request: Request, kind: str, request_id: str) -> bool:
    session_id = opencode_client.request_session(CONFIG["opencode"]["base_url"], kind, request_id)
    return bool(session_id) and _owns_task(request, session_id)


def _not_your_task() -> JSONResponse:
    return JSONResponse({"detail": "Tarea no encontrada."}, status_code=404)


def _own_tasks(request: Request, tasks):
    """Solo las tareas de quien pregunta (antes se veian las de todos)."""
    if isinstance(tasks, JSONResponse):
        return tasks
    return [t for t in tasks if _owns_task(request, t["session_id"])]


@app.get("/agent/web-login")
def agent_web_login(request: Request):
    """Usuario y contraseña de la vista avanzada de OpenCode (Opciones), solo
    para el administrador: OpenCode tiene contraseña desde la auditoria."""
    if request.state.session["role"] != "admin":
        return JSONResponse({"detail": "Solo el administrador."}, status_code=403)
    return {"user": opencode_client.AUTH[0], "password": opencode_client.AUTH[1]}


@app.get("/agent/tasks")
def agent_list_tasks(request: Request):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    return _own_tasks(request, _opencode_call(opencode_client.list_tasks))


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
        if isinstance(result, JSONResponse):
            return result
        task_owners.record(result, request.state.session["user_id"])
        return {"session_id": result}
    if not req.potente and not req.confirmed:
        choice = _assess_for_fast_agent(task)
        if choice:
            return {"needs_choice": True, **choice, "attachments": req.attachments}
    agent = CONFIG["opencode"]["agent_potente" if req.potente else "agent"]
    note = agent_attachments.task_note(req.attachments)
    result = _opencode_call(opencode_client.start_task, f"{task}\n\n{note}" if note else task, agent)
    if isinstance(result, JSONResponse):
        return result
    task_owners.record(result, request.state.session["user_id"])
    return {"session_id": result}


@app.get("/agent/tasks/{session_id}")
def agent_task_view(request: Request, session_id: str):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    if not _owns_task(request, session_id):
        return _not_your_task()
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
    if not _owns_task(request, session_id):
        return _not_your_task()
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
    if not _owns_task(request, session_id):
        return _not_your_task()
    return _opencode_call(opencode_client.get_changes, session_id)


@app.post("/agent/tasks/{session_id}/revert")
def agent_task_revert(request: Request, session_id: str):
    """Deshacer lo que la tarea cambio en la carpeta de trabajo."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    if not _owns_task(request, session_id):
        return _not_your_task()
    if not opencode_client.workdir_undoable():
        return JSONResponse({"detail": "No se puede deshacer: falta git en este equipo."}, status_code=400)
    result = _opencode_call(opencode_client.revert_task, session_id)
    return result if isinstance(result, JSONResponse) else {"ok": True}


@app.post("/agent/tasks/{session_id}/unrevert")
def agent_task_unrevert(request: Request, session_id: str):
    """Rehacer: vuelve a poner lo que se deshizo."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    if not _owns_task(request, session_id):
        return _not_your_task()
    result = _opencode_call(opencode_client.unrevert_task, session_id)
    return result if isinstance(result, JSONResponse) else {"ok": True}


@app.post("/agent/tasks/{session_id}/abort")
def agent_abort(request: Request, session_id: str):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    if not _owns_task(request, session_id):
        return _not_your_task()
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
        # "--": una carpeta que empiece por "-" no se toma como opcion de git
        out = subprocess.run(["git", "init", "-q", "--", str(p.resolve())], capture_output=True, text=True,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if out.returncode != 0:
            return JSONResponse({"detail": f"git no pudo: {out.stderr.strip()}"}, status_code=400)
    return _project_info(str(p))


@app.get("/code/tasks")
def code_tasks(request: Request, path: str):
    """Conversaciones anteriores en este proyecto."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    return _own_tasks(request, _opencode_call(opencode_client.list_tasks, 20, path))


class AgentFollowUp(BaseModel):
    text: str
    # modo Codigo: "plan" o "build" para cambiar de agente al seguir ("adelante")
    code_mode: str | None = None


@app.post("/agent/tasks/{session_id}/message")
def agent_continue(request: Request, session_id: str, req: AgentFollowUp):
    """Seguir la misma tarea ("corregir o continuar"), sin empezar de cero."""
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    if not _owns_task(request, session_id):
        return _not_your_task()
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
    if not _owns_request(request, "question", request_id):
        return _not_your_task()
    result = _opencode_call(opencode_client.reply_question, request_id, req.answers)
    return result if isinstance(result, JSONResponse) else {"ok": True}


@app.post("/agent/questions/{request_id}/reject")
def agent_reject_question(request: Request, request_id: str):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    if not _owns_request(request, "question", request_id):
        return _not_your_task()
    result = _opencode_call(opencode_client.reject_question, request_id)
    return result if isinstance(result, JSONResponse) else {"ok": True}


@app.post("/agent/permissions/{request_id}")
def agent_reply_permission(request: Request, request_id: str, req: AgentPermissionReply):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    if req.reply not in ("once", "always", "reject"):
        return JSONResponse({"detail": "Respuesta invalida."}, status_code=400)
    if not _owns_request(request, "permission", request_id):
        return _not_your_task()
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


MAX_UPSCALE_PIXELS = 4_200_000  # x4 por lado: ~67 MP de salida, lo que aguanta la GPU de 8 GB


def _check_upscale_size(data: bytes) -> None:
    """Una foto de 8000x8000 salia de 32000x32000 y tumbaba ComfyUI (auditoria 2026-10-05)."""
    from PIL import Image, UnidentifiedImageError
    try:
        w, h = Image.open(io.BytesIO(data)).size
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("El archivo no es una imagen valida.") from exc
    if w * h > MAX_UPSCALE_PIXELS:
        raise ValueError(f"La imagen ya es muy grande para escalarla x4 ({w}x{h}); "
                         f"como mucho unos {MAX_UPSCALE_PIXELS // 1_000_000} megapixeles.")


@app.post("/image/upscale", response_model=ChatResponse)
def image_upscale(request: Request, file_path: str | None = Form(None), image: UploadFile | None = File(None)):
    """Escala x4 una imagen (ya generada, referenciada por su nombre, o una
    subida nueva) sin volver a generarla desde cero."""
    start = time.perf_counter()
    session = request.state.session
    try:
        src_bytes = _source_image_bytes(file_path, image, session)
        _check_upscale_size(src_bytes)
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


# Iconos y manifiesto para añadir Chati a la pantalla de inicio (iPhone,
# Android): se abre a pantalla completa como una app. Publicos (ver
# security.PAIR_PATHS): el movil los pide sin la cookie del dispositivo.
@app.get("/apple-touch-icon.png")
@app.get("/apple-touch-icon-precomposed.png")
def apple_touch_icon():
    return FileResponse(Path(__file__).parent / "static" / "apple-touch-icon.png", media_type="image/png")


@app.get("/favicon.ico")
def favicon():
    return FileResponse(Path(__file__).parent / "static" / "logo.ico", media_type="image/x-icon")


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({
        "name": "Chati IA", "short_name": "Chati", "start_url": "/", "display": "standalone",
        "background_color": "#14161a", "theme_color": "#14161a",
        "icons": [{"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
                  {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"}],
    }, media_type="application/manifest+json")


@app.get("/admin/errors")
def admin_errors(request: Request):
    """Los ultimos avisos y errores (errores.log), lo mas nuevo primero, para
    verlos desde Opciones sin abrir archivos. Solo administradores."""
    if request.state.session["role"] != "admin":
        return JSONResponse({"detail": "Solo para administradores."}, status_code=403)
    path = logging_setup.ERROR_FILE
    if not path.exists():
        return {"entries": []}
    text = path.read_text(encoding="utf-8", errors="replace")[-200_000:]
    # una entrada por linea que empieza con fecha; las trazas van con la suya
    entries: list[str] = []
    for line in text.splitlines():
        if re.match(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d", line) or not entries:
            entries.append(line)
        else:
            entries[-1] += "\n" + line
    return {"entries": entries[::-1][:300]}


# --- Errores de la pantalla (el navegador, tambien el del movil) ---

class ClientLog(BaseModel):
    message: str = Field("", max_length=2000)
    source: str = Field("", max_length=300)
    stack: str = Field("", max_length=4000)
    page: str = Field("", max_length=300)


_client_log_times: list[float] = []


@app.post("/client-log")
def client_log(body: ClientLog, request: Request):
    """Un error que ha pasado en la pantalla (JavaScript, una conexion que se
    corta...). Sin esto, lo que fallaba en Safari del iPhone no llegaba a
    ningun registro ("TypeError: Load failed", 2026-10-07). Sin sesion: puede
    fallar antes de entrar. Limitado a 30 por minuto."""
    now = time.time()
    while _client_log_times and _client_log_times[0] < now - 60:
        _client_log_times.pop(0)
    if len(_client_log_times) >= 30:
        return {"ok": False}
    _client_log_times.append(now)
    device = getattr(request.state, "device", None)
    where = f"el dispositivo '{device['name']}'" if device else "el ordenador"
    log.warning("Error en la pantalla (%s, %s): %s | %s%s", where, body.page or "?", body.message,
                body.source, f"\n{body.stack}" if body.stack else "")
    return {"ok": True}


# --- Dispositivos vinculados: usar Chati desde el movil (devices.py) ---

@app.get("/pair")
def pair_page():
    return FileResponse(Path(__file__).parent / "static" / "pair.html")


class PairClaim(BaseModel):
    code: str
    name: str = "Dispositivo"


@app.post("/pair/claim")
def pair_claim(body: PairClaim, request: Request):
    """El movil presenta el codigo que enseña el ordenador y recibe su llave
    (cookie). Solo tiene sentido llegando por la direccion de fuera."""
    if not security.is_remote_host(request.headers.get("host", "")):
        return JSONResponse({"detail": "Abre esta pagina desde el dispositivo que quieres vincular."},
                            status_code=400)
    token = devices.claim(body.code, body.name)
    if token is None:
        return JSONResponse({"detail": "Codigo no valido o caducado. Pide otro en el ordenador."},
                            status_code=403)
    resp = JSONResponse({"ok": True})
    resp.set_cookie(devices.COOKIE_NAME, token, max_age=400 * 24 * 3600, httponly=True, secure=True,
                    samesite="strict", path="/")
    return resp


@app.post("/devices/pair")
def devices_pair(request: Request):
    """Codigo para vincular un dispositivo nuevo: solo desde el propio
    ordenador y con la sesion de un usuario (no un invitado)."""
    session = request.state.session
    if getattr(request.state, "device", None) is not None:
        return JSONResponse({"detail": "Los dispositivos se vinculan desde el ordenador."}, status_code=403)
    if not session.get("user_id"):
        return JSONResponse({"detail": "Hay que iniciar sesion."}, status_code=403)
    if not security.REMOTE_HOSTS:
        return JSONResponse({"detail": "Falta la direccion de Tailscale de este ordenador (data/remote_hosts.txt)."},
                            status_code=400)
    import segno
    pairing = devices.start_pairing(session["user_id"])
    host = sorted(security.REMOTE_HOSTS)[0]
    # el secreto va tras "#": el navegador no lo manda al servidor ni queda
    # en ningun registro; pair.js lo lee y lo envia en el cuerpo
    url = f"https://{host}/pair#{pairing['secret']}"
    qr = segno.make(url, error="m").svg_data_uri(scale=6, border=2)
    return {"url": f"https://{host}/pair", "qr": qr, "code": pairing["short"],
            "expires_in": pairing["expires_in"]}


@app.get("/devices")
def devices_list(request: Request):
    session = request.state.session
    if not session.get("user_id"):
        return []
    current = getattr(request.state, "device", None)
    return [dict(d, current=bool(current and current["id"] == d["id"]))
            for d in devices.list_for(session["user_id"])]


@app.delete("/devices/{device_id}")
def devices_revoke(device_id: str, request: Request):
    session = request.state.session
    if not session.get("user_id") or not devices.revoke(device_id, session["user_id"]):
        return JSONResponse({"detail": "No existe ese dispositivo."}, status_code=404)
    return {"ok": True}


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
    ext = Path(image.filename or "").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):  # va en el nombre del archivo guardado
        ext = ".jpg"
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
        task_owners.record(task_id, user_id)
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

    def _save_assistant_message(content: str, agent: str, media: str | None = None) -> None:
        memory.add_message(session_id, "assistant", content, agent=agent,
                            dek=dek, key_generation=key_generation, user_id=user_id, media=media)

    if agent_name == "image":
        try:
            prompt, (width, height) = prompt_writer.image_prompt(ollama, CONFIG["router"]["model"], message)
            img_bytes = image_agent.generate(prompt, width=width, height=height, model_id=image_model)
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
        _save_assistant_message(resp.response, "image", media=resp.file_path)
        if model_registry.get_edit_model() is not None:
            # la imagen generada pasa a ser la foto de la conversacion: "ahora
            # ponle un sombrero" la edita en vez de generar otra desde cero
            photo_session.start(auth_session, session_id, resp.file_path, "generada")
        return resp

    if agent_name == "video":
        try:
            vid_bytes = video_agent.generate(prompt_writer.video_prompt(ollama, CONFIG["router"]["model"], message),
                                             model_id=video_model)
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
                task_owners.record(task_id, auth_session.get("user_id"))
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


def _wants_photo_edit(message: str) -> bool:
    """Foto recien adjuntada: ¿pide cambiarla o pregunta por ella? El mismo
    clasificador que las correcciones (el de antes tomaba "que nos abracemos"
    por una pregunta y el modelo de vision contestaba que no podia, 2026-10-06)."""
    if model_registry.get_edit_model() is None or not (message or "").strip():
        return False
    return photo_edit.photo_intent(ollama, CONFIG["router"]["model"], message) in ("editar", "repetir", "nueva")


def _decode_photo(image_base64: str) -> bytes | None:
    """None si el adjunto no es base64 valido (antes: error 500)."""
    try:
        return base64.b64decode(image_base64, validate=True)
    except ValueError:
        return None


def _image_mode_without_editor(message: str, image_base64: str, session_id: str,
                               auth_session: dict) -> ChatResponse:
    """Modo Imagen con foto pero sin el editor (Kontext) instalado: como antes,
    una imagen nueva con esa cara (FaceID) o con su composicion (ControlNet)."""
    photo = _decode_photo(image_base64)
    if photo is None:
        return _assistant_reply("La foto adjunta no se ha podido leer. Vuelve a adjuntarla.", "image",
                                session_id, auth_session)
    agent_used, response_text, error_context, ext, generate = _image_from_photo(message, photo, None)
    try:
        img_bytes = generate()
    except Exception as exc:
        return _assistant_reply(_generation_error_message(error_context, exc), agent_used, session_id, auth_session)
    name = media_store.save(img_bytes, ext, auth_session["dek"])
    return _assistant_reply(response_text, agent_used, session_id, auth_session, media=name)


def _start_conversation_photo(auth_session: dict, session_id: str, image_base64: str, message: str) -> str | None:
    """Foto adjunta en el chat: queda como la foto de la conversacion (para
    seguir editandola y para verla en el historial) y se apunta el mensaje.
    Devuelve su nombre, o None si no se guarda (sin editor o foto ilegible)."""
    name = _store_photo(auth_session, _decode_photo(image_base64))
    memory.add_message(session_id, "user", message or "[imagen adjunta]", dek=auth_session["dek"],
                       key_generation=auth_session["key_generation"], user_id=auth_session["user_id"], media=name)
    if name:
        photo_session.start(auth_session, session_id, name, "subida")
    return name


def _photo_edit_reply(message: str, base_name: str, session_id: str, auth_session: dict,
                      face_name: str | None = None, earlier: list[str] | None = None) -> ChatResponse:
    """Edita la foto `base_name` (guardada en media_store) siguiendo `message`
    y la deja como version nueva de la foto de la conversacion. face_name: la
    original, de donde sale la cara. earlier: lo pedido antes sobre ella (si
    ya llevaba sombrero, "quita a la otra persona" no debe quitarselo)."""
    start = time.perf_counter()
    photo = media_store.load(base_name, auth_session["dek"])
    face = media_store.load(face_name, auth_session["dek"]) if face_name and face_name != base_name else None
    if photo is None:
        return _assistant_reply("Ya no tengo esa foto (se borran a los 30 dias). Vuelve a adjuntarla y dime el cambio.",
                                "image_edit", session_id, auth_session)
    try:
        img_bytes, done = _edit_photo(message, photo, face, earlier)
    except Exception as exc:
        metrics.log_event("image_edit", (time.perf_counter() - start) * 1000, error=str(exc))
        return _assistant_reply(_generation_error_message("editando la foto", exc), "image_edit",
                                session_id, auth_session)
    metrics.log_event("image_edit", (time.perf_counter() - start) * 1000)
    name = media_store.save(img_bytes, ".jpg", auth_session["dek"])
    photo_session.push(auth_session, session_id, name, message)
    return _assistant_reply(done, "image_edit", session_id, auth_session, media=name)


def _run_vision_chat(message: str, image_base64: str, session_id: str | None, auth_session: dict,
                     agent_override: str | None = None) -> ChatResponse:
    """Comenta una imagen subida - bypasea el router normal (ningun otro
    modelo sabe procesar imagenes), sin herramientas ni bucle. Ver
    ROADMAP.md, punto 5c. Sin model_profile: eso son los perfiles de TEXTO
    (rapido/bueno/seguridad) - aplicarlos aqui era un bug real (encontrado
    en vivo, 2026-09-25): si el perfil de chat activo era "rapido", vision
    intentaba usar qwen2.5:7b (sin soporte de imagenes) en vez del modelo
    de vision configurado, y Ollama lo rechazaba con un 400."""
    session_id = _own_session_id(session_id, auth_session)
    dek, key_generation, user_id = auth_session["dek"], auth_session["key_generation"], auth_session["user_id"]
    name = _start_conversation_photo(auth_session, session_id, image_base64, message)
    if name and (agent_override == "image" or _wants_photo_edit(message)):
        return _photo_edit_reply(message, name, session_id, auth_session)
    if agent_override == "image":
        return _image_mode_without_editor(message, image_base64, session_id, auth_session)
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


def _stream_vision_chat(message: str, image_base64: str, session_id: str | None, auth_session: dict,
                        agent_override: str | None = None):
    """Sin model_profile a proposito - ver _run_vision_chat: los perfiles de
    chat (rapido/bueno/seguridad) son de texto, aplicarlos aqui rompia
    vision con un 400 de Ollama si el perfil activo no era el modelo de
    vision."""
    session_id = _own_session_id(session_id, auth_session)
    dek, key_generation, user_id = auth_session["dek"], auth_session["key_generation"], auth_session["user_id"]
    name = _start_conversation_photo(auth_session, session_id, image_base64, message)
    # modo Imagen con foto: siempre se edita (en el automatico, solo si pide un cambio)
    if name and (agent_override == "image" or _wants_photo_edit(message)):
        yield from _stream_with_progress(lambda: _photo_edit_reply(message, name, session_id, auth_session),
                                         "image_edit", session_id)
        return
    if agent_override == "image":
        yield from _stream_with_progress(
            lambda: _image_mode_without_editor(message, image_base64, session_id, auth_session), "image", session_id)
        return
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
    followup = _photo_followup(auth_session, message, agent_override, session_id)
    if followup and followup[0] != "nueva":
        return _photo_followup_reply(message, *followup, session_id, auth_session)
    agent_name, is_factual = ("image", False) if followup else _resolve_agent(message, agent_override)
    start = time.perf_counter()
    resp = _generate_response(message, agent_name, is_factual, session_id, auth_session, model_profile,
                               verify, image_model, video_model)
    metrics.log_event(resp.agent_used, (time.perf_counter() - start) * 1000, resp.verifier_gated)
    return resp


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest, request: Request):
    if req.image_base64:
        return _run_vision_chat(req.message, req.image_base64, req.session_id, request.state.session, req.agent)
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

    followup = _photo_followup(auth_session, message, agent_override, session_id)
    if followup and followup[0] != "nueva":
        yield from _stream_with_progress(
            lambda: _photo_followup_reply(message, *followup, session_id, auth_session), "image_edit", session_id)
        return
    if followup:  # una imagen nueva con una foto en marcha: al generador, sin pasar por el router
        agent_override = "image"

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
        log.exception("Fallo generando la respuesta del chat")
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
            _stream_vision_chat(req.message, req.image_base64, req.session_id, request.state.session, req.agent),
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
    if len(photos) > MAX_PERSONA_PHOTOS:
        return JSONResponse({"detail": f"Como mucho {MAX_PERSONA_PHOTOS} fotos."}, status_code=400)

    tmp_dir = media_store.tmp_dir() / f"persona_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    try:
        for i, photo in enumerate(photos):
            ext = Path(photo.filename or "").suffix.lower()
            if ext not in (".jpg", ".jpeg", ".png", ".webp"):  # va en el nombre del archivo
                ext = ".jpg"
            dest = tmp_dir / f"{i:03d}{ext}"
            saved_paths.append(dest)  # antes de escribir: si falla a medias, tambien se borra
            dest.write_bytes(_read_upload(photo))
        # el entrenamiento necesita la GPU entera: fuera modelos de chat e imagen
        _keep_only_ollama(set(), heavy=None)
        comfyui_client.free_memory(CONFIG["comfyui"]["base_url"])
        result = persona_trainer.start_training(session["user_id"], name, saved_paths)
    except persona_trainer.NoBaseModelError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except persona_trainer.TrainingAlreadyRunningError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)  # fotos en claro: fuera pase lo que pase
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
        log.warning("No se pudo preparar el modo %s: %s", req.mode, exc)
        state, detail = "error", str(exc)
    with _prepare_lock:
        stale = _prepare_state["ticket"] != ticket
        now_generating = _prepare_state["mode"] in ("image", "video")
        if not stale:
            _prepare_state.update(state=state, detail=detail)
    # Al abrir la pagina (Automatico carga el chat) y pasar enseguida a Imagen,
    # la carga del chat terminaba DESPUES y volvia a meter qwen3:8b en la GPU a
    # mitad de la imagen de FLUX, que pasaba de segundos a minutos (auditoria
    # 2026-09-29, GPU al 95%). Si ya se esta en Imagen/Video, lo que acaba de
    # cargar un modo anterior se descarga.
    if stale and now_generating and req.mode not in ("image", "video"):
        _keep_only_ollama(set(), heavy=None)


@app.get("/work/pending")
def pending_work(request: Request):
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

    # solo las tareas propias (antes cualquiera, invitados incluidos, veia los
    # titulos de las tareas del agente de todos)
    agent_tasks = [{"session_id": sid, "title": summary(sid)}
                   for sid in opencode_client.busy_session_ids(base) if _owns_task(request, sid)]
    waiting_tasks = [{"session_id": sid, "title": summary(sid), "reason": reason}
                     for sid, reason in opencode_client.waiting_sessions(base).items()
                     if _owns_task(request, sid)]
    generations = [g for g in comfyui_client.user_jobs(CONFIG["comfyui"]["base_url"])
                   if _may_stop_generation(request, g["id"])]
    running = sum(g["state"] == "running" for g in generations)
    queued = sum(g["state"] == "queued" for g in generations)
    return {"agent_tasks": agent_tasks, "waiting_tasks": waiting_tasks, "generations": generations,
            "generations_running": running, "generations_queued": queued,
            "any": bool(agent_tasks or running or queued)}


@app.post("/work/agent/{session_id}/stop")
def stop_agent_task(session_id: str, request: Request):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    if not _owns_task(request, session_id):
        return _not_your_task()
    result = _opencode_call(opencode_client.stop_task, session_id)
    return result if isinstance(result, JSONResponse) else {"ok": True}


def _may_stop_generation(request: Request, prompt_id: str) -> bool:
    """Las propias; las sin dueño conocido (de antes de un reinicio), solo el admin."""
    owner = comfyui_client.owner_of(prompt_id)
    session = request.state.session
    return owner == _owner_of(session) or (owner is None and session["role"] == "admin")


@app.post("/work/generation/{prompt_id}/stop")
def stop_generation(prompt_id: str, request: Request):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    if not _may_stop_generation(request, prompt_id):
        return JSONResponse({"detail": "Esa generacion no es tuya."}, status_code=403)
    try:
        comfyui_client.stop_job(CONFIG["comfyui"]["base_url"], prompt_id)
    except requests.RequestException as exc:
        return JSONResponse({"detail": f"ComfyUI no responde: {exc}"}, status_code=502)
    log.info("Detener (tareas en segundo plano): generacion %s, pedido desde %s", prompt_id, _where(request))
    return {"ok": True}


@app.post("/work/stop")
def stop_work(request: Request):
    if request.state.session["role"] == "guest":
        return _guest_blocked()
    base = CONFIG["opencode"]["base_url"]
    for sid in opencode_client.busy_session_ids(base):
        if not _owns_task(request, sid):
            continue  # las tareas de otros no se paran desde aqui
        try:
            opencode_client.abort(base, sid)
        except requests.RequestException:
            pass
    try:  # solo lo propio: antes vaciaba la cola de ComfyUI entera, de todos
        n = comfyui_client.stop_owned(CONFIG["comfyui"]["base_url"], _owner_of(request.state.session))
        log.info("Detener todo: %d generacion(es) paradas, pedido desde %s", n, _where(request))
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
    sid = _resolve_session_id(session_id, auth_session)
    # para el boton "Ver" de las tareas en marcha
    comfyui_client.note_conversation(comfyui_client.current_owner.get(), sid)
    return sid


def _resolve_session_id(session_id: str | None, auth_session: dict) -> str:
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
    _delete_conversation(request.state.session, session_id)
    return {"ok": True}


@app.delete("/sessions")
def delete_all_sessions(request: Request):
    """Todas las conversaciones del usuario de una vez, para cuando se
    acumulan (Sergio, 2026-10-07). Solo las suyas: sin usuario, list_sessions
    devolveria las de todos, asi que no se hace."""
    session = request.state.session
    if not session.get("user_id"):
        return JSONResponse({"detail": "Hay que iniciar sesion para borrar el historial."}, status_code=403)
    ids = [s["session_id"] for s in memory.list_sessions(limit=1_000_000, user_id=session["user_id"])]
    for session_id in ids:
        _delete_conversation(session, session_id)
    return {"ok": True, "deleted": len(ids)}


def _delete_conversation(session: dict, session_id: str) -> None:
    # las fotos de la conversacion (subidas, ediciones y sus versiones) tambien,
    # no a los 30 dias de la limpieza general
    names = {m["media"] for m in memory.get_history(session_id, limit=100000) if m.get("media")}
    state = photo_session.load(session, session_id)
    names |= set(state["versions"]) if state else set()
    for name in names:
        media_store.delete(name)
    memory.clear_session(session_id)
    knowledge_base.delete_conversation(session_id)  # tambien borra su rastro de la memoria a largo plazo (RAG)
    session_docs.delete_session(session["user_id"], session_id)


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
    try:
        with media_store.plain_copy(audio_bytes, ".wav") as audio_path:
            transcript = voice_agent.transcribe(str(audio_path))
    except RuntimeError as exc:  # la voz no puede cargar en este equipo
        return JSONResponse({"detail": str(exc)}, status_code=503)
    if not transcript:
        return {"transcript": "", "agent_used": None, "response": "No se entendio ningun audio.",
                "verifier_gated": False, "file_url": None, "session_id": session_id}

    result = _run_chat(transcript, None, session_id, request.state.session)

    speech_path = media_store.new_tmp_path(".wav")
    try:
        voice_agent.speak(result.response, str(speech_path))
        speech_name = media_store.save_file(speech_path, request.state.session["dek"])
    except RuntimeError:
        speech_path.unlink(missing_ok=True)
        speech_name = None  # se contesta por escrito aunque no se pueda hablar

    return {
        "transcript": transcript,
        "agent_used": result.agent_used,
        "response": result.response,
        "verifier_gated": result.verifier_gated,
        "verifier_reason": result.verifier_reason,
        "file_url": media_store.url_for(speech_name) if speech_name else None,
        "session_id": result.session_id,
    }


@app.post("/speak")
def speak(request: Request, text: str = Form(...)):
    out_path = media_store.new_tmp_path(".wav")
    try:
        # tope: sin el, un texto de varios MB tenia a Piper trabajando sin fin (auditoria 2026-10-05)
        voice_agent.speak(text[:MAX_SPEAK_CHARS], str(out_path))
    except RuntimeError as exc:
        out_path.unlink(missing_ok=True)
        return JSONResponse({"detail": str(exc)}, status_code=503)
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


_PHOTO_EDITED = "Foto editada."


def _edit_summary(steps) -> str:
    """Lo que se ha hecho, dicho por el planificador: el texto fijo de antes
    ("Lo que no pediste cambiar se ha dejado igual") no decia nada y, si se
    saltaba media peticion, no habia forma de saber que parte (Sergio,
    2026-10-05)."""
    done = [s.summary for s in steps if s.summary]
    if not done:
        return _PHOTO_EDITED
    text = "; ".join(done).rstrip(". ")
    return f"Listo: {text}."


def _store_photo(auth_session: dict, photo: bytes | None) -> str | None:
    """Guarda (cifrada) una foto subida para poder seguir editandola y verla
    en el historial. Solo con el editor instalado: sin el no hay nada que
    seguir editando y no se guarda."""
    if photo is None or model_registry.get_edit_model() is None:
        return None
    try:
        return media_store.save(photo_edit.to_jpeg(photo_edit.load_rgb(photo)), ".jpg", auth_session["dek"])
    except Exception:
        log.warning("No se pudo guardar la foto subida para seguir editandola", exc_info=True)
        return None


def _photo_followup(auth_session: dict, message: str, agent_override: str | None,
                    chat: str | None) -> tuple[str, dict] | None:
    """Mensaje sin foto en una conversacion que ya tiene una (subida, editada o
    generada): (que quiere, estado de la foto) o None si no va de la foto.
    Que quiere: editar, deshacer, original, repetir o nueva (ver
    photo_edit.photo_intent). Va por conversacion y se guarda en la base de
    datos: antes vivia en la sesion de login, se perdia al reiniciar y en el
    modo automatico "haz que..." acababa en el agente (Sergio, 2026-10-05)."""
    if agent_override not in (None, "image") or model_registry.get_edit_model() is None:
        return None
    state = photo_session.load(auth_session, chat)
    if state is None:
        return None
    intent = photo_edit.photo_intent(ollama, CONFIG["router"]["model"], message,
                                     photo_session.last_request(state))
    if intent == "otra":
        return None  # sigue el camino normal: el router, o en el modo Imagen una imagen nueva
    return intent, state  # "nueva": quien llama la manda a generar


_UNDONE = {"deshacer": "He vuelto a la version anterior.", "original": "He vuelto a la foto original."}


def _photo_followup_reply(message: str, intent: str, state: dict, session_id: str,
                          auth_session: dict) -> ChatResponse:
    if intent == "preguntar":
        return _ask_about_photo(message, photo_session.current(state), session_id, auth_session)
    if intent in _UNDONE:
        name = photo_session.move(auth_session, session_id, "original" if intent == "original" else "anterior")
        if name is None:
            text = "Esta ya es la foto original: no hay nada que deshacer." if intent == "original" \
                else "No hay ninguna version anterior a esta."
            return _assistant_reply(text, "image_edit", session_id, auth_session)
        return _assistant_reply(_UNDONE[intent], "image_edit", session_id, auth_session, media=name)
    request = message
    if intent == "repetir":
        # otra variante del ultimo cambio, desde la version de antes de el
        request = photo_session.last_request(state) or message
        if photo_session.previous(state) is not None:
            photo_session.move(auth_session, session_id, "anterior")
            state = photo_session.load(auth_session, session_id)
    return _photo_edit_reply(request, photo_session.current(state), session_id, auth_session,
                             face_name=photo_session.original(state),
                             earlier=photo_session.requests_so_far(state))


def _ask_about_photo(message: str, name: str, session_id: str, auth_session: dict) -> ChatResponse:
    """Pregunta sobre la foto de la conversacion ("¿que lleva puesto?"): la
    contesta el modelo de vision mirando la version que se ve ahora. El de
    texto no ve la foto y se inventaba la respuesta."""
    photo = media_store.load(name, auth_session["dek"])
    if photo is None:
        return _assistant_reply("Ya no tengo esa foto. Vuelve a adjuntarla.", "vision", session_id, auth_session)
    _ensure_active_model(vision_agent.model)
    try:
        text = "".join(vision_agent.respond_with_image_stream(message, base64.b64encode(photo).decode()))
    except Exception as exc:
        text = f"Fallo mirando la foto: {exc}"
    return _assistant_reply(text, "vision", session_id, auth_session)


def _assistant_reply(text: str, agent: str, session_id: str, auth_session: dict,
                     media: str | None = None) -> ChatResponse:
    memory.add_message(session_id, "assistant", text, agent=agent, dek=auth_session["dek"],
                       key_generation=auth_session["key_generation"], user_id=auth_session["user_id"],
                       media=media)
    resp = ChatResponse(agent_used=agent, response=text, verifier_gated=False, session_id=session_id)
    if media:
        resp.file_path, resp.file_url = media, media_store.url_for(media)
    return resp


def _edit_photo(request_text: str, photo: bytes, face_photo: bytes | None = None,
                earlier: list[str] | None = None) -> tuple[bytes, str]:
    """Edicion de una foto real (ver photo_edit.py): el modelo del router pasa
    la peticion a instrucciones en ingles, Kontext edita y luego se vuelve a
    poner la original en todo lo que no se pidio cambiar. Devuelve un JPEG a la
    resolucion de la foto y que se ha hecho. face_photo: de donde sacar la cara
    exacta (la original de la conversacion, no la ya editada). earlier: lo
    pedido antes sobre esta foto, en orden ("mas grande" se refiere a eso).
    De una en una (_edit_lock)."""
    if not _edit_lock.acquire(blocking=False):
        _report("Hay otra edicion en marcha: la tuya va justo despues…")
        _edit_lock.acquire()
    try:
        return _edit_photo_locked(request_text, photo, face_photo, earlier or [])
    finally:
        _edit_lock.release()


# Lo que se va haciendo en una edicion larga (~4 min), para que la interfaz lo
# enseñe en vez de solo "generando..." (ver _stream_with_progress)
_progress: contextvars.ContextVar = contextvars.ContextVar("photo_progress", default=None)


def _report(text: str) -> None:
    callback = _progress.get()
    if callback:
        callback(text)


STREAM_HEARTBEAT_SECONDS = 10


def _stream_with_progress(work, agent_used: str, session_id: str):
    """Ejecuta work() (que devuelve un ChatResponse) en un hilo y va mandando
    sus avisos de _report como eventos "status" del streaming."""
    events: queue.Queue = queue.Queue()
    result: dict = {}

    def run():
        _progress.set(events.put)
        # el streaming corre fuera del contexto donde _own_session_id apunto la
        # conversacion: sin esto el boton "Ver" de la tarea no salia
        comfyui_client.current_conversation.set(session_id)
        try:
            result["resp"] = work()
        except Exception as exc:  # noqa: BLE001 - se le cuenta al usuario
            log.exception("Fallo editando la foto")
            result["error"] = exc

    yield json.dumps({"type": "start", "agent_used": agent_used, "session_id": session_id}) + "\n"
    # copy_context: el hilo hereda el dueño de las generaciones (comfyui_client.current_owner)
    thread = threading.Thread(target=contextvars.copy_context().run, args=(run,), daemon=True)
    thread.start()
    last_sent = time.monotonic()
    while thread.is_alive() or not events.empty():
        try:
            yield json.dumps({"type": "status", "text": events.get(timeout=1)}) + "\n"
            last_sent = time.monotonic()
        except queue.Empty:
            # latido: durante los 2-3 min de Kontext no habia nada que mandar y
            # Safari en el iPhone (o el proxy de Tailscale) cortaba la conexion
            # a los ~60 s en silencio: "TypeError: Load failed" (2026-10-07).
            # La interfaz ignora este tipo de evento.
            if time.monotonic() - last_sent >= STREAM_HEARTBEAT_SECONDS:
                yield json.dumps({"type": "ping"}) + "\n"
                last_sent = time.monotonic()
    thread.join()
    if "error" in result:
        yield json.dumps({"type": "done", "response": _generation_error_message("editando la foto", result["error"]),
                          "verifier_gated": False, "session_id": session_id}) + "\n"
        return
    yield json.dumps({"type": "done", **result["resp"].model_dump()}) + "\n"


# Dos ediciones a la vez se quitaban la GPU (8 GB) la una a la otra: el
# modelo de vision o el planificador de una se cargaba en mitad del Kontext
# de la otra y una pasada pasaba de 2,5 a casi 10 min (2026-10-05, 29 s/it).
# En fila, cada una tarda lo suyo.
_edit_lock = threading.Lock()


def _edit_photo_locked(request_text: str, photo: bytes, face_photo: bytes | None,
                       earlier: list[str]) -> tuple[bytes, str]:
    t0 = time.perf_counter()
    _report("Mirando la foto…")
    _free_comfyui()  # FLUX o Kontext de antes en la GPU: la vision iria a la CPU, varias veces mas lenta
    scene = photo_edit.describe_photo(ollama, vision_agent.model, photo)
    ollama.unload(vision_agent.model)  # fuera de la GPU antes del traductor y de Kontext
    t1 = time.perf_counter()
    _report("Pensando como hacer el cambio…")
    faces = face_detect.face_positions(photo)
    steps = photo_edit.plan_edit(ollama, CONFIG["router"]["model"], request_text, faces, scene, earlier)
    if len(faces) <= 1:
        # una sola pasada (la mitad de tiempo) solo con una persona: con un grupo,
        # ropa y fondo a la vez hacian que Kontext recolocara a la gente y cambiara
        # caras (2026-10-06); en dos pasos el fondo vuelve a pegar a cada uno en su sitio
        steps = photo_edit.merge_steps(steps)
    if not faces:
        # el modo "fondo" recorta a las PERSONAS (MODNet) y las vuelve a pegar: con
        # un perro o un objeto ese recorte no vale y lo teñia de amarillo (2026-10-06)
        for step in steps:
            if step.mode == "fondo":
                step.mode = "local"
    log.info("Edicion: describir %.0f s, planificar %.0f s", t1 - t0, time.perf_counter() - t1)
    for step in steps:  # para poder ver despues que se le pidio a Kontext
        log.info("Edicion: paso %s: %s", step.mode, step.instruction)
    _report("Editando la foto (2-3 minutos)…")
    result = _apply_edit(steps, photo, face_photo, request_text, earlier)
    summary = _edit_summary(steps)
    if photo_edit.barely_changed(photo, result):
        # la misma foto redibujada ("con manchas"): mejor decirlo que darla
        log.info("Edicion: el resultado es casi la misma foto, no se entrega")
        raise photo_edit.NeedsClarification(
            "No he conseguido hacer el cambio: la foto me ha salido prácticamente igual. "
            "¿Me lo pides de otra forma o con más detalle (qué cambiar, cómo y dónde)?")

    # ¿se ve TODO lo pedido? (Sergio: "ha hecho la mitad de lo que he pedido")
    t3 = time.perf_counter()
    _report("Comprobando que se ve todo lo que pediste…")
    asked_en = photo_edit.change_requested(steps)
    # se juzga contra lo que pidio el usuario (y lo que se le dice que se hizo),
    # no contra la instruccion detallada: con "la mano por encima de la cabeza,
    # el brazo izquierdo relajado" daba por mal un saludo bien hecho (2026-10-06)
    done_es = "; ".join(s.summary for s in steps if s.summary)
    wanted = f"{request_text} ({done_es})" if done_es else request_text
    _free_comfyui()  # sin Kontext en la GPU, la vision va en segundos
    # quitar algo no se comprueba: ver que algo NO esta le cuesta al modelo de
    # vision y decia "no se ha quitado" con la persona y el gorro ya quitados
    # (2026-10-06), repitiendo 3,5 min y avisando en falso
    verdict = None if photo_edit.is_removal(asked_en) else \
        photo_edit.verify_edit(ollama, vision_agent.model, result, wanted)
    log.info("Edicion: comprobar %.0f s -> %s", time.perf_counter() - t3,
             None if verdict is None else (verdict.ok, verdict.problem_en))
    if verdict is not None and not verdict.ok:
        _report(f"No ha salido del todo bien ({verdict.problem_es or 'falta algo'}): lo repito corrigiendolo…")
        ollama.unload(vision_agent.model)
        retry = [photo_edit.EditPlan(photo_edit.insist(s.instruction, verdict.problem_en), s.mode, s.summary)
                 for s in steps]
        second = _apply_edit(retry, photo, face_photo, request_text, earlier)
        _free_comfyui()
        second_verdict = photo_edit.verify_edit(ollama, vision_agent.model, second, wanted)
        log.info("Edicion: reintento -> %s", None if second_verdict is None else second_verdict.ok)
        if second_verdict is not None and second_verdict.ok:
            result, verdict = second, second_verdict
    ollama.unload(vision_agent.model)
    if verdict is not None and not verdict.ok:
        # ni al segundo intento: no se entrega una foto mal hecha, se dice
        # que ha fallado (Sergio, 2026-10-08: "si no puede hacerlo que lo diga")
        problem = verdict.problem_es or "no se ve todo lo que pediste"
        raise photo_edit.NeedsClarification(
            f"No me ha salido bien ni repitiéndolo: {problem[0].lower() + problem[1:]}. "
            "Prefiero no darte una foto mal hecha. Si me lo pides de otra forma o con más detalle, lo intento otra vez.")
    return result, summary


def _apply_edit(steps, photo: bytes, face_photo: bytes | None, request_text: str, earlier: list[str]) -> bytes:
    """Los pasos de Kontext sobre la foto, la original vuelta a poner en lo que
    no se pidio cambiar y la cara exacta de antes. Devuelve un JPEG.
    La cara sale de la original; si ya se cambio a proposito en un paso
    anterior (gafas, barba...), de la foto de partida, que ya lo lleva: antes
    se dejaba la de Kontext y en cada edicion siguiente se parecia un poco
    menos (2026-10-06)."""
    if not photo_edit.wants_face_kept(request_text):
        face_src, keep_hair = None, False  # este cambio es en la cara: la nueva se queda
    elif photo_edit.wants_face_kept(" ".join(earlier)):
        face_src, keep_hair = face_photo or photo, photo_edit.wants_hair_kept(" ".join(earlier + [request_text]))
    else:
        face_src, keep_hair = photo, photo_edit.wants_hair_kept(request_text)
    current = photo_edit.load_rgb(photo)
    head_touched = False
    for step in steps:
        # Solo ropa: Kontext genera solo el cuerpo y la cabeza de la foto no se
        # toca (ni se pega despues). Pegar la cara sobre un cuerpo generado
        # entero no casaba: otro tono, otro cuello, la cabeza "de lado" sobre
        # un cuello recto (Sergio, 2026-10-07).
        mask = (photo_edit.clothes_mask(current, step.instruction)
                if photo_edit.is_clothes_only(step, request_text) else None)
        reference = current
        if mask is None and photo_edit.is_pose_change(step.instruction) and not _editor_is_qwen():
            # Kontext copia la inclinacion del selfie: de pie quedaba la cabeza
            # torcida (Sergio, 2026-10-08). Ve la cabeza ya recta. Qwen no lo
            # necesita: endereza la cabeza y deja la copa el solo.
            reference = _pose_reference(current, request_text)
        t = time.perf_counter()
        edited_png = image_agent.edit_with_kontext(
            step.instruction, photo_edit.prepare_for_kontext(reference),
            mask_png=None if mask is None else photo_edit.mask_png_for_kontext(mask))
        log.info("Edicion: Kontext %.0f s%s", time.perf_counter() - t, " (solo el cuerpo)" if mask is not None else "")
        t = time.perf_counter()
        edited = _upscaled(current, edited_png)
        if reference is not current:
            # control de calidad: con la cabeza aun torcida, la cara pegada
            # sigue esa inclinacion y queda rara ("la cara esta rara con
            # inclinacion extraña", Sergio, 2026-10-08). Otra vez (otra
            # semilla) y se queda la mas recta. Se mide ya ampliada: en la de
            # Kontext (1 MP) una cara de cuerpo entero es tan pequeña que no
            # se detectaba y contaba como recta.
            tilt = photo_edit.max_head_tilt(edited)
            if tilt > photo_edit.HEAD_TILT_MIN:
                _report("La cabeza ha salido torcida, repitiendo…")
                t = time.perf_counter()
                retry = _upscaled(current, image_agent.edit_with_kontext(
                    step.instruction, photo_edit.prepare_for_kontext(reference)))
                retry_tilt = photo_edit.max_head_tilt(retry)
                log.info("Edicion: cabeza torcida %.0f grados, repetida (%.0f grados) %.0f s",
                         tilt, retry_tilt, time.perf_counter() - t)
                if retry_tilt < tilt:
                    edited = retry
            t = time.perf_counter()
        if mask is not None:
            current, mode = photo_edit.compose_masked(current, edited, mask), "cuerpo"
        else:
            mode = "entera" if photo_edit.is_pose_change(step.instruction) else step.mode
            if mode == "fondo" and face_swap.available():
                # otra escena: la de Kontext entera (luz y angulo de la escena) y
                # luego los rasgos; pegar a la persona original sobre el fondo
                # nuevo quedaba como un recorte con halo (Sergio, 2026-10-08)
                mode = "entera"
            current = photo_edit.finish(current, edited, mode)
            head_touched = True
        log.info("Edicion: componer (%s) %.0f s", mode, time.perf_counter() - t)
    if face_src is not None and head_touched:
        # Regla por cara y por angulo medido (Sergio, 2026-10-08):
        # - mismo angulo: la cara exacta de antes, no la redibujada por Kontext
        #   (si cambio la postura o la escena, solo la cara: pelo y contorno
        #   nuevos, ver restore_faces);
        # - cabeza girada de otra forma: no encaja, se clonan sus rasgos sobre
        #   la cara de Kontext (face_swap.py).
        # Antes se clonaba siempre que cambiaba la postura o el fondo y la cara
        # salia mas ancha y no era el ("sigue poniendome la cara rara").
        t = time.perf_counter()
        source = photo_edit.load_rgb(face_src)
        faces_o, faces_r = photo_edit._faces(source), photo_edit._faces(current)
        turned_o, turned_r = photo_edit.turned_faces(faces_o, faces_r)
        pose = any(photo_edit.is_pose_change(s.instruction) or s.mode == "fondo" for s in steps)
        moved = any(photo_edit.is_pose_change(s.instruction) for s in steps)
        if moved and _editor_is_qwen():
            # (solo si cambia la postura: en un cambio de sitio el encuadre es
            # el mismo y va la cara original, abajo; con la de Qwen Mon en las
            # piramides se parecia 0,68, 2026-10-08)
            # Qwen dibuja la cara con el angulo y la luz de la postura nueva y
            # se parece (0,89 de frente, prueba del 2026-10-08): pegar la
            # original encima era lo que dejaba la mascara. Solo se repasan
            # las caras que se le parecen poco: repasar una que ya estaba bien
            # la cambiaba ("porque me cambia la cara", de pie: 0,81 -> 0,70);
            # en el paracaidas, diminuta, si la mejoraba (0,41 -> 0,77).
            weak_o, weak_r = _weak_faces(source, current, faces_o, faces_r)
            if weak_r and face_swap.available():
                current = _detail_faces(current, source, weak_o, weak_r, steps[-1].instruction)
                log.info("Edicion: %d cara(s) poco parecidas repasadas %.0f s", len(weak_r), time.perf_counter() - t)
            return photo_edit.to_jpeg(current)
        if pose and face_swap.available() and photo_edit.face_shrunk(faces_o, faces_r):
            # otro encuadre (cuerpo entero...): la cara pegada era una mascara;
            # la de Kontext repintada con detalle y con los rasgos
            current = _detail_faces(current, source, faces_o, faces_r, steps[-1].instruction)
            log.info("Edicion: caras repintadas con detalle y rasgos %.0f s", time.perf_counter() - t)
            return photo_edit.to_jpeg(current)
        seams: list = []
        current = photo_edit.restore_faces(source, current, keep_hair=keep_hair,
                                           face_only=pose, seams=seams)
        log.info("Edicion: caras %.0f s", time.perf_counter() - t)
        current = _repaint_seam(current, seams)
        if turned_r and face_swap.available():
            t = time.perf_counter()
            current = face_swap.swap_faces(source, current, turned_o, turned_r)
            log.info("Edicion: rasgos clonados en %d cara(s) giradas %.0f s", len(turned_r), time.perf_counter() - t)
    return photo_edit.to_jpeg(current)


WEAK_LIKENESS = 0.6  # parecido ArcFace por debajo del cual se repasa la cara


def _weak_faces(source, current, faces_o: list, faces_r: list) -> tuple[list, list]:
    """Las parejas (original, resultado) cuya cara se parece poco, emparejadas
    de izquierda a derecha. Sin el modelo de rasgos, ninguna."""
    if not face_swap.available():
        return [], []
    order_o = sorted(faces_o, key=lambda f: f[0] + f[2] / 2)
    order_r = sorted(faces_r, key=lambda f: f[0] + f[2] / 2)
    weak_o, weak_r = [], []
    for fo, fr in zip(order_o, order_r):
        likeness = face_swap.similarity(source, fo, current, fr)
        log.info("Edicion: parecido de la cara de Qwen %.2f", likeness)
        if likeness < WEAK_LIKENESS:
            weak_o.append(fo)
            weak_r.append(fr)
    return weak_o, weak_r


def _editor_is_qwen() -> bool:
    entry = model_registry.get_edit_model()
    return entry is not None and entry.architecture == "qwen"


def _detail_faces(current, source, faces_o: list, faces_r: list, instruction: str):
    """Cada cara del resultado: recortada y ampliada, repintada con detalle por
    Kontext y con los rasgos de su pareja en la original (de izquierda a
    derecha) puestos ya a buena resolucion; luego devuelta a su sitio. Si el
    repintado falla, solo los rasgos."""
    order_o = sorted(faces_o, key=lambda f: f[0] + f[2] / 2)
    order_r = sorted(faces_r, key=lambda f: f[0] + f[2] / 2)
    for fo, fr in zip(order_o, order_r):
        box, crop_png, mask_png, mask = photo_edit.detail_crop(current, fr)
        _report("Afinando la cara…")
        crop = photo_edit.load_rgb(crop_png)
        try:
            refined = photo_edit.load_rgb(image_agent.refine_with_kontext(
                photo_edit.detail_prompt(instruction), crop_png, mask_png, denoise=photo_edit.DETAIL_DENOISE))
            refined = photo_edit.blend_resized(crop, refined, mask)
        except comfyui_client.GenerationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - mejor solo con los rasgos
            log.warning("Edicion: no se pudo repintar la cara: %s", exc)
            refined = crop
        found = photo_edit._faces(refined)
        if found:
            target = max(found, key=lambda f: f[2])
            refined = face_swap.swap_faces(source, refined, [fo], [target])
        current = photo_edit.paste_detail(current, refined, box, mask)
    return current


_POSE_REFERENCES: dict = {}  # foto -> referencia preparada (se repite mucho: de pie, playa, paracaidas...)
POSE_REFERENCE_CACHE = 8
REMOVE_HELD_PROMPT = ("Remove the object the person is holding in their hand, so that their hand is empty and "
                      "relaxed. Keep the person, their face, clothes, pose and the background exactly the same.")


def _pose_reference(current, request_text: str):
    """La foto que ve Kontext en un cambio de postura (Sergio, 2026-10-08):
    - sin lo que se tenga en la mano, salvo que se pida: Kontext copiaba la
      copa aunque la instruccion pidiera las manos vacias. Lo quita Kontext
      mismo (borrarlo a mano dejaba una mancha que Kontext copiaba);
    - con la cabeza recta (straighten_heads): copiaba la inclinacion del selfie.
    Se guarda por foto: sirve para cada postura que se pida con ella."""
    import hashlib
    keep_object = photo_edit.mentions_held_object(request_text)
    key = (hashlib.sha1(current.tobytes()).hexdigest(), keep_object)
    if key in _POSE_REFERENCES:
        return _POSE_REFERENCES[key]
    reference = current
    if not keep_object and photo_edit.detect_hands(current):
        _report("Preparando la foto para la nueva postura…")
        t = time.perf_counter()
        try:
            reference = _upscaled(current, image_agent.edit_with_kontext(
                REMOVE_HELD_PROMPT, photo_edit.prepare_for_kontext(current)))
            reference = photo_edit.finish(current, reference, "entera")
            log.info("Edicion: quitado lo que tenia en la mano %.0f s", time.perf_counter() - t)
        except comfyui_client.GenerationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - mejor con el objeto que sin edicion
            log.warning("Edicion: no se pudo quitar lo que tenia en la mano: %s", exc)
            reference = current
    reference = photo_edit.straighten_heads(reference)
    if len(_POSE_REFERENCES) >= POSE_REFERENCE_CACHE:
        _POSE_REFERENCES.pop(next(iter(_POSE_REFERENCES)))
    _POSE_REFERENCES[key] = reference
    return reference


def _upscaled(current, edited_png: bytes):
    """La salida de Kontext, ampliada con ESRGAN si se queda pequeña para la foto."""
    edited = photo_edit.load_rgb(edited_png)
    if photo_edit.needs_upscale(current, edited):
        edited = photo_edit.load_rgb(image_agent.upscale_bytes(edited_png))
    return edited


def _repaint_seam(current, seams: list):
    """La franja donde la cara original se junta con lo de Kontext, repintada
    por Kontext con poca intensidad: aun igualando tono y textura el corte del
    cuello se notaba ("parece un photoshop mal hecho", Sergio, 2026-10-07).
    Si falla, se queda la foto sin repintar."""
    crop = photo_edit.seam_crop(current, seams)
    if crop is None:
        return current
    _report("Afinando la unión de la cara…")
    t = time.perf_counter()
    box, crop_png, mask_png = crop
    try:
        refined = image_agent.refine_with_kontext(photo_edit.SEAM_PROMPT, crop_png, mask_png,
                                                  denoise=photo_edit.SEAM_DENOISE)
    except comfyui_client.GenerationCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 - mejor la foto sin repintar que ninguna
        log.warning("Edicion: no se pudo repintar la union: %s", exc)
        return current
    log.info("Edicion: union %.0f s", time.perf_counter() - t)
    return photo_edit.paste_seam(current, refined, box, seams)


def _image_from_photo(prompt: str, ref_bytes: bytes, model_id: str | None) -> tuple[str, str, str, str, object]:
    """Que hacer con una foto adjunta: (agent_used, texto de respuesta, contexto
    de error, extension, funcion que genera)."""
    if model_registry.get_edit_model() is not None:
        return ("image_edit", _PHOTO_EDITED,
                "editando la foto", ".jpg", lambda: _edit_photo(prompt, ref_bytes))
    if face_detect.has_face(ref_bytes):
        return ("image_faceid", "Imagen generada preservando la cara de referencia.",
                "generando la imagen con cara real", ".png",
                lambda: image_agent.generate_with_face(prompt, ref_bytes, model_id=model_id))
    return ("image_controlnet", "Imagen generada a partir de la composicion de la foto.",
            "generando la imagen a partir de la foto", ".png",
            lambda: image_agent.generate_with_controlnet(prompt, ref_bytes))


@app.post("/image_with_face", response_model=ChatResponse)
def image_with_face(request: Request, prompt: str = Form(...), image: UploadFile | None = File(None),
                     person_name: str | None = Form(None), model_id: str | None = Form(None),
                     session_id: str | None = Form(None)):
    """Imagen a partir de una foto adjunta. Con el modelo de edicion (FLUX
    Kontext) instalado, la foto se EDITA siguiendo la peticion y lo demas
    queda como en el original - antes se generaba una imagen nueva desde cero
    con la cara de una sola persona y no se parecia (Sergio, 2026-10-01: "no
    nos pareciamos en nada y no hacia lo que le pedia"). Sin el, como antes:
    - Con cara: preserva esa cara (IPAdapter FaceID). Ver aviso de uso
      responsable en verifier.py/manual.md: solo para fotos propias o con
      consentimiento.
    - Sin cara (mariposa, paisaje, objeto...): sigue la composicion/forma
      de la foto (ControlNet) - aplicar FaceID siempre generaba caras
      alucinadas (2026-09-25).
    model_id: checkpoint SDXL concreto (de model_registry.py), solo se usa
    en el camino de FaceID; None = el primero instalado."""
    start = time.perf_counter()
    session = request.state.session

    try:
        ref_bytes = _resolve_reference_bytes(image, person_name, session)
    except ValueError as exc:
        metrics.log_event("image_faceid", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(agent_used="image_faceid", response=str(exc), verifier_gated=False)

    name = _store_photo(session, ref_bytes)
    if name is not None:
        # con el editor: la foto queda en la conversacion (historial y "ahora
        # ponle...", "deshaz eso") igual que en el chat automatico
        chat = _own_session_id(session_id, session)
        memory.add_message(chat, "user", prompt, dek=session["dek"], key_generation=session["key_generation"],
                           user_id=session["user_id"], media=name)
        photo_session.start(session, chat, name, "subida")
        return _photo_edit_reply(prompt, name, chat, session)

    agent_used, response_text, error_context, ext, generate = _image_from_photo(prompt, ref_bytes, model_id)

    try:
        img_bytes = generate()
        if isinstance(img_bytes, tuple):  # edicion: (foto, que se ha hecho)
            img_bytes, response_text = img_bytes
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
        img_bytes, ext, session,
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
        described, (width, height) = prompt_writer.image_prompt(ollama, CONFIG["router"]["model"], prompt)
        img_bytes = image_agent.generate_with_lora(described, lora[0], lora[1], model_id=model_id,
                                                   width=width, height=height)
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
        img_bytes = _image_from_photo(prompt, ref_bytes, model_id)[4]()
        if isinstance(img_bytes, tuple):  # edicion: (foto, que se ha hecho)
            img_bytes = img_bytes[0]
    except Exception as exc:
        metrics.log_event("video_faceid", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(
            agent_used="video_faceid",
            response=_generation_error_message("generando la imagen base", exc),
            verifier_gated=False,
        )

    try:
        with media_store.plain_copy(img_bytes, ".png") as frame_path:
            motion = prompt_writer.video_prompt(ollama, CONFIG["router"]["model"], prompt, from_image=True)
            vid_bytes = video_agent.generate_from_image(motion, str(frame_path))
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
