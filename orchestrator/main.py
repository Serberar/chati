import json
import subprocess
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
from starlette.middleware.base import BaseHTTPMiddleware

import auth
import auth_sessions
import model_registry
import paths
import persona_trainer
import users

from agents.ollama_client import OllamaClient
from agents.llm_agent import LLMAgent
from agents.image_agent import ImageAgent
from agents.video_agent import VideoAgent
from agents.voice_agent import VoiceAgent
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
import tools
from rag import EMBED_MODEL, KnowledgeBase

with open("config.yaml", "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

# se genera aqui (no de forma perezosa en el propio endpoint) para que el
# archivo exista desde el primer arranque y el admin pueda consultarlo antes
# de que nadie intente registrarse - mismo patron que auth.API_KEY.
users.get_or_create_registration_key()

ollama = OllamaClient(CONFIG["ollama"]["base_url"])

text_agent = LLMAgent("text", CONFIG["agents"]["text"]["model"], ollama)
code_agent = LLMAgent("code", CONFIG["agents"]["code"]["model"], ollama)
vision_agent = LLMAgent("vision", CONFIG["agents"]["vision"]["model"], ollama)
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
# CONFIG["router"]["model"] (qwen2.5:7b) vive en VRAM, no en RAM del
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

OUTPUT_DIR = paths.OUTPUT_DIR
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="IA personal - orquestador")


class SessionAuthMiddleware(BaseHTTPMiddleware):
    """Sustituye a la clave API estatica compartida (ver ROADMAP.md, punto 0,
    fase 4): cada peticion necesita un token de sesion valido, obtenido via
    /auth/login, /auth/guest o /auth/register. La clave API vieja
    (auth.API_KEY) queda sin usar - no se borra, solo deja de comprobarse."""
    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS" or auth.is_public_path(request.url.path):
            return await call_next(request)
        token = request.headers.get("X-Session-Token") or request.query_params.get("session")
        session = auth_sessions.get_session(token) if token else None
        if session is None:
            return JSONResponse({"detail": "No autorizado. Inicia sesion."}, status_code=401)
        request.state.session = session
        return await call_next(request)


app.add_middleware(SessionAuthMiddleware)


# --- Sistema de usuarios (ver ROADMAP.md, punto 0) ---

class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str
    registration_key: str
    security_question: str | None = None
    security_answer: str | None = None


class ChangePasswordRequest(BaseModel):
    old_password: str
    new_password: str


class ResetPasswordRequest(BaseModel):
    username: str
    security_answer: str
    new_password: str


def _session_token(request: Request) -> str | None:
    return request.headers.get("X-Session-Token") or request.query_params.get("session")


def _current_session(request: Request) -> dict | None:
    token = _session_token(request)
    return auth_sessions.get_session(token) if token else None


def _guest_blocked() -> JSONResponse:
    """Modo invitado: acceso completo a chat/imagen/video, pero sin galeria
    de caras, sin documentos, sin CV - ver ROADMAP.md, punto 0."""
    return JSONResponse({"detail": "No disponible en modo invitado."}, status_code=403)


@app.post("/auth/login")
def auth_login(req: LoginRequest):
    try:
        session = users.login(req.username, req.password)
    except users.UserError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=401)
    token = auth_sessions.create_session(
        session["id"], session["username"], session["role"], session["dek"], session["key_generation"])
    return {"token": token, "username": session["username"], "role": session["role"]}


@app.post("/auth/guest")
def auth_guest():
    token = auth_sessions.create_guest_session()
    return {"token": token, "username": None, "role": "guest"}


@app.post("/auth/register")
def auth_register(req: RegisterRequest):
    if req.registration_key != users.get_or_create_registration_key():
        return JSONResponse({"detail": "Clave de registro incorrecta."}, status_code=403)
    # el primer usuario de todo el sistema es admin automaticamente
    role = "admin" if not users.any_users_exist() else "user"
    try:
        users.create_user(req.username, req.password, role, req.security_question, req.security_answer)
    except users.UserError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    session = users.login(req.username, req.password)
    token = auth_sessions.create_session(
        session["id"], session["username"], session["role"], session["dek"], session["key_generation"])
    return {"token": token, "username": req.username, "role": role}


@app.post("/auth/logout")
def auth_logout(request: Request):
    token = _session_token(request)
    if token:
        auth_sessions.destroy_session(token)
    return {"ok": True}


