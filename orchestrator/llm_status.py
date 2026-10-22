"""Que esta haciendo ahora mismo el modelo del agente. Todo lo que OpenCode
le pide al modelo pasa por la pasarela /llm/v1 (main.py), que lo apunta aqui:
cuando empieza una peticion, cada trozo que escribe el modelo y cuando acaba.

Sirve para enseñar en la tarea algo mas util que "Trabajando..." y para saber
si de verdad esta atascada. El 2026-10-16 una tarea estuvo horas "en marcha":
el modelo tardaba mas de 5 min en leer la peticion, OpenCode cortaba y
reintentaba desde cero, y nada lo decia.

Ollama no cuenta cuanto le falta mientras lee la peticion (antes de escribir
la primera palabra): ahi solo se sabe el tamaño, cuanto lleva y si esta
trabajando: gastando procesador (ollama_cpu) o tarjeta grafica (gpu_percent;
qwen3:8b lee entero en la GPU con el procesador casi parado, medido)."""

import itertools
import json
import subprocess
import threading
import time

import psutil
import requests

OLLAMA_URL = "http://127.0.0.1:11434"  # main.py pone el de config.yaml

_lock = threading.Lock()
_ids = itertools.count(1)
_active: dict[int, dict] = {}
# peticiones seguidas que acabaron sin que el modelo escribiera nada: OpenCode
# corta y reintenta desde cero, y cada intento vuelve a leerlo todo
_unanswered = {"count": 0}
# ultima vez que el modelo escribio algo, y que Ollama estuvo gastando procesador
_last = {"output": time.time(), "busy": time.time()}

CHARS_PER_TOKEN = 3.5  # texto y codigo mezclados; solo para dar una idea del tamaño
BUSY_CPU_PERCENT = 50  # medio nucleo: leyendo o escribiendo en CPU, Ollama gasta varios
BUSY_GPU_PERCENT = 20


def start(body: bytes, model: str | None) -> int:
    rid = next(_ids)
    with _lock:
        _active[rid] = {"model": model, "started": time.time(), "input_tokens": int(len(body) / CHARS_PER_TOKEN),
                        "first_output": None, "tokens": 0, "tool": None, "target": None}
    return rid


def output(rid: int, line: bytes) -> None:
    """Una linea del stream de respuesta (un trozo es ~1 token)."""
    if not line.startswith(b"data: {"):
        return
    now = time.time()
    tool = target = None
    if b'"tool_calls"' in line:
        # Ollama manda cada llamada a herramienta entera en un trozo (ver llm_proxy.py)
        try:
            for choice in json.loads(line[6:]).get("choices", []):
                for call in (choice.get("delta") or {}).get("tool_calls") or []:
                    fn = call.get("function") or {}
                    tool = fn.get("name") or tool
                    args = json.loads(fn.get("arguments") or "{}")
                    target = args.get("filePath") or args.get("path") or args.get("command") or target
        except (ValueError, AttributeError):
            pass
    with _lock:
        r = _active.get(rid)
        if not r:
            return
        r["tokens"] += 1
        r["first_output"] = r["first_output"] or now
        if tool:
            r["tool"], r["target"] = tool, target if isinstance(target, str) else None
        _last["output"] = now
        _unanswered["count"] = 0


def finish(rid: int, wrote: bool = False) -> None:
    with _lock:
        r = _active.pop(rid, None)
        if r is None:
            return
        if wrote or r["first_output"]:
            _last["output"] = time.time()
            _unanswered["count"] = 0
        else:
            _unanswered["count"] += 1


_procs: dict[int, psutil.Process] = {}


def ollama_cpu() -> float:
    """% de procesador que gasta Ollama desde la anterior consulta (100 = un
    nucleo entero). Los modelos cargados son procesos ollama.exe aparte."""
    total, seen = 0.0, set()
    for p in psutil.process_iter(["name"]):
        if not (p.info["name"] or "").lower().startswith("ollama"):
            continue
        seen.add(p.pid)
        proc = _procs.setdefault(p.pid, p)  # el mismo objeto: mide desde la vez anterior
        try:
            total += proc.cpu_percent(None)
        except psutil.Error:
            pass
    for pid in set(_procs) - seen:
        _procs.pop(pid, None)
    if total >= BUSY_CPU_PERCENT:
        _last["busy"] = time.time()
    return total


_cached: dict[str, tuple[float, object]] = {}


def _every(seconds: float, key: str, fetch):
    at, value = _cached.get(key, (0.0, None))
    if time.time() - at > seconds:
        value = fetch()
        _cached[key] = (time.time(), value)
    return value


def gpu_percent() -> int:
    """% de uso de la tarjeta grafica NVIDIA (0 si no hay). nvidia-smi tarda
    ~70 ms: como mucho una vez cada 2 s. Cuenta tambien a ComfyUI."""
    def fetch():
        try:
            out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"],
                                 capture_output=True, check=False, text=True, timeout=5,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return max(int(x) for x in out.stdout.split())
        except (OSError, ValueError, subprocess.SubprocessError):
            return 0
    value = _every(2, "gpu", fetch)
    if value >= BUSY_GPU_PERCENT:
        _last["busy"] = time.time()
    return value


def _loaded_models() -> set[str]:
    def fetch():
        try:
            return {m["name"] for m in requests.get(f"{OLLAMA_URL}/api/ps", timeout=2).json().get("models", [])}
        except (requests.RequestException, ValueError, KeyError, TypeError):
            return None  # no se sabe: mejor no decir "cargando"
    return _every(2, "ps", fetch)


def retries() -> int:
    """Peticiones seguidas cortadas sin respuesta (ver _unanswered)."""
    return _unanswered["count"]


def last_sign_of_life() -> float:
    """Ultima vez que el modelo escribio algo u Ollama estuvo trabajando."""
    ollama_cpu()
    gpu_percent()
    return max(_last["output"], _last["busy"])


def _clock(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 60} min {seconds % 60:02d} s" if seconds >= 60 else f"{seconds} s"


def snapshot() -> dict | None:
    """Lo que hace el modelo ahora, en una frase. None si no esta haciendo nada."""
    cpu = ollama_cpu()
    with _lock:
        if not _active:
            return None
        r = dict(max(_active.values(), key=lambda x: x["started"]))
        retries = _unanswered["count"]
    gpu = gpu_percent()
    now = time.time()
    model = r["model"] or "el modelo"
    if r["first_output"] is None:
        loaded = _loaded_models()
        phase = "leyendo"
        text = f"{model} está leyendo la petición (~{r['input_tokens']:,} tokens)".replace(",", ".")
        if loaded is not None and r["model"] and r["model"] not in loaded:
            phase, text = "cargando", f"Cargando {model} en memoria"
        since = r["started"]
        if cpu < BUSY_CPU_PERCENT and gpu < BUSY_GPU_PERCENT:
            text += " · Ollama no está usando ni el procesador ni la tarjeta gráfica"
    elif r["tool"]:
        phase = "herramienta"
        text = f"Preparando {r['tool']}" + (f": {r['target']}" if r["target"] else "")
        since = r["first_output"]
    else:
        phase = "escribiendo"
        rate = r["tokens"] / max(now - r["first_output"], 1)
        text = f"{model} está escribiendo: {r['tokens']} tokens a {rate:.1f} tokens/s"
        since = r["first_output"]
    if retries:
        text += f" · intento {retries + 1}: los anteriores se cortaron sin respuesta"
    return {"fase": phase, "texto": text, "desde": since, "en_esta_fase": _clock(now - since),
            "reintentos": retries, "cpu": round(cpu), "gpu": gpu}
