"""Los ordenadores con Chati de Sergio (casa, trabajo...), vistos por
Tailscale: cual esta encendido y con Chati funcionando, para elegir en cual
trabajar al abrirla desde el movil (Sergio, pendiente.md 2026-10-08: "quiero
que pueda usar la chati del trabajo y la chati de casa").

No hay lista que mantener: son los otros equipos Windows de su red de
Tailscale. Que tengan Chati se comprueba pidiendo su /health (publica, solo
dice "ok"). El movil tiene que estar vinculado en cada uno (la vinculacion es
por ordenador)."""
import json
import subprocess
import threading
import time
from pathlib import Path

import requests

TAILSCALE = Path(r"C:\Program Files\Tailscale\tailscale.exe")
CACHE_SECONDS = 20

_cache: dict = {"at": 0.0, "items": []}
_lock = threading.Lock()


def _status() -> dict | None:
    if not TAILSCALE.exists():
        return None
    try:
        out = subprocess.run([str(TAILSCALE), "status", "--json"], capture_output=True, text=True, timeout=8,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(out.stdout) if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def _host(node: dict) -> str:
    return str(node.get("DNSName", "")).rstrip(".")


def _has_chati(host: str) -> bool:
    try:
        return requests.get(f"https://{host}/health", timeout=4).json().get("status") == "ok"
    except (requests.RequestException, ValueError, AttributeError):
        return False


def list_computers() -> list[dict]:
    """[{"name", "url", "this", "online", "chati"}]: este primero y luego los
    demas Windows de Tailscale. [] sin Tailscale. Se guarda 20 s (comprobar
    un ordenador apagado tarda unos segundos)."""
    with _lock:
        if time.time() - _cache["at"] < CACHE_SECONDS:
            return _cache["items"]
    status = _status()
    items = []
    if status:
        me = status.get("Self", {})
        host = _host(me)
        items.append({"name": host.split(".")[0], "url": f"https://{host}/", "this": True,
                      "online": True, "chati": True})
        for peer in (status.get("Peer") or {}).values():
            if str(peer.get("OS", "")).lower() != "windows":
                continue
            host = _host(peer)
            online = bool(peer.get("Online"))
            items.append({"name": host.split(".")[0], "url": f"https://{host}/", "this": False,
                          "online": online, "chati": online and _has_chati(host)})
    with _lock:
        _cache.update(at=time.time(), items=items)
    return items