@app.get("/auth/me")
def auth_me(request: Request):
    session = _current_session(request)
    if not session:
        return JSONResponse({"detail": "No hay sesion activa."}, status_code=401)
    return {"username": session["username"], "role": session["role"]}


@app.post("/auth/change-password")
def auth_change_password(req: ChangePasswordRequest, request: Request):
    session = _current_session(request)
    if not session or session["role"] == "guest":
        return JSONResponse({"detail": "No hay sesion activa."}, status_code=401)
    try:
        users.change_password(session["username"], req.old_password, req.new_password)
    except users.UserError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    auth_sessions.destroy_all_sessions_for_user(session["username"])
    new_session = users.login(session["username"], req.new_password)
    new_token = auth_sessions.create_session(
        new_session["id"], new_session["username"], new_session["role"],
        new_session["dek"], new_session["key_generation"])
    return {"token": new_token}


@app.get("/auth/security-question/{username}")
def auth_security_question(username: str):
    user = users.get_user(username)
    if not user or not user["security_question"]:
        return JSONResponse({"detail": "No hay pregunta de seguridad configurada para ese usuario."}, status_code=404)
    return {"security_question": user["security_question"]}


@app.post("/auth/reset-password")
def auth_reset_password(req: ResetPasswordRequest):
    try:
        users.reset_via_security_question(req.username, req.security_answer, req.new_password)
    except users.UserError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    auth_sessions.destroy_all_sessions_for_user(req.username)
    return {"ok": True}


@app.get("/auth/users")
def auth_list_users(request: Request):
    session = _current_session(request)
    if not session or session["role"] != "admin":
        return JSONResponse({"detail": "Solo un administrador puede ver la lista de usuarios."}, status_code=403)
    return {"users": users.list_users()}


@app.delete("/auth/users/{username}")
def auth_delete_user(username: str, request: Request):
    session = _current_session(request)
    if not session or session["role"] != "admin":
        return JSONResponse({"detail": "Solo un administrador puede eliminar usuarios."}, status_code=403)
    if username == session["username"]:
        return JSONResponse({"detail": "No puedes eliminarte a ti mismo."}, status_code=400)
    target = users.get_user(username)
    deleted = users.delete_user(username)
    if deleted and target:
        # borrado en cascada de todos sus datos - no hace falta descifrar
        # nada para borrar, coherente con zero-knowledge (ver ROADMAP.md, punto 0)
        target_id = target["id"]
        memory.delete_user_messages(target_id)
        people.delete_all_for_user(target_id)
        profile_store.delete_all_for_user(target_id)
        knowledge_base.delete_all_for_user(target_id)
    auth_sessions.destroy_all_sessions_for_user(username)
    return {"ok": deleted}


@app.get("/auth/security-events")
def auth_security_events(request: Request):
    session = _current_session(request)
    if not session or session["role"] != "admin":
        return JSONResponse({"detail": "Solo un administrador puede ver esto."}, status_code=403)
    return {"events": users.list_security_events()}


@app.get("/auth/registration-key")
def auth_registration_key(request: Request):
    session = _current_session(request)
    if not session or session["role"] != "admin":
        return JSONResponse({"detail": "Solo un administrador puede ver esto."}, status_code=403)
    return {"registration_key": users.get_or_create_registration_key()}


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


def _with_file(resp: ChatResponse, path: Path) -> ChatResponse:
    resp.file_path = str(path)
    resp.file_url = f"/outputs/{path.name}"
    return resp


def _generation_error_message(action: str, exc: Exception) -> str:
    if isinstance(exc, GenerationCancelled):
        return "Generación cancelada."
    return f"Fallo {action}: {exc}"


@app.get("/health")
def health():
    return {"status": "ok"}


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


def _resolve_source_image(file_path: str | None, image: UploadFile | None) -> Path:
    """Reutiliza una imagen ya generada (por su ruta, evita re-subirla) o una
    subida nueva. Solo se permiten rutas dentro de OUTPUT_DIR (no cualquier
    ruta del sistema) para evitar servir/leer archivos arbitrarios."""
    if file_path:
        candidate = Path(file_path)
        if candidate.parent.resolve() != OUTPUT_DIR.resolve() or not candidate.exists():
            raise ValueError("Ruta de imagen no valida.")
        return candidate
    if image is not None and image.filename:
        upload_path = OUTPUT_DIR / f"src_{uuid.uuid4().hex}_{Path(image.filename).name}"
        upload_path.write_bytes(image.file.read())
        return upload_path
    raise ValueError("Hay que indicar una imagen (subida o ya generada).")


