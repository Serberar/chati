"""Rutas de usuarios y sesiones (/auth/*), sacadas de main.py (auditoria
2026-09-29). Ver ROADMAP.md, punto 0: cifrado de conocimiento cero, cada
sesion lleva la clave (DEK) del usuario solo en memoria.

Lo que hay que borrar al eliminar un usuario vive en otros modulos: main.py
lo pasa con configure()."""

import secrets
from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import access
import auth_sessions
import rate_limit
import users

router = APIRouter()

# los rellena main.py (configure)
_on_login: Callable[[dict], None] = lambda session: None
_delete_user_data: Callable[[str], None] = lambda user_id: None


def configure(on_login: Callable[[dict], None], delete_user_data: Callable[[str], None]) -> None:
    global _on_login, _delete_user_data
    _on_login, _delete_user_data = on_login, delete_user_data


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


class ProfileUpdateRequest(BaseModel):
    display_name: str


class SecurityQuestionRequest(BaseModel):
    password: str
    question: str
    answer: str


class PcAccessRequest(BaseModel):
    allowed: bool


def _with_session_cookie(body: dict, token: str) -> JSONResponse:
    resp = JSONResponse(body)
    resp.set_cookie(access.SESSION_COOKIE, token, max_age=auth_sessions.SESSION_TTL_SECONDS,
                    httponly=True, samesite="strict", path="/")
    return resp


def _too_many(seconds: int) -> JSONResponse:
    return JSONResponse({"detail": rate_limit.wait_message(seconds)}, status_code=429,
                        headers={"Retry-After": str(seconds)})


def _new_session(login: dict) -> str:
    return auth_sessions.create_session(login["id"], login["username"], login["role"],
                                        login["dek"], login["key_generation"])


def _registered(request: Request) -> dict | None:
    session = access.current_session(request)
    return session if session and session["role"] != "guest" else None


def _admin(request: Request) -> dict | None:
    session = access.current_session(request)
    return session if session and session["role"] == "admin" else None


@router.post("/auth/login")
def auth_login(req: LoginRequest):
    key = req.username.strip().lower()
    wait = rate_limit.login.retry_after(key)
    if wait:
        return _too_many(wait)
    try:
        login = users.login(req.username, req.password)
    except users.UserError as exc:
        rate_limit.login.fail(key)
        return JSONResponse({"detail": str(exc)}, status_code=401)
    rate_limit.login.succeed(key)
    token = _new_session(login)
    _on_login(login)
    return _with_session_cookie({"token": token, "username": login["username"], "role": login["role"]}, token)


@router.post("/auth/guest")
def auth_guest():
    token = auth_sessions.create_guest_session()
    return _with_session_cookie({"token": token, "username": None, "role": "guest"}, token)


@router.post("/auth/register")
def auth_register(req: RegisterRequest):
    wait = rate_limit.registration.retry_after("registro")
    if wait:
        return _too_many(wait)
    if not secrets.compare_digest(req.registration_key.encode(), users.get_or_create_registration_key().encode()):
        rate_limit.registration.fail("registro")
        return JSONResponse({"detail": "Clave de registro incorrecta."}, status_code=403)
    # el primer usuario de todo el sistema es admin automaticamente
    role = "admin" if not users.any_users_exist() else "user"
    try:
        users.create_user(req.username, req.password, role, req.security_question, req.security_answer)
    except users.UserError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    token = _new_session(users.login(req.username, req.password))
    return _with_session_cookie({"token": token, "username": req.username, "role": role}, token)


@router.post("/auth/logout")
def auth_logout(request: Request):
    token = access.session_token(request)
    if token:
        auth_sessions.destroy_session(token)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(access.SESSION_COOKIE, path="/")
    return resp


@router.get("/auth/me")
def auth_me(request: Request):
    session = access.current_session(request)
    if not session:
        return JSONResponse({"detail": "No hay sesion activa."}, status_code=401)
    user = users.get_user(session["username"]) if session["username"] else None
    return {
        "username": session["username"], "role": session["role"], "pc_access": access.can_use_pc(session),
        "display_name": user["display_name"] if user else None,
        "security_question": user["security_question"] if user else None,
    }


