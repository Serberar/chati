"""Rutas de "Mis apps" (buscador de empleo, compras y busqueda profunda),
sacadas de main.py (auditoria 2026-09-29: main.py pasaba de 2.900 lineas).

Los invitados no llegan aqui: el middleware de main.py solo les deja su lista
de rutas. Lo que estas rutas necesitan del orquestador (el modelo, liberar
memoria, describir una foto) se lo da main.py con configure()."""

from typing import Callable

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

import deep_search
import job_search
import profile_store
import shopping

router = APIRouter()

# los rellena main.py (configure)
_chat: Callable[[str], str] | None = None
_before: Callable[[], None] | None = None
_describe_image: Callable[[str], str] | None = None


def configure(chat: Callable[[str], str], before: Callable[[], None], describe_image: Callable[[str], str]) -> None:
    global _chat, _before, _describe_image
    _chat, _before, _describe_image = chat, before, describe_image


def _keys(request: Request) -> tuple[str, bytes, int]:
    s = request.state.session
    return s["user_id"], s["dek"], s["key_generation"]


def _started(start: Callable[[], None]):
    try:
        start()
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)
    return {"ok": True}


# --- Buscador de empleo (roadmap n.º 7, ver job_search.py) ---

class JobPrefsRequest(BaseModel):
    prefs: dict


@router.get("/jobs")
def jobs_overview(request: Request):
    """Todo lo del buscador de empleo: preferencias, ultimos resultados y si
    hay una busqueda en marcha."""
    uid, dek, gen = _keys(request)
    results = job_search.get_results(uid, dek, gen)
    return {"prefs": job_search.get_prefs(uid, dek, gen),
            "results": {k: v for k, v in results.items() if k != "seen"},
            "status": job_search.status(uid),
            "has_cv": profile_store.cv_filename(user_id=uid) is not None}


@router.put("/jobs/prefs")
def jobs_save_prefs(request: Request, req: JobPrefsRequest):
    try:
        return job_search.save_prefs(req.prefs, *_keys(request))
    except ValueError as exc:
        return JSONResponse({"detail": str(exc)}, status_code=400)


@router.post("/jobs/search")
def jobs_start(request: Request):
    return _started(lambda: job_search.start(*_keys(request), _chat, before=_before))


@router.get("/jobs/status")
def jobs_status(request: Request):
    return job_search.status(_keys(request)[0])


@router.post("/jobs/cancel")
def jobs_cancel(request: Request):
    job_search.cancel(_keys(request)[0])
    return {"ok": True}


# --- Compras (ver shopping.py) ---

class ShoppingSearchRequest(BaseModel):
    descripcion: str = ""
    image_base64: str | None = None
    precio_min: float | str | None = None
    precio_max: float | str | None = None
    solo_espana: bool = True
    valoraciones: bool = True


@router.get("/shopping")
def shopping_overview(request: Request):
    """Busquedas anteriores (las ultimas 10) y si hay una en marcha."""
    uid, dek, gen = _keys(request)
    return {"history": shopping.get_history(uid, dek, gen), "status": shopping.status(uid)}


@router.post("/shopping/search")
def shopping_start(request: Request, req: ShoppingSearchRequest):
    return _started(lambda: shopping.start(
        *_keys(request), req.model_dump(exclude={"image_base64"}), _chat,
        describe_image=_describe_image, image_b64=req.image_base64, before=_before))


@router.get("/shopping/status")
def shopping_status(request: Request):
    return shopping.status(_keys(request)[0])


@router.post("/shopping/cancel")
def shopping_cancel(request: Request):
    shopping.cancel(_keys(request)[0])
    return {"ok": True}


# --- Busqueda profunda (ver deep_search.py) ---

class DeepSearchRequest(BaseModel):
    consulta: str
    afinar_de: str | None = None  # id de una busqueda anterior: "consulta" se añade a ella


@router.get("/deep")
def deep_overview(request: Request):
    """Busquedas anteriores (las ultimas 10) y si hay una en marcha."""
    uid, dek, gen = _keys(request)
    return {"history": deep_search.get_history(uid, dek, gen), "status": deep_search.status(uid)}


@router.post("/deep/search")
def deep_start(request: Request, req: DeepSearchRequest):
    return _started(lambda: deep_search.start(*_keys(request), req.consulta, _chat,
                                              afinar_de=req.afinar_de, before=_before))


@router.get("/deep/status")
def deep_status(request: Request):
    return deep_search.status(_keys(request)[0])


@router.post("/deep/cancel")
def deep_cancel(request: Request):
    deep_search.cancel(_keys(request)[0])
    return {"ok": True}