@app.post("/image/upscale", response_model=ChatResponse)
def image_upscale(file_path: str | None = Form(None), image: UploadFile | None = File(None)):
    """Escala x4 una imagen (ya generada, referenciada por su ruta, o una
    subida nueva) sin volver a generarla desde cero."""
    start = time.perf_counter()
    try:
        src = _resolve_source_image(file_path, image)
    except ValueError as exc:
        return ChatResponse(agent_used="image_upscale", response=str(exc), verifier_gated=False)

    try:
        img_bytes = image_agent.upscale(str(src))
    except Exception as exc:
        metrics.log_event("image_upscale", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(agent_used="image_upscale",
                             response=_generation_error_message("escalando la imagen", exc),
                             verifier_gated=False)

    out_path = OUTPUT_DIR / f"{uuid.uuid4().hex}.png"
    out_path.write_bytes(img_bytes)
    metrics.log_event("image_upscale", (time.perf_counter() - start) * 1000)
    return _with_file(
        ChatResponse(agent_used="image_upscale", response="Imagen escalada x4.", verifier_gated=False),
        out_path,
    )


@app.post("/image/inpaint", response_model=ChatResponse)
def image_inpaint(prompt: str = Form(...), mask: UploadFile = File(...),
                   file_path: str | None = Form(None), image: UploadFile | None = File(None),
                   denoise: float = Form(1.0)):
    """Repinta solo la zona marcada en blanco en la mascara (dibujada en la
    interfaz), dejando el resto de la imagen intacto."""
    start = time.perf_counter()
    try:
        src = _resolve_source_image(file_path, image)
    except ValueError as exc:
        return ChatResponse(agent_used="image_inpaint", response=str(exc), verifier_gated=False)

    mask_path = OUTPUT_DIR / f"mask_{uuid.uuid4().hex}.png"
    mask_path.write_bytes(mask.file.read())

    try:
        img_bytes = image_agent.inpaint(prompt, str(src), str(mask_path), denoise=denoise)
    except Exception as exc:
        metrics.log_event("image_inpaint", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(agent_used="image_inpaint",
                             response=_generation_error_message("repintando la imagen", exc),
                             verifier_gated=False)

    out_path = OUTPUT_DIR / f"{uuid.uuid4().hex}.png"
    out_path.write_bytes(img_bytes)
    metrics.log_event("image_inpaint", (time.perf_counter() - start) * 1000)
    return _with_file(
        ChatResponse(agent_used="image_inpaint", response="Zona seleccionada repintada.", verifier_gated=False),
        out_path,
    )


@app.get("/")
def index():
    return FileResponse(Path(__file__).parent / "static" / "index.html")


app.mount("/outputs", StaticFiles(directory=OUTPUT_DIR), name="outputs")
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
        people.save_person(name, image.file.read(), ext, user_id=session["user_id"],
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
    saved = profile_store.save_cv(file.filename or "cv.pdf", file.file.read(), user_id=session["user_id"],
                                   dek=session["dek"], key_generation=session["key_generation"])
    return {"ok": True, "filename": saved.name.removesuffix(".enc")}


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
        n_chunks = knowledge_base.add_document(filename, file.file.read(), user_id=session["user_id"],
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


def _execute_tool(name: str, arguments: dict, session_id: str, user_id: str | None = None,
                   dek: bytes | None = None, key_generation: int | None = None) -> tuple[str, list[dict] | None]:
    """Ejecuta una herramienta que el agente de texto ha pedido usar. Todas
    son consultas de solo lectura sobre datos que ya existen (memoria,
    documentos, personas guardadas, fecha real), salvo ejecutar_python, que
    corre en un proceso aparte con timeout y directorio temporal (ver
    tools.ejecutar_python) - pensado para verificar calculos, no para tocar
    archivos ni acceder a la red."""
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
        text = opencode_client.delegate(
            CONFIG["opencode"]["base_url"], CONFIG["opencode"]["web_url"], tarea,
        )
        return text, [{"source": "sistema:delegar_a_agente_de_codigo", "text": text}]

    return f"Herramienta desconocida: {name}", None


def _resolve_agent(message: str, agent_override: str | None) -> tuple[str, bool]:
    """Devuelve (agente, es_factual). Una sola llamada al modelo hace las dos
    cosas quando no hay override (ver router.py)."""
    if agent_override in (AGENTS.keys() | {"image", "video", "opencode"}):
        return agent_override, True  # no importa el valor real: solo se usa is_factual para 'text'
    route = router.classify(message)
    return route["agent"], route["factual"]


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
        out_path = OUTPUT_DIR / f"{uuid.uuid4().hex}.png"
        out_path.write_bytes(img_bytes)
        resp = _with_file(
            ChatResponse(agent_used="image", response="Imagen generada.", verifier_gated=False,
                         session_id=session_id),
            out_path,
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
        out_path = OUTPUT_DIR / f"{uuid.uuid4().hex}.mp4"
        out_path.write_bytes(vid_bytes)
        resp = _with_file(
            ChatResponse(agent_used="video", response="Video generado.", verifier_gated=False,
                         session_id=session_id),
            out_path,
        )
        _save_assistant_message(resp.response, "video")
        return resp

    if agent_name == "opencode":
        text = opencode_client.delegate(CONFIG["opencode"]["base_url"], CONFIG["opencode"]["web_url"], message)
        resp = ChatResponse(agent_used="opencode", response=text, verifier_gated=False, session_id=session_id)
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
        tool_executor = partial(_execute_tool, session_id=session_id, user_id=user_id,
                                 dek=dek, key_generation=key_generation)
        raw_answer, evidence = agent.respond_with_tools(
            message, history, tools.TOOL_DEFS, tool_executor, model=override_model, think=think)
        evidence = evidence or None  # lista vacia -> None, mas explicito para lo que sigue
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

    _save_assistant_message(resp.response, agent_name)
    if agent_name in ("text", "code") and not resp.verifier_gated:
        # memoria a largo plazo real: indexado para recuperar por relevancia mas adelante,
        # no solo los ultimos N mensajes. No se indexan respuestas bloqueadas (no hay nada que recordar).
        knowledge_base.add_conversation(session_id, message, resp.response,
                                         user_id=user_id, dek=dek, key_generation=key_generation)
    return resp


def _run_vision_chat(message: str, image_base64: str, session_id: str | None, auth_session: dict) -> ChatResponse:
    """Comenta una imagen subida - bypasea el router normal (ningun otro
    modelo sabe procesar imagenes), sin herramientas ni bucle. Ver
    ROADMAP.md, punto 5c. Sin model_profile: eso son los perfiles de TEXTO
    (rapido/bueno/seguridad) - aplicarlos aqui era un bug real (encontrado
    en vivo, 2026-09-25): si el perfil de chat activo era "rapido", vision
    intentaba usar qwen2.5:7b (sin soporte de imagenes) en vez del modelo
    de vision configurado, y Ollama lo rechazaba con un 400."""
    session_id = session_id or memory.new_session_id()
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
    session_id = session_id or memory.new_session_id()
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
    session_id = session_id or memory.new_session_id()
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

    session_id = session_id or memory.new_session_id()
    memory.add_message(session_id, "user", message, dek=dek, key_generation=key_generation, user_id=user_id)

    agent_name, is_factual = _resolve_agent(message, agent_override)
    yield json.dumps({"type": "start", "agent_used": agent_name, "session_id": session_id}) + "\n"
    stream_start = time.perf_counter()

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

    evidence: list[dict] = []
    full_text = ""
    try:
        if agent_name in ("text", "code"):
            _ensure_active_model(override_model or agent.model)
            tool_executor = partial(_execute_tool, session_id=session_id, user_id=user_id,
                                     dek=dek, key_generation=key_generation)
            chunks = agent.respond_with_tools_stream(
                message, history, tools.TOOL_DEFS, tool_executor, evidence, model=override_model, think=think)
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
    verifier_gated = False
    verifier_reason = None
    sources = None

    if CONFIG["verifier"]["enabled"] and verify and agent_name in ("text", "code"):
        result = verifier.check(message, full_text, is_factual=is_factual, evidence=evidence)
        final_response = result.answer
        verifier_gated = result.gated
        verifier_reason = result.reason
        sources = result.sources

    memory.add_message(session_id, "assistant", final_response, agent=agent_name,
                        dek=dek, key_generation=key_generation, user_id=user_id)
    if agent_name in ("text", "code") and not verifier_gated:
        knowledge_base.add_conversation(session_id, message, final_response,
                                         user_id=user_id, dek=dek, key_generation=key_generation)
    metrics.log_event(agent_name, (time.perf_counter() - stream_start) * 1000, verifier_gated)
    yield json.dumps({
        "type": "done",
        "response": final_response,
        "verifier_gated": verifier_gated,
        "verifier_reason": verifier_reason,
        "sources": sources,
        "session_id": session_id,
        "corrected": verifier_gated or (final_response != full_text),
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
    return {"personas": persona_trainer.list_personas()}


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

    tmp_dir = OUTPUT_DIR / f"_persona_upload_{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    saved_paths = []
    try:
        for i, photo in enumerate(photos):
            ext = Path(photo.filename or "").suffix or ".jpg"
            dest = tmp_dir / f"{i:03d}{ext}"
            dest.write_bytes(photo.file.read())
            saved_paths.append(dest)
        result = persona_trainer.start_training(name, saved_paths)
    except persona_trainer.NoBaseModelError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    except persona_trainer.TrainingAlreadyRunningError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=409)
    finally:
        for p in saved_paths:
            p.unlink(missing_ok=True)
        tmp_dir.rmdir()
    return result


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
    add(CONFIG["agents"]["vision"]["model"], "Vision (comentar fotos que subes)")
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

    out_path = OUTPUT_DIR / f"finetune_export_{uuid.uuid4().hex}.jsonl"
    count = finetune_export.export_user_conversations_chatml(
        user_id=session["user_id"], dek=session["dek"], key_generation=session["key_generation"],
        out_path=out_path,
    )
    return {"file_url": f"/outputs/{out_path.name}", "conversations_exported": count}


@app.get("/sessions")
def get_sessions(request: Request):
    session = request.state.session
    return memory.list_sessions(user_id=session["user_id"], dek=session["dek"], key_generation=session["key_generation"])


@app.get("/sessions/{session_id}")
def get_session_history(session_id: str, request: Request):
    session = request.state.session
    return memory.get_history(session_id, limit=200, dek=session["dek"], key_generation=session["key_generation"])


@app.put("/sessions/{session_id}/title")
def rename_session(session_id: str, request: Request, title: str = Form(...)):
    session = request.state.session
    title = title.strip()[:80]  # nombres cortos - es una etiqueta para reconocer el hilo, no un resumen
    if not title:
        return JSONResponse({"detail": "El nombre no puede estar vacio."}, status_code=400)
    memory.set_session_title(session_id, title, user_id=session["user_id"],
                              dek=session["dek"], key_generation=session["key_generation"])
    return {"ok": True, "title": title}


@app.delete("/sessions/{session_id}")
def delete_session(session_id: str):
    memory.clear_session(session_id)
    knowledge_base.delete_conversation(session_id)  # tambien borra su rastro de la memoria a largo plazo (RAG)
    return {"ok": True}


@app.put("/sessions/{session_id}/messages/{message_id}")
def edit_message(session_id: str, message_id: int, request: Request, content: str = Form(...)):
    session = request.state.session
    ok = memory.update_message(message_id, content, dek=session["dek"], key_generation=session["key_generation"])
    return {"ok": ok}


@app.delete("/sessions/{session_id}/messages/{message_id}")
def delete_message(session_id: str, message_id: int):
    ok = memory.delete_message(message_id)
    return {"ok": ok}


@app.post("/voice_chat")
def voice_chat(request: Request, audio: UploadFile = File(...), session_id: str | None = Form(None)):
    """Conversacion por voz: transcribe el audio, lo pasa por el pipeline de
    chat normal (router + verificador + memoria), y devuelve tanto el texto
    como un audio con la respuesta hablada."""
    audio_path = OUTPUT_DIR / f"voice_in_{uuid.uuid4().hex}.wav"
    audio_path.write_bytes(audio.file.read())

    transcript = voice_agent.transcribe(str(audio_path))
    if not transcript:
        return {"transcript": "", "agent_used": None, "response": "No se entendio ningun audio.",
                "verifier_gated": False, "file_url": None, "session_id": session_id}

    result = _run_chat(transcript, None, session_id, request.state.session)

    speech_path = OUTPUT_DIR / f"{uuid.uuid4().hex}.wav"
    voice_agent.speak(result.response, str(speech_path))

    return {
        "transcript": transcript,
        "agent_used": result.agent_used,
        "response": result.response,
        "verifier_gated": result.verifier_gated,
        "verifier_reason": result.verifier_reason,
        "file_url": f"/outputs/{speech_path.name}",
        "session_id": result.session_id,
    }


@app.post("/speak")
def speak(text: str = Form(...)):
    out_path = OUTPUT_DIR / f"{uuid.uuid4().hex}.wav"
    voice_agent.speak(text, str(out_path))
    return {"file_url": f"/outputs/{out_path.name}"}


def _resolve_reference_bytes(image: UploadFile | None, person_name: str | None, session: dict) -> bytes:
    """Usa la foto subida si la hay, si no busca la persona guardada por
    nombre (descifrando si hace falta con la dek de la sesion actual)."""
    if image is not None and image.filename:
        return image.file.read()
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
    """Genera una imagen nueva preservando la cara de una persona (foto subida
    o guardada por nombre, IPAdapter FaceID). Ver aviso de uso responsable en
    verifier.py/manual.md: solo para fotos propias o con consentimiento.
    model_id: checkpoint SDXL concreto (de model_registry.py), None = el primero instalado."""
    start = time.perf_counter()
    session = request.state.session

    try:
        ref_bytes = _resolve_reference_bytes(image, person_name, session)
    except ValueError as exc:
        metrics.log_event("image_faceid", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(agent_used="image_faceid", response=str(exc), verifier_gated=False)

    try:
        img_bytes = image_agent.generate_with_face(prompt, ref_bytes, model_id=model_id)
    except Exception as exc:
        metrics.log_event("image_faceid", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(
            agent_used="image_faceid",
            response=_generation_error_message("generando la imagen con cara real", exc),
            verifier_gated=False,
        )

    out_path = OUTPUT_DIR / f"{uuid.uuid4().hex}.png"
    out_path.write_bytes(img_bytes)
    metrics.log_event("image_faceid", (time.perf_counter() - start) * 1000)
    return _with_file(
        ChatResponse(agent_used="image_faceid", response="Imagen generada preservando la cara de referencia.",
                     verifier_gated=False),
        out_path,
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
            prompt, image.file.read(), control_type=control_type, strength=strength,
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

    out_path = OUTPUT_DIR / f"{uuid.uuid4().hex}.png"
    out_path.write_bytes(img_bytes)
    metrics.log_event("image_controlnet", (time.perf_counter() - start) * 1000)
    return _with_file(
        ChatResponse(agent_used="image_controlnet", response="Imagen generada siguiendo la composicion de la referencia.",
                     verifier_gated=False),
        out_path,
    )


@app.post("/video_with_face", response_model=ChatResponse)
def video_with_face(request: Request, prompt: str = Form(...), image: UploadFile | None = File(None),
                     person_name: str | None = Form(None), model_id: str | None = Form(None)):
    """Cadena completa: genera una escena nueva preservando la cara de una
    persona (foto subida o guardada por nombre) y despues la anima
    (image-to-video, LTXV). Solo para fotos propias o con consentimiento.
    model_id: checkpoint SDXL concreto para el fotograma base (de model_registry.py)."""
    start = time.perf_counter()
    session = request.state.session

    try:
        ref_bytes = _resolve_reference_bytes(image, person_name, session)
    except ValueError as exc:
        metrics.log_event("video_faceid", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(agent_used="video_faceid", response=str(exc), verifier_gated=False)

    try:
        img_bytes = image_agent.generate_with_face(prompt, ref_bytes, model_id=model_id)
    except Exception as exc:
        metrics.log_event("video_faceid", (time.perf_counter() - start) * 1000, error=str(exc))
        return ChatResponse(
            agent_used="video_faceid",
            response=_generation_error_message("generando la imagen base con cara real", exc),
            verifier_gated=False,
        )

    frame_path = OUTPUT_DIR / f"{uuid.uuid4().hex}.png"
    frame_path.write_bytes(img_bytes)

    try:
        vid_bytes = video_agent.generate_from_image(prompt, str(frame_path))
    except Exception as exc:
        metrics.log_event("video_faceid", (time.perf_counter() - start) * 1000, error=str(exc))
        return _with_file(
            ChatResponse(agent_used="video_faceid",
                         response=_generation_error_message("animando la imagen base ya generada", exc),
                         verifier_gated=False),
            frame_path,
        )

    out_path = OUTPUT_DIR / f"{uuid.uuid4().hex}.mp4"
    out_path.write_bytes(vid_bytes)
    metrics.log_event("video_faceid", (time.perf_counter() - start) * 1000)
    return _with_file(
        ChatResponse(agent_used="video_faceid", response="Video generado preservando la cara de referencia.",
                     verifier_gated=False),
        out_path,
    )