@router.post("/auth/profile")
def auth_update_profile(req: ProfileUpdateRequest, request: Request):
    session = _registered(request)
    if not session:
        return JSONResponse({"detail": "No hay sesion activa."}, status_code=401)
    try:
        users.set_display_name(session["username"], req.display_name)
    except users.UserError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"ok": True}


@router.post("/auth/security-question")
def auth_set_security_question(req: SecurityQuestionRequest, request: Request):
    session = _registered(request)
    if not session:
        return JSONResponse({"detail": "No hay sesion activa."}, status_code=401)
    try:
        users.set_security_question(session["username"], req.password, req.question, req.answer)
    except users.UserError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"ok": True}


@router.post("/auth/change-password")
def auth_change_password(req: ChangePasswordRequest, request: Request):
    session = _registered(request)
    if not session:
        return JSONResponse({"detail": "No hay sesion activa."}, status_code=401)
    try:
        users.change_password(session["username"], req.old_password, req.new_password)
    except users.UserError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    auth_sessions.destroy_all_sessions_for_user(session["username"])
    new_token = _new_session(users.login(session["username"], req.new_password))
    return _with_session_cookie({"token": new_token}, new_token)


@router.get("/auth/security-question/{username}")
def auth_security_question(username: str):
    user = users.get_user(username)
    if not user or not user["security_question"]:
        return JSONResponse({"detail": "No hay pregunta de seguridad configurada para ese usuario."}, status_code=404)
    return {"security_question": user["security_question"]}


@router.post("/auth/reset-password")
def auth_reset_password(req: ResetPasswordRequest):
    key = req.username.strip().lower()
    wait = rate_limit.password_reset.retry_after(key)
    if wait:
        return _too_many(wait)
    try:
        users.reset_via_security_question(req.username, req.security_answer, req.new_password)
    except users.UserError as exc:
        rate_limit.password_reset.fail(key)
        return JSONResponse({"detail": str(exc)}, status_code=400)
    rate_limit.password_reset.succeed(key)
    auth_sessions.destroy_all_sessions_for_user(req.username)
    return {"ok": True}


@router.get("/auth/users")
def auth_list_users(request: Request):
    if not _admin(request):
        return JSONResponse({"detail": "Solo un administrador puede ver la lista de usuarios."}, status_code=403)
    return {"users": users.list_users()}


@router.delete("/auth/users/{username}")
def auth_delete_user(username: str, request: Request):
    session = _admin(request)
    if not session:
        return JSONResponse({"detail": "Solo un administrador puede eliminar usuarios."}, status_code=403)
    if username == session["username"]:
        return JSONResponse({"detail": "No puedes eliminarte a ti mismo."}, status_code=400)
    target = users.get_user(username)
    deleted = users.delete_user(username)
    if deleted and target:
        # borrado en cascada de todos sus datos - no hace falta descifrar
        # nada para borrar, coherente con zero-knowledge (ver ROADMAP.md, punto 0)
        _delete_user_data(target["id"])
    auth_sessions.destroy_all_sessions_for_user(username)
    return {"ok": deleted}


@router.put("/auth/users/{username}/pc_access")
def auth_set_pc_access(username: str, req: PcAccessRequest, request: Request):
    if not _admin(request):
        return JSONResponse({"detail": "Solo un administrador puede cambiar esto."}, status_code=403)
    try:
        users.set_pc_access(username, req.allowed)
    except users.UserError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=404)
    return {"ok": True}


@router.get("/auth/security-events")
def auth_security_events(request: Request):
    if not _admin(request):
        return JSONResponse({"detail": "Solo un administrador puede ver esto."}, status_code=403)
    return {"events": users.list_security_events()}


@router.get("/auth/registration-key")
def auth_registration_key(request: Request):
    if not _admin(request):
        return JSONResponse({"detail": "Solo un administrador puede ver esto."}, status_code=403)
    return {"registration_key": users.get_or_create_registration_key()}
