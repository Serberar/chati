"""Herramientas extra del agente de codigo (OpenCode), para que trabaje lo mas
parecido posible a un asistente de programacion de verdad (Sergio,
2026-10-13: "que lea repos, que almacene memoria, que revise actualizaciones,
que pueda revisar si existen librerias"). Las ofrece agent_mcp.py por MCP;
aqui solo esta la logica, para poder probarla sin OpenCode.

- Memoria persistente: lo que aprende del usuario y de cada proyecto, en
  ficheros (data/agente_memoria). El indice (MEMORY.md) lo lee OpenCode al
  empezar cada conversacion (ver "instructions" en opencode.json).
- Internet: buscar (DuckDuckGo en HTML, sin navegador) y leer paginas. Nunca
  direcciones de la red local (web_tools.is_private_host).
- Paquetes: si existe una libreria en PyPI o npm, su ultima version y para que
  sirve (para no inventarse librerias), y que dependencias de un proyecto
  tienen version nueva.
- Repositorios: descargar uno de GitHub (u otro git) para leerlo."""

import json
import os
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from html import unescape
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import requests

from paths import DATA_DIR

MEMORY_DIR = DATA_DIR / "agente_memoria"
MEMORY_INDEX = MEMORY_DIR / "MEMORY.md"
REPOS_DIR = DATA_DIR / "agente_repos"
MEMORY_TYPES = ("usuario", "proyecto", "preferencia", "referencia")
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) ChatiIA-agente/1.0"}
TIMEOUT = 15
PAGE_CHARS = 12000


# ---------- memoria ----------

def _slug(text: str) -> str:
    text = text.lower()
    for a, b in zip("áéíóúüñ", "aeiouun"):
        text = text.replace(a, b)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")[:60] or "nota"


def memory_index() -> str:
    """El indice de la memoria, tal cual (lo que OpenCode carga al empezar)."""
    return MEMORY_INDEX.read_text(encoding="utf-8") if MEMORY_INDEX.exists() else ""


def _rewrite_index() -> None:
    lines = ["# Memoria del agente de codigo de Chati",
             "Una linea por recuerdo. Para leer uno entero: herramienta chati_memoria_leer con su nombre.", ""]
    for f in sorted(MEMORY_DIR.glob("*.md")):
        if f.name == MEMORY_INDEX.name:
            continue
        meta = _frontmatter(f.read_text(encoding="utf-8"))
        project = f" [{meta['proyecto']}]" if meta.get("proyecto") else ""
        lines.append(f"- {f.stem} ({meta.get('tipo', '?')}){project}: {meta.get('resumen', '')}")
    MEMORY_INDEX.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _frontmatter(text: str) -> dict:
    m = re.match(r"---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        return {}
    return {k.strip(): v.strip() for k, v in (line.split(":", 1) for line in m.group(1).splitlines() if ":" in line)}


def memory_save(title: str, content: str, kind: str = "proyecto", project: str = "", summary: str = "") -> str:
    """Guarda (o actualiza, si ya hay uno con ese titulo) un recuerdo."""
    if kind not in MEMORY_TYPES:
        return f"Tipo no valido: {kind}. Usa uno de: {', '.join(MEMORY_TYPES)}."
    if not content.strip():
        return "No hay nada que guardar."
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    name = _slug(title)
    path = MEMORY_DIR / f"{name}.md"
    existed = path.exists()
    summary = (summary or content.strip().splitlines()[0])[:150].replace("\n", " ")
    path.write_text(f"---\ntitulo: {title}\ntipo: {kind}\nproyecto: {project}\nresumen: {summary}\n"
                    f"fecha: {date.today().isoformat()}\n---\n{content.strip()}\n", encoding="utf-8")
    _rewrite_index()
    return f"{'Actualizado' if existed else 'Guardado'} el recuerdo '{name}'."


def memory_read(name: str) -> str:
    path = MEMORY_DIR / f"{_slug(name)}.md"
    if not path.exists():
        return f"No hay ningun recuerdo llamado '{name}'. Indice:\n{memory_index() or '(vacio)'}"
    return path.read_text(encoding="utf-8")


def memory_search(text: str) -> str:
    words = [w for w in _slug(text).split("-") if len(w) > 2]
    hits = []
    for f in sorted(MEMORY_DIR.glob("*.md")) if MEMORY_DIR.exists() else []:
        if f.name == MEMORY_INDEX.name:
            continue
        body = _slug(f.read_text(encoding="utf-8"))
        score = sum(body.count(w) for w in words)
        if score:
            hits.append((score, f))
    if not hits:
        return "Nada en la memoria sobre eso."
    return "\n\n".join(f.read_text(encoding="utf-8") for _, f in sorted(hits, reverse=True)[:5])


def memory_delete(name: str) -> str:
    path = MEMORY_DIR / f"{_slug(name)}.md"
    if not path.exists():
        return f"No hay ningun recuerdo llamado '{name}'."
    path.unlink()
    _rewrite_index()
    return f"Borrado el recuerdo '{path.stem}'."


# ---------- internet ----------

def _public(url: str) -> bool:
    import web_tools
    parsed = urlparse(url)
    return parsed.scheme in ("http", "https") and not web_tools.is_private_host(parsed.hostname)


def web_search(query: str, n: int = 8) -> str:
    """Resultados de busqueda (titulo, direccion y resumen): Bing con el
    navegador de Chati (web_tools.Browser). Sin navegador, DuckDuckGo y Bing
    contestaban con paginas anti-robots (probado el 2026-10-13)."""
    import web_tools
    try:
        with web_tools.Browser() as browser:
            page = browser._ctx.new_page()
            try:
                page.goto(web_tools.BING.format(q=quote_plus(query)), timeout=30000, wait_until="domcontentloaded")
                page.wait_for_timeout(1500)
                found = page.eval_on_selector_all(
                    "li.b_algo", "els => els.map(e => [(e.querySelector('h2 a') || {}).innerText || '', "
                                 "(e.querySelector('h2 a') || {}).href || '', "
                                 "(e.querySelector('.b_caption p, p') || {}).innerText || ''])")
            finally:
                page.close()
    except Exception as exc:  # noqa: BLE001 - sin navegador o sin red: se dice
        return f"No se pudo buscar: {exc}"
    results = [f"- {title.strip()}\n  {web_tools.unbing(href)}\n  {snippet.strip()[:300]}"
               for title, href, snippet in found if href.startswith("http")][:n]
    return "\n".join(results) if results else "Sin resultados."


def _text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg|head)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h[1-6]|tr|pre)>", "\n", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def web_read(url: str, start: int = 0) -> str:
    """El texto de una pagina (sin HTML), por trozos de PAGE_CHARS."""
    if not _public(url):
        return "Esa direccion no esta permitida (solo paginas publicas de internet)."
    try:
        resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        return f"No se pudo leer {url}: {exc}"
    kind = resp.headers.get("content-type", "")
    text = resp.text if ("text/plain" in kind or "json" in kind or url.endswith((".md", ".txt"))) else _text(resp.text)
    if len(text) < 800 and "html" in kind:
        # pagina que se pinta con JavaScript: con el navegador de Chati
        try:
            import web_tools
            with web_tools.Browser() as browser:
                text = browser.fetch_page(url)["text"]
        except Exception:  # noqa: BLE001, S110 - se queda lo que hubiera
            pass
    part = text[start:start + PAGE_CHARS]
    more = f"\n\n[... sigue: vuelve a pedirla con start={start + PAGE_CHARS}]" if len(text) > start + PAGE_CHARS else ""
    return part + more


# ---------- paquetes ----------

def package_info(name: str, ecosystem: str = "python") -> str:
    """Si existe una libreria y lo basico de ella. ecosystem: python | npm."""
    eco = ecosystem.lower()
    try:
        if eco in ("python", "pypi", "pip"):
            resp = requests.get(f"https://pypi.org/pypi/{quote_plus(name)}/json", headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 404:
                return f"NO EXISTE ningun paquete llamado '{name}' en PyPI."
            info = resp.json()["info"]
            releases = resp.json().get("releases", {}).get(info["version"], [])
            when = releases[0]["upload_time"][:10] if releases else "?"
            return (f"{info['name']} {info['version']} (publicada {when}) - PyPI\n{info.get('summary') or ''}\n"
                    f"Requiere Python: {info.get('requires_python') or '?'} · Licencia: {info.get('license') or '?'}\n"
                    f"Web: {info.get('home_page') or (info.get('project_urls') or {}).get('Homepage', '?')}\n"
                    f"Instalar: pip install {info['name']}")
        if eco in ("npm", "node", "javascript", "js"):
            resp = requests.get(f"https://registry.npmjs.org/{name.replace('/', '%2F')}", headers=HEADERS,
                                timeout=TIMEOUT)
            if resp.status_code == 404:
                return f"NO EXISTE ningun paquete llamado '{name}' en npm."
            data = resp.json()
            latest = data.get("dist-tags", {}).get("latest", "?")
            when = (data.get("time") or {}).get(latest, "?")[:10]
            return (f"{name} {latest} (publicada {when}) - npm\n{data.get('description') or ''}\n"
                    f"Licencia: {data.get('license') or '?'} · Web: {data.get('homepage') or '?'}\n"
                    f"Instalar: npm install {name}")
    except (requests.RequestException, ValueError, KeyError) as exc:
        return f"No se pudo consultar '{name}': {exc}"
    return f"Ecosistema no soportado: {ecosystem} (usa python o npm)."


def _latest(name: str, eco: str) -> str | None:
    try:
        if eco == "python":
            resp = requests.get(f"https://pypi.org/pypi/{quote_plus(name)}/json", headers=HEADERS, timeout=TIMEOUT)
            return resp.json()["info"]["version"] if resp.ok else None
        resp = requests.get(f"https://registry.npmjs.org/{name.replace('/', '%2F')}/latest", headers=HEADERS,
                            timeout=TIMEOUT)
        return resp.json().get("version") if resp.ok else None
    except (requests.RequestException, ValueError, KeyError):
        return None


def _project_dependencies(folder: Path) -> list[tuple[str, str, str]]:
    """(ecosistema, nombre, version pedida) de requirements*.txt, pyproject.toml y package.json."""
    deps = []
    for req in sorted(folder.glob("requirements*.txt")):
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#")[0].strip()
            m = re.match(r"^([A-Za-z0-9_.\-]+)(\[.*?\])?\s*([=<>!~]=?\s*[^;\s]+)?", line)
            if m and not line.startswith("-"):
                deps.append(("python", m.group(1), (m.group(3) or "").replace(" ", "")))
    pyproject = folder / "pyproject.toml"
    if pyproject.exists():
        block = re.search(r"dependencies\s*=\s*\[(.*?)\]", pyproject.read_text(encoding="utf-8"), re.DOTALL)
        for item in re.findall(r'"([^"]+)"', block.group(1)) if block else []:
            m = re.match(r"^([A-Za-z0-9_.\-]+)(\[.*?\])?\s*([=<>!~]=?\s*[^;\s]+)?", item)
            if m:
                deps.append(("python", m.group(1), (m.group(3) or "").replace(" ", "")))
    package = folder / "package.json"
    if package.exists():
        data = json.loads(package.read_text(encoding="utf-8"))
        for section in ("dependencies", "devDependencies"):
            deps += [("npm", n, v) for n, v in (data.get(section) or {}).items()]
    seen, unique = set(), []
    for d in deps:
        if (d[0], d[1].lower()) not in seen:
            seen.add((d[0], d[1].lower()))
            unique.append(d)
    return unique


def _pinned(spec: str) -> str:
    m = re.search(r"(\d+(?:\.\d+)*[A-Za-z0-9.\-]*)", spec)
    return m.group(1) if m else ""


def check_updates(folder: str) -> str:
    """Las dependencias del proyecto frente a la ultima version publicada."""
    root = Path(folder)
    if not root.is_dir():
        return f"No existe la carpeta {folder}."
    deps = _project_dependencies(root)
    if not deps:
        return "No encuentro dependencias (requirements*.txt, pyproject.toml ni package.json) en esa carpeta."
    with ThreadPoolExecutor(max_workers=8) as pool:
        latest = list(pool.map(lambda d: _latest(d[1], d[0]), deps))
    outdated, ok, missing = [], [], []
    for (eco, name, spec), new in zip(deps, latest):
        have = _pinned(spec)
        if new is None:
            missing.append(f"- {name} ({eco}): no se encuentra en el registro (¿nombre mal escrito o privado?)")
        elif have and _version_key(have) < _version_key(new):
            outdated.append(f"- {name} ({eco}): pedida {spec or 'sin version'} -> ultima {new}")
        else:
            ok.append(f"- {name} ({eco}): {spec or 'sin version fija'} (ultima {new})")
    parts = [f"{len(deps)} dependencias revisadas en {root}."]
    if outdated:
        parts.append("CON VERSION NUEVA:\n" + "\n".join(outdated))
    if missing:
        parts.append("NO ENCONTRADAS:\n" + "\n".join(missing))
    parts.append("Al dia o sin version fija:\n" + "\n".join(ok) if ok else "")
    parts.append("Ojo: una version mayor nueva puede romper cosas; revisa sus notas de cambios antes de actualizar.")
    return "\n\n".join(p for p in parts if p)


def _version_key(v: str) -> tuple:
    try:
        from packaging.version import Version
        return (0, Version(v))
    except Exception:  # noqa: BLE001 - versiones de npm raras: comparacion por numeros
        return (1, tuple(int(x) for x in re.findall(r"\d+", v)[:4]))


# ---------- repositorios ----------

def read_repo(url: str) -> str:
    """Descarga (o actualiza) un repositorio git publico y devuelve donde esta,
    su arbol de carpetas y el principio del README, para leerlo despues con
    las herramientas normales (read, grep, glob)."""
    parsed = urlparse(url if "://" in url else f"https://github.com/{url}")
    if parsed.scheme != "https" or not _public(f"https://{parsed.hostname}/"):
        return "Solo repositorios publicos por https (por ejemplo https://github.com/usuario/proyecto)."
    parts = [p for p in parsed.path.strip("/").removesuffix(".git").split("/") if p]
    if len(parts) < 2:
        return "Dame la direccion del repositorio: https://github.com/usuario/proyecto"
    target = REPOS_DIR / f"{parts[0]}__{parts[1]}"
    REPOS_DIR.mkdir(parents=True, exist_ok=True)
    clone_url = f"https://{parsed.hostname}/{parts[0]}/{parts[1]}.git"
    try:
        if (target / ".git").exists():
            subprocess.run(["git", "-C", str(target), "pull", "--ff-only", "--depth", "1"], capture_output=True, check=False,
                           text=True, timeout=180)
        else:
            out = subprocess.run(["git", "clone", "--depth", "1", clone_url, str(target)], capture_output=True, check=False,
                                 text=True, timeout=600)
            if out.returncode != 0:
                return f"No se pudo descargar {clone_url}: {out.stderr.strip()[-400:]}"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"No se pudo descargar {clone_url}: {exc}"
    tree = []
    for path in sorted(target.rglob("*")):
        rel = path.relative_to(target)
        if any(p.startswith(".") or p in ("node_modules", "__pycache__", "dist", "build") for p in rel.parts):
            continue
        if len(rel.parts) <= 2:
            tree.append(("  " * (len(rel.parts) - 1)) + rel.name + ("/" if path.is_dir() else ""))
    readme = next((p for p in target.iterdir() if p.name.lower().startswith("readme")), None)
    intro = readme.read_text(encoding="utf-8", errors="replace")[:3000] if readme else "(sin README)"
    return (f"Repositorio en: {target}\nLeelo con read/grep/glob en esa carpeta.\n\nCarpetas (2 niveles):\n"
            + "\n".join(tree[:200]) + f"\n\nREADME:\n{intro}")


# ---------- buscar en el codigo por significado ----------

INDEX_DIR = DATA_DIR / "agente_indices"
EMBED_MODEL = "nomic-embed-text"
OLLAMA_URL = "http://127.0.0.1:11434"
CODE_EXTENSIONS = {".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".cs", ".go", ".rs", ".rb", ".php", ".c", ".h",
                   ".cpp", ".hpp", ".kt", ".swift", ".sql", ".html", ".css", ".scss", ".vue", ".svelte", ".md",
                   ".yaml", ".yml", ".toml", ".json", ".ps1", ".sh"}
SKIP_DIRS = {"venv", ".venv", "env", "node_modules", ".git", "__pycache__", "dist", "build", ".next", "target",
             ".pytest_cache", ".mypy_cache", "coverage"}
CHUNK_LINES, CHUNK_OVERLAP = 60, 10
MAX_FILE_BYTES = 300_000
MAX_CHUNKS = 5000


def _embed(texts: list[str]) -> list[list[float]]:
    # en el procesador: la tarjeta grafica es del modelo del agente
    resp = requests.post(f"{OLLAMA_URL}/api/embed", json={"model": EMBED_MODEL, "input": texts,
                                                          "options": {"num_gpu": 0}}, timeout=600)
    resp.raise_for_status()
    return resp.json()["embeddings"]


def _chunks(path: Path) -> list[tuple[int, int, str]]:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out, start = [], 0
    while start < len(lines):
        end = min(len(lines), start + CHUNK_LINES)
        text = "\n".join(lines[start:end]).strip()
        if text:
            out.append((start + 1, end, text))
        if end == len(lines):
            break
        start = end - CHUNK_OVERLAP
    return out


def _index_path(folder: Path) -> Path:
    import hashlib
    return INDEX_DIR / (hashlib.sha1(str(folder.resolve()).lower().encode()).hexdigest()[:16] + ".json")


def _update_index(folder: Path) -> list[dict]:
    """Indice del proyecto (trozos con su vector), recalculando solo los
    archivos que han cambiado desde la ultima vez."""
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    cache_file = _index_path(folder)
    cache = json.loads(cache_file.read_text(encoding="utf-8")) if cache_file.exists() else {}
    files = {}
    for path in folder.rglob("*"):
        if (path.is_file() and path.suffix.lower() in CODE_EXTENSIONS and not SKIP_DIRS.intersection(path.parts)
                and path.stat().st_size <= MAX_FILE_BYTES):
            files[path.relative_to(folder).as_posix()] = path
    fresh, pending = {}, []
    for rel, path in files.items():
        mtime = path.stat().st_mtime_ns
        old = cache.get(rel)
        if old and old["mtime"] == mtime:
            fresh[rel] = old
        else:
            fresh[rel] = {"mtime": mtime, "chunks": []}
            pending += [(rel, a, b, text) for a, b, text in _chunks(path)]
    pending = pending[:MAX_CHUNKS]
    for i in range(0, len(pending), 32):
        batch = pending[i:i + 32]
        vectors = _embed([f"search_document: {rel}\n{text[:2000]}" for rel, _, _, text in batch])
        for (rel, a, b, text), vec in zip(batch, vectors):
            fresh[rel]["chunks"].append({"start": a, "end": b, "vec": [round(v, 5) for v in vec]})
    cache_file.write_text(json.dumps(fresh), encoding="utf-8")
    return [{"file": rel, **chunk} for rel, entry in fresh.items() for chunk in entry["chunks"]]


def code_search(folder: str, query: str, n: int = 6) -> str:
    """Los trozos de codigo del proyecto mas relacionados con lo que se busca
    (por significado, no por palabras exactas: "donde se valida el login")."""
    import numpy as np
    root = Path(folder)
    if not root.is_dir():
        return f"No existe la carpeta {folder}."
    try:
        chunks = _update_index(root)
        if not chunks:
            return "No hay archivos de codigo en esa carpeta."
        q = np.array(_embed([f"search_query: {query}"])[0])
    except (requests.RequestException, KeyError, ValueError) as exc:
        return f"No se pudo buscar (¿esta Ollama con {EMBED_MODEL}?): {exc}"
    vecs = np.array([c["vec"] for c in chunks])
    scores = vecs @ q / (np.linalg.norm(vecs, axis=1) * np.linalg.norm(q) + 1e-9)
    out = []
    for i in np.argsort(-scores)[:n]:
        c = chunks[i]
        lines = (root / c["file"]).read_text(encoding="utf-8", errors="replace").splitlines()[c["start"] - 1:c["end"]]
        preview = "\n".join(lines[:15])
        out.append(f"== {c['file']} lineas {c['start']}-{c['end']} (parecido {scores[i]:.2f})\n{preview}")
    return "\n\n".join(out) + "\n\nPara ver un trozo entero, lee el archivo en esas lineas."


# ---------- ver: la web en desarrollo y capturas ----------

VISION_MODEL = "qwen2.5vl:7b-cpu"  # en el procesador: la tarjeta grafica es del modelo del agente
SHOTS_DIR = DATA_DIR / "agente_capturas"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "::1", "0.0.0.0"}


def _describe_image(png: bytes, question: str) -> str:
    import base64
    prompt = ("Eres los ojos de un programador. Describe en español, concreto y ordenado, lo que se ve en esta "
              "imagen de una aplicación: estructura de la pantalla, textos importantes, mensajes de error visibles y "
              f"cualquier cosa que parezca rota, descolocada o vacía. {question}".strip())
    try:
        resp = requests.post(f"{OLLAMA_URL}/api/chat", json={
            "model": VISION_MODEL, "stream": False, "keep_alive": "2m",
            "messages": [{"role": "user", "content": prompt, "images": [base64.b64encode(png).decode()]}]},
            timeout=900)
        resp.raise_for_status()
        return resp.json()["message"]["content"].strip()
    except (requests.RequestException, KeyError, ValueError) as exc:
        return f"(No se pudo describir la imagen con {VISION_MODEL}: {exc})"


def look_at_local_page(url: str, question: str = "") -> str:
    """Abre la web que se esta desarrollando (solo en este ordenador), hace una
    captura, junta los errores de la consola de JavaScript y la describe."""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    if parsed.hostname not in _LOCAL_HOSTS:
        return ("Solo se pueden mirar webs de este ordenador (http://localhost:PUERTO). Para paginas de internet, "
                "usa chati_leer_web.")
    from playwright.sync_api import sync_playwright
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(channel="msedge", headless=True)
            except Exception:  # noqa: BLE001 - sin Edge: el Chromium de Playwright
                browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.on("console", lambda m: errors.append(f"consola {m.type}: {m.text}")
                        if m.type in ("error", "warning") else None)
                page.on("pageerror", lambda e: errors.append(f"error de JavaScript: {e}"))
                page.on("requestfailed", lambda r: errors.append(f"no cargo {r.url}: {r.failure}"))
                resp = page.goto(parsed.geturl(), timeout=30000, wait_until="networkidle")
                page.wait_for_timeout(1000)
                status = resp.status if resp else "?"
                title = page.title()
                png = page.screenshot(full_page=True)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - servidor apagado, puerto mal...
        return f"No se pudo abrir {parsed.geturl()}: {exc}. ¿Esta arrancado el servidor de desarrollo?"
    shot = SHOTS_DIR / f"captura_{date.today().isoformat()}_{abs(hash(url)) % 10000}.png"
    shot.write_bytes(png)
    description = _describe_image(png, question)
    found = "\n".join(errors[:30]) if errors else "ninguno"
    return (f"Pagina {parsed.geturl()} (respuesta {status}, titulo «{title}»). Captura guardada en {shot}.\n\n"
            f"Errores de la consola y de carga:\n{found}\n\nLo que se ve:\n{description}")


def look_at_image(path: str, question: str = "") -> str:
    """Describe una captura o imagen que ha dejado el usuario (o una del proyecto)."""
    file = Path(path)
    if not file.is_file():
        return f"No existe la imagen {path}."
    if file.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
        return "Solo imagenes (png, jpg, webp, bmp, gif)."
    data = file.read_bytes()
    if file.suffix.lower() not in (".png", ".jpg", ".jpeg"):
        import io

        from PIL import Image
        buf = io.BytesIO()
        Image.open(io.BytesIO(data)).convert("RGB").save(buf, format="PNG")
        data = buf.getvalue()
    return _describe_image(data, question)


# ---------- programas en marcha ----------
# El bash de OpenCode espera a que el comando termine: un servidor o una app
# que se queda abierta lo dejaba colgado. Estas arrancan el programa aparte,
# guardan lo que escribe y dejan leerlo despues (Sergio, 2026-10-13: "arrancar
# tu programa y mirar sus errores"). Viven lo que vive el servidor MCP.
PROGRAMS_DIR = DATA_DIR / "agente_programas"
MAX_PROGRAMS = 3
_programs: dict[str, dict] = {}


def _program_status(prog: dict) -> str:
    code = prog["proc"].poll()
    return "en marcha" if code is None else f"terminado (codigo {code})"


def _tail(path: Path, lines: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])[-PAGE_CHARS:]


def start_program(folder: str, command: str, wait: int = 8) -> str:
    """Arranca un programa que se queda abierto (servidor, app) en la carpeta
    del proyecto y devuelve lo que ha escrito en los primeros segundos."""
    import time

    import code_checks
    path = Path(folder)
    if not path.is_dir():
        return f"No existe la carpeta {folder}."
    if code_checks._DANGEROUS.search(command):
        return "Ese comando no se arranca desde aqui (borra, sube o toca el sistema). Pideselo al usuario."
    running = [k for k, p in _programs.items() if p["proc"].poll() is None]
    if len(running) >= MAX_PROGRAMS:
        return f"Ya hay {MAX_PROGRAMS} programas en marcha ({', '.join(running)}): para alguno con parar_programa."
    PROGRAMS_DIR.mkdir(parents=True, exist_ok=True)
    prog_id = f"p{len(_programs) + 1}"
    log = PROGRAMS_DIR / f"{prog_id}_{int(time.time())}.log"
    out = open(log, "w", encoding="utf-8", errors="replace")  # noqa: SIM115 - lo escribe el programa
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        proc = subprocess.Popen(command, shell=True, cwd=path, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, creationflags=flags,
                                env={**__import__("os").environ, "PYTHONUNBUFFERED": "1"})
    except OSError as exc:
        out.close()
        return f"No se pudo arrancar: {exc}"
    _programs[prog_id] = {"proc": proc, "log": log, "command": command, "out": out}
    time.sleep(max(1, min(wait, 60)))
    return (f"Programa {prog_id} ({command}): {_program_status(_programs[prog_id])}.\n"
            f"Lo que ha escrito hasta ahora:\n{_tail(log, 60) or '(nada todavia)'}\n\n"
            f"Leelo despues con ver_salida('{prog_id}') y paralo con parar_programa('{prog_id}') al terminar.")


def program_output(prog_id: str, lines: int = 80) -> str:
    prog = _programs.get(prog_id)
    if not prog:
        return f"No hay ningun programa {prog_id}. En marcha: {', '.join(_programs) or 'ninguno'}."
    return f"{prog_id} ({prog['command']}): {_program_status(prog)}.\n{_tail(prog['log'], lines) or '(sin salida)'}"


def stop_program(prog_id: str) -> str:
    prog = _programs.get(prog_id)
    if not prog:
        return f"No hay ningun programa {prog_id}."
    if prog["proc"].poll() is None:
        # con /T tambien sus hijos (npm run dev arranca node, python arranca workers)
        subprocess.run(["taskkill", "/PID", str(prog["proc"].pid), "/T", "/F"], capture_output=True, check=False)
        try:
            prog["proc"].wait(timeout=10)
        except subprocess.TimeoutExpired:
            prog["proc"].kill()
    prog["out"].close()
    return f"Parado {prog_id}. Ultimas lineas:\n{_tail(prog['log'], 20)}"


def stop_all_programs() -> None:
    for prog_id in list(_programs):
        stop_program(prog_id)


def _window_titles(pids: set[int]) -> list[str]:
    """Titulos de las ventanas visibles de esos procesos (Windows)."""
    import ctypes
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    titles = []

    def collect(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value in pids and user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(256)
            user32.GetWindowTextW(hwnd, buf, 256)
            if buf.value:
                titles.append(buf.value)
        return True
    user32.EnumWindows(ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(collect), 0)
    return titles


def try_program(path: str, seconds: int = 6) -> str:
    """Abre un programa como si el usuario le diera doble clic y cuenta que
    pasa: se cierra con un error, abre una ventana o se queda sin ventana. Lo
    cierra al acabar. El agente lo lanzaba con bash y se quedaba bloqueado
    esperando a que alguien cerrara la ventana (banco del 2026-10-22)."""
    import time

    import psutil
    target = Path(path)
    if not target.is_file():
        return f"No existe el archivo {path}."
    ext = target.suffix.lower()
    if ext in (".py", ".pyw"):
        cmd = [shutil.which("python") or sys.executable, str(target)]
    elif ext == ".exe":
        cmd = [str(target)]
    else:
        return "Solo se pueden probar programas .py, .pyw o .exe."
    PROGRAMS_DIR.mkdir(parents=True, exist_ok=True)
    log = PROGRAMS_DIR / f"prueba_{int(time.time())}.log"
    with open(log, "w", encoding="utf-8", errors="replace") as out:
        try:
            proc = subprocess.Popen(cmd, cwd=target.parent, stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, env={**os.environ, "PYTHONUNBUFFERED": "1"},
                                    creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        except OSError as exc:
            if getattr(exc, "winerror", None) == 740:
                return ("Pide permisos de administrador al abrirse (UAC), asi que no se puede probar solo. Prueba su "
                        ".py con probar_programa: si abre la ventana, el .exe tambien lo hara.")
            return f"No se pudo abrir: {exc}"
        titles: list[str] = []
        end = time.time() + max(2, min(seconds, 30))
        while time.time() < end and proc.poll() is None:
            time.sleep(0.5)
        if proc.poll() is None:
            try:
                pids = {proc.pid} | {c.pid for c in psutil.Process(proc.pid).children(recursive=True)}
            except psutil.Error:
                pids = {proc.pid}
            titles = _window_titles(pids)
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
            proc.wait(timeout=10)
            code = None
        else:
            code = proc.returncode
    said = _tail(log, 30) or "(no ha escrito nada)"
    # un .exe de PyInstaller que falla abre su propia ventana de aviso: eso no es "funciona"
    crashed = "Traceback (most recent call last)" in said or any(
        w in title.lower() for title in titles for w in ("unhandled exception", "fatal error", "error"))
    gui = ext != ".exe" and bool(_GUI_IMPORT.search(target.read_text(encoding="utf-8", errors="replace")))
    if code == 0 and not crashed and not gui:
        # un programa de consola que hace lo suyo y termina: eso es funcionar (antes salia MAL y el
        # agente lo convertia en una ventana y le hacia un .exe sin que nadie lo pidiera)
        verdict = "BIEN: ha terminado sin errores (codigo 0). Comprueba en lo que ha escrito que el resultado es correcto."
    elif code is not None or crashed:
        verdict = "MAL: falla con un error." if crashed else f"MAL: se ha cerrado solo (codigo {code})."
        if "EOF when reading a line" in said:
            verdict += (" Espera que se escriba algo por teclado (input) y al abrirlo no hay donde: si el usuario"
                        " quiere una ventana, hazle una interfaz con tkinter.")
    elif titles:
        verdict = f"BIEN: se abre una ventana ('{titles[0]}') y sigue abierta."
    else:
        verdict = ("MAL: sigue en marcha pero no abre ninguna ventana (si es de consola, esta esperando algo por"
                   " teclado; si debia tener ventana, no la crea).")
    return f"{verdict}\nLo que ha escrito:\n{said}\n(Ya lo he cerrado.)"


_GUI_IMPORT = re.compile(r"^\s*(import|from)\s+(tkinter|customtkinter|PyQt\d|PySide\d|wx|pygame)\b", re.MULTILINE)


def build_exe(script: str, admin: bool = False) -> str:
    """Crea el .exe de un .py con PyInstaller, lo pone en la carpeta del script
    (sustituyendo el que hubiera) y lo prueba. El agente compilaba en su
    carpeta de trabajo y dejaba el .exe nuevo en dist/ sin moverlo: el que
    abria el usuario seguia siendo el roto (banco del 2026-10-22)."""
    import tempfile
    source = Path(script)
    if not source.is_file() or source.suffix.lower() not in (".py", ".pyw"):
        return f"No existe el programa Python {script}."
    window = bool(_GUI_IMPORT.search(source.read_text(encoding="utf-8", errors="replace")))
    olds = [p for p in source.parent.glob("*.exe")]
    target = olds[0] if len(olds) == 1 else source.with_suffix(".exe")
    python = shutil.which("python") or sys.executable
    PROGRAMS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=PROGRAMS_DIR) as tmp:
        cmd = [python, "-m", "PyInstaller", "--noconfirm", "--onefile", "--log-level", "WARN",
               "--name", target.stem, "--distpath", f"{tmp}/dist", "--workpath", f"{tmp}/build", "--specpath", tmp]
        cmd += ["--windowed"] if window else []
        cmd += ["--uac-admin"] if admin else []
        try:
            out = subprocess.run(cmd + [str(source)], cwd=source.parent, capture_output=True, text=True,
                                 timeout=900, check=False, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            return "PyInstaller no ha terminado en 15 minutos."
        built = Path(tmp) / "dist" / f"{target.stem}.exe"
        if out.returncode != 0 or not built.exists():
            tail = "\n".join((out.stdout + out.stderr).splitlines()[-25:])
            if "No module named PyInstaller" in tail:
                tail += "\n(Falta PyInstaller: instalalo con: python -m pip install pyinstaller)"
            return f"MAL: PyInstaller ha fallado:\n{tail}"
        try:
            os.replace(built, target)
        except PermissionError:
            return (f"MAL: {target.name} esta abierto y no se puede sustituir. Cierralo (Stop-Process -Name "
                    f"\"{target.stem}\") y vuelve a crear el .exe.")
    kind = "con ventana (--windowed)" if window else "de consola"
    made = f"Creado {target} ({kind}{', pide administrador al abrirse' if admin else ''})."
    if admin:
        return f"{made}\nComo pide administrador no se puede abrir solo: prueba {source.name} con probar_programa."
    return f"{made}\nPrueba del .exe nuevo: {try_program(str(target))}"


# ---------- renombrar ----------
# qwen renombraba a base de comandos de bash archivo por archivo: 25-40 pasos
# y ~15 minutos para cambiar un nombre en 3 archivos (banco 2026-10-13).
_RENAME_EXT = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".html", ".css", ".scss", ".md",
               ".json", ".toml", ".yaml", ".yml", ".txt", ".cfg", ".ini", ".sql", ".sh", ".ps1", ".java", ".cs",
               ".go", ".rs", ".php", ".rb", ".kt", ".swift", ".c", ".h", ".cpp", ".hpp"}
_RENAME_SKIP = {"venv", ".venv", "env", "node_modules", ".git", "__pycache__", "dist", "build", ".pytest_cache"}
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def rename_symbol(folder: str, old: str, new: str, apply: bool = False) -> str:
    """Cambia un nombre (funcion, clase, variable) por otro en todos los
    archivos de texto del proyecto, solo como palabra completa. Sin aplicar,
    dice donde sale; con apply=True lo cambia y dice que archivos ha tocado."""
    root = Path(folder)
    if not root.is_dir():
        return f"No existe la carpeta {folder}."
    if not _IDENT.fullmatch(old) or not _IDENT.fullmatch(new):
        return "Solo nombres simples (letras, numeros y _, sin espacios ni puntos)."
    if old == new:
        return "El nombre nuevo es igual que el viejo."
    word = re.compile(rf"\b{re.escape(old)}\b")
    hits, changed = [], []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if not path.is_file() or path.suffix.lower() not in _RENAME_EXT or _RENAME_SKIP.intersection(rel.parts):
            continue
        try:
            text = path.read_bytes().decode("utf-8")  # tal cual: read_text cambiaba los saltos de Windows
        except (UnicodeDecodeError, OSError):
            continue
        if not word.search(text):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if word.search(line):
                hits.append(f"{rel.as_posix()}:{n}: {line.strip()[:120]}")
        if apply:
            with open(path, "w", encoding="utf-8", newline="") as f:  # sin cambiar los saltos de linea
                f.write(word.sub(new, text))
            changed.append(rel.as_posix())
    if not hits:
        return f"'{old}' no aparece en ningun archivo de texto del proyecto."
    shown = "\n".join(hits[:80]) + (f"\n... y {len(hits) - 80} mas" if len(hits) > 80 else "")
    if apply:
        return (f"Cambiado '{old}' por '{new}' en {len(hits)} sitios de {len(changed)} archivos: "
                f"{', '.join(changed)}.\n{shown}\nAhora pasa los tests.")
    return (f"'{old}' aparece en {len(hits)} sitios (todavia NO se ha cambiado nada). Revisa que todos sean el "
            f"mismo nombre (no otra cosa que se llame igual) y cambialo con renombrar:\n{shown}")


def find_symbol(folder: str, name: str) -> str:
    """Donde aparece un nombre en el proyecto, sin cambiar nada."""
    return rename_symbol(folder, name, name + "_")


# ---------- navegar el codigo ----------
# "Donde esta definido X" y "donde se usa X", como en un editor de codigo. En
# Python con jedi (entiende el codigo: no confunde dos cosas que se llamen
# igual ni cuenta comentarios); en el resto, por patrones.
_DEF_PATTERNS = (
    r"^\s*(?:async\s+)?def\s+{n}\b", r"^\s*class\s+{n}\b", r"^{n}\s*(?::[^=]+)?=[^=]",
    r"\bfunction\s*\*?\s*{n}\s*\(", r"\b(?:const|let|var)\s+{n}\s*=", r"^\s*(?:export\s+)?(?:default\s+)?class\s+{n}\b",
    r"^\s*(?:public|private|protected|static|async|\s)*{n}\s*\([^)]*\)\s*\{{", r"\b(?:func|fn|struct|interface|type|enum)\s+{n}\b",
)


def _code_files(root: Path):
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if path.is_file() and path.suffix.lower() in _RENAME_EXT and not _RENAME_SKIP.intersection(rel.parts):
            yield path, rel.as_posix()


def _regex_definitions(root: Path, name: str) -> list[tuple[str, int, str]]:
    pattern = re.compile("|".join(p.format(n=re.escape(name)) for p in _DEF_PATTERNS))
    found = []
    for path, rel in _code_files(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        found += [(rel, n, line.strip()) for n, line in enumerate(lines, 1) if pattern.search(line)]
    return found


def _jedi_definitions(root: Path, name: str) -> list:
    import jedi
    project = jedi.Project(str(root), smart_sys_path=False)
    defs = []
    for path, rel in _code_files(root):
        if path.suffix != ".py":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not re.search(rf"\b(def|class)\s+{re.escape(name)}\b|^{re.escape(name)}\s*=", text, re.MULTILINE):
            continue
        script = jedi.Script(text, path=str(path), project=project)
        defs += [d for d in script.get_names(all_scopes=True, definitions=True) if d.name == name
                 and d.type in ("function", "class", "statement", "instance")]
    return defs


def find_definition(folder: str, name: str) -> str:
    """Donde esta definido un nombre (funcion, clase, variable) y como es."""
    root = Path(folder)
    if not root.is_dir():
        return f"No existe la carpeta {folder}."
    if not _IDENT.fullmatch(name):
        return "Dame solo el nombre (sin parentesis ni puntos), por ejemplo precio_final."
    lines = []
    try:
        for d in _jedi_definitions(root, name)[:20]:
            rel = Path(d.module_path).relative_to(root).as_posix()
            sig = "; ".join(s.to_string() for s in d.get_signatures()) or d.description
            doc = (d.docstring(raw=True) or "").strip().splitlines()
            lines.append(f"- {rel}:{d.line} ({d.type}) {sig}" + (f" — {doc[0][:150]}" if doc else ""))
    except Exception:  # noqa: BLE001 - jedi no pudo: los patrones de abajo
        lines = []
    if not lines:
        lines = [f"- {rel}:{n}: {text[:160]}" for rel, n, text in _regex_definitions(root, name)[:20]]
    if not lines:
        return f"No encuentro donde se define '{name}' en el proyecto. ¿Viene de una libreria? Prueba chati_usos."
    return f"Definicion de '{name}':\n" + "\n".join(lines)


def find_usages(folder: str, name: str) -> str:
    """Donde se usa un nombre en el proyecto (sin contar donde se define).
    Por palabra completa: jedi se saltaba los usos desde otros archivos
    (from x import nombre, modulo.nombre), y es justo lo que mas interesa."""
    root = Path(folder)
    if not root.is_dir():
        return f"No existe la carpeta {folder}."
    if not _IDENT.fullmatch(name):
        return "Dame solo el nombre (sin parentesis ni puntos), por ejemplo precio_final."
    word = re.compile(rf"\b{re.escape(name)}\b")
    defs_at = {(rel, n) for rel, n, _ in _regex_definitions(root, name)}
    hits: list[str] = []
    for path, rel in _code_files(root):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        hits += [f"{rel}:{n}: {line.strip()[:140]}" for n, line in enumerate(lines, 1)
                 if word.search(line) and (rel, n) not in defs_at]
    if not hits:
        return f"'{name}' no se usa en ningun sitio del proyecto (aparte de donde se define)."
    files = len({h.split(":", 1)[0] for h in hits})
    shown = "\n".join(hits[:80]) + (f"\n... y {len(hits) - 80} mas" if len(hits) > 80 else "")
    return (f"'{name}' aparece en {len(hits)} sitios de {files} archivos (imports incluidos; si otra cosa se llama "
            f"igual, tambien sale):\n{shown}")


# ---------- usar una web en desarrollo ----------
WEB_STEP_LIMIT = 25


def _locate(page, target: str):
    """El elemento que el agente describe: por su texto, su etiqueta, su
    texto de ayuda o, si no, como selector CSS."""
    for find in (lambda: page.get_by_role("button", name=target), lambda: page.get_by_role("link", name=target),
                 lambda: page.get_by_label(target), lambda: page.get_by_placeholder(target),
                 lambda: page.get_by_text(target), lambda: page.locator(target)):
        try:
            loc = find()
            if loc.count():
                return loc.first
        except Exception:  # noqa: BLE001, S112 - selector no valido: el siguiente
            continue
    return None


def _visible_controls(page) -> str:
    js = """() => Array.from(document.querySelectorAll('a,button,input,select,textarea,[role=button]'))
      .filter(e => e.offsetParent !== null).slice(0, 40).map(e => {
        const label = (e.labels && e.labels[0] && e.labels[0].innerText) || e.getAttribute('aria-label')
          || e.placeholder || e.innerText || e.value || e.name || e.id || '';
        return `${e.tagName.toLowerCase()}${e.type ? '[' + e.type + ']' : ''}: ${label.trim().slice(0, 60)}`;
      })"""
    try:
        return "\n".join(page.evaluate(js)) or "(ninguno)"
    except Exception:  # noqa: BLE001
        return "(no se pudieron leer)"


def use_local_page(url: str, steps: list | None = None, question: str = "") -> str:
    """Abre la web en desarrollo (solo de este ordenador) y hace los pasos que
    se le pidan: clic, escribir, elegir, tecla, esperar, ir. Devuelve que paso
    en cada uno, los errores de la consola y como queda la pagina."""
    parsed = urlparse(url if "://" in url else f"http://{url}")
    if parsed.hostname not in _LOCAL_HOSTS:
        return "Solo webs de este ordenador (http://localhost:PUERTO)."
    steps = list(steps or [])[:WEB_STEP_LIMIT]
    from playwright.sync_api import sync_playwright
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    log_lines: list[str] = []
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(channel="msedge", headless=True)
            except Exception:  # noqa: BLE001 - sin Edge: el Chromium de Playwright
                browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 800})
                page.on("console", lambda m: errors.append(f"consola {m.type}: {m.text}")
                        if m.type in ("error", "warning") else None)
                page.on("pageerror", lambda e: errors.append(f"error de JavaScript: {e}"))
                page.goto(parsed.geturl(), timeout=30000, wait_until="networkidle")
                for i, step in enumerate(steps, 1):
                    action = str(step.get("accion", "")).lower()
                    target = str(step.get("en", ""))
                    try:
                        if action == "ir":
                            dest = urlparse(str(step.get("url", "")))
                            if dest.hostname and dest.hostname not in _LOCAL_HOSTS:
                                raise ValueError("solo webs de este ordenador")
                            page.goto(step["url"], timeout=30000, wait_until="networkidle")
                        elif action == "esperar":
                            page.wait_for_timeout(min(int(step.get("ms", 1000)), 15000))
                        elif action == "tecla":
                            page.keyboard.press(str(step.get("tecla", "Enter")))
                        else:
                            el = _locate(page, target)
                            if el is None:
                                raise ValueError(f"no encuentro «{target}» en la pagina")
                            if action == "clic":
                                el.click(timeout=10000)
                            elif action == "escribir":
                                el.fill(str(step.get("texto", "")), timeout=10000)
                            elif action == "elegir":
                                el.select_option(label=str(step.get("opcion", "")), timeout=10000)
                            else:
                                raise ValueError(f"accion desconocida «{action}» (clic, escribir, elegir, tecla, "
                                                 "esperar, ir)")
                        page.wait_for_load_state("networkidle", timeout=10000)
                        log_lines.append(f"{i}. {action} {target or step.get('url', '')}: bien")
                    except Exception as exc:  # noqa: BLE001 - se cuenta y se para
                        log_lines.append(f"{i}. {action} {target}: FALLO - {str(exc).splitlines()[0][:200]}")
                        break
                title, final_url = page.title(), page.url
                text = page.inner_text("body")[:3000]
                controls = _visible_controls(page)
                png = page.screenshot(full_page=True)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 - servidor apagado, puerto mal...
        return f"No se pudo abrir {parsed.geturl()}: {exc}. ¿Esta arrancado el servidor de desarrollo?"
    shot = SHOTS_DIR / f"uso_{date.today().isoformat()}_{abs(hash((url, str(steps)))) % 10000}.png"
    shot.write_bytes(png)
    parts = [f"Pagina final: {final_url} («{title}»). Captura en {shot}.",
             "Pasos:\n" + ("\n".join(log_lines) or "(ninguno: solo se ha abierto)"),
             "Errores de la consola:\n" + ("\n".join(errors[:30]) or "ninguno"),
             f"Botones y campos visibles:\n{controls}", f"Texto de la pagina:\n{text}"]
    if question:
        parts.append("Lo que se ve:\n" + _describe_image(png, question))
    return "\n\n".join(parts)


# ---------- GitHub (solo leer) ----------
GITHUB_API = "https://api.github.com"


def _github_token() -> str | None:
    """El acceso a GitHub que ya usa git en este ordenador (para los repos
    privados del usuario). Sin ventanas: si no hay, se sigue sin el."""
    try:
        out = subprocess.run(["git", "credential", "fill"], input="protocol=https\nhost=github.com\n\n",
                             capture_output=True, check=False, text=True, timeout=15,
                             env={**__import__("os").environ, "GCM_INTERACTIVE": "never", "GIT_TERMINAL_PROMPT": "0"})
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"^password=(.+)$", out.stdout, re.MULTILINE)
    return m.group(1).strip() if out.returncode == 0 and m else None


def github_read(repo: str, what: str = "issues", number: int | None = None) -> str:
    """Lee de un repositorio de GitHub: issues, prs, issue N, pr N (con sus
    cambios), commits, acciones o readme. Solo leer: no cambia nada."""
    repo = repo.strip().removeprefix("https://github.com/").removesuffix(".git").strip("/")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return "Dime el repositorio como usuario/proyecto (por ejemplo Serberar/chati)."
    headers = {**HEADERS, "Accept": "application/vnd.github+json"}
    token = _github_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    what = what.lower().strip()
    paths = {"issues": f"/repos/{repo}/issues?state=open&per_page=30", "prs": f"/repos/{repo}/pulls?state=open&per_page=30",
             "commits": f"/repos/{repo}/commits?per_page=20", "acciones": f"/repos/{repo}/actions/runs?per_page=10",
             "readme": f"/repos/{repo}/readme"}
    if what in ("issue", "pr") and number:
        path = f"/repos/{repo}/{'issues' if what == 'issue' else 'pulls'}/{int(number)}"
    elif what in paths:
        path = paths[what]
    else:
        return "Que quieres ver: issues, prs, issue (con numero), pr (con numero), commits, acciones o readme."
    try:
        resp = requests.get(GITHUB_API + path, headers=headers, timeout=TIMEOUT)
        if resp.status_code == 404:
            return f"No encuentro {repo} (¿existe? Si es privado, git en este ordenador no tiene acceso)."
        resp.raise_for_status()
        data = resp.json()
        if what == "readme":
            import base64
            return base64.b64decode(data["content"]).decode("utf-8", "replace")[:PAGE_CHARS]
        if what in ("issues", "prs"):
            rows = [f"#{i['number']} {i['title']} ({i['user']['login']}, {i['updated_at'][:10]})"
                    for i in data if what == "prs" or "pull_request" not in i]
            return f"{what} abiertos en {repo}:\n" + ("\n".join(rows) or "(ninguno)")
        if what == "commits":
            return "\n".join(f"{c['sha'][:7]} {c['commit']['author']['date'][:10]} {c['commit']['message'].splitlines()[0]}"
                             for c in data)
        if what == "acciones":
            return "\n".join(f"{r['name']}: {r['status']}/{r['conclusion'] or '-'} ({r['head_branch']}, "
                             f"{r['created_at'][:16]})" for r in data.get("workflow_runs", [])) or "(sin ejecuciones)"
        text = f"#{data['number']} {data['title']} [{data['state']}] por {data['user']['login']}\n\n{data.get('body') or ''}"
        comments = requests.get(data["comments_url"], headers=headers, timeout=TIMEOUT).json()
        text += "\n\nComentarios:\n" + ("\n".join(f"- {c['user']['login']}: {c['body'][:500]}" for c in comments[:20])
                                        or "(ninguno)")
        if what == "pr":
            diff = requests.get(GITHUB_API + path, headers={**headers, "Accept": "application/vnd.github.diff"},
                                timeout=TIMEOUT).text
            text += f"\n\nCambios:\n{diff}"
        return text[:PAGE_CHARS * 2]
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        return f"No se pudo leer {repo} de GitHub: {exc}"


# ---------- bases de datos (solo leer) ----------
_SQL_FIRST = re.compile(r"^\s*(select|with|pragma|explain)\b", re.IGNORECASE)
_SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|merge|exec|execute|grant|revoke|deny|backup|restore|"
    r"shutdown|into|attach|detach|vacuum|dbcc|openrowset|openquery|bulk)\b|\b(sp|xp)_\w+", re.IGNORECASE)
SQL_OUTPUT_CHARS = 8000


def _read_only(sql: str) -> str | None:
    """None si es una consulta de lectura; si no, por que no."""
    if not _SQL_FIRST.match(sql):
        return "Solo consultas de lectura (SELECT, WITH, PRAGMA o EXPLAIN)."
    m = _SQL_FORBIDDEN.search(sql)
    return f"Esa consulta usa «{m.group(0)}», que puede cambiar datos: aqui solo se lee." if m else None


def query_database(source: str, sql: str = "") -> str:
    """Consulta de solo lectura a una base de datos de este ordenador: un
    archivo SQLite (.db, .sqlite, .sqlite3) o SQL Server local
    ("sqlserver:BASE" o "sqlserver:SERVIDOR/BASE"). Sin consulta, lista las
    tablas."""
    source = source.strip()
    sql = sql.strip().rstrip(";")
    if sql and (why := _read_only(sql)):
        return why
    if source.lower().startswith("sqlserver:"):
        target = source.split(":", 1)[1].strip("/")
        server, _, database = target.rpartition("/")
        server = server or "localhost"
        if server.split("\\")[0].lower() not in ("localhost", ".", "127.0.0.1", "(local)"):
            return "Solo SQL Server de este ordenador (sqlserver:BASE o sqlserver:localhost/BASE)."
        if not re.fullmatch(r"[\w$.-]+", database):
            return "Dime la base de datos: sqlserver:NOMBRE_DE_LA_BASE."
        query = sql or ("SELECT TABLE_SCHEMA + '.' + TABLE_NAME AS tabla, TABLE_TYPE AS tipo "
                        "FROM INFORMATION_SCHEMA.TABLES ORDER BY 1")
        try:
            out = subprocess.run(["sqlcmd", "-E", "-C", "-S", server, "-d", database, "-W", "-s", "|",
                                  "-Q", f"SET NOCOUNT ON; {query}"], capture_output=True, check=False, text=True, timeout=120,
                                 encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return "No esta instalado sqlcmd (las herramientas de SQL Server)."
        except subprocess.TimeoutExpired:
            return "La consulta no termino en 2 minutos."
        text = (out.stdout + out.stderr).strip()
        return text[:SQL_OUTPUT_CHARS] + ("\n[... recortado ...]" if len(text) > SQL_OUTPUT_CHARS else "")
    path = Path(source)
    if not path.is_file() or path.suffix.lower() not in (".db", ".sqlite", ".sqlite3"):
        return "Dame un archivo SQLite (.db, .sqlite, .sqlite3) o sqlserver:BASE para SQL Server de este ordenador."
    import sqlite3
    query = sql or "SELECT name AS tabla, type AS tipo FROM sqlite_master WHERE type IN ('table','view') ORDER BY 1"
    try:
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10) as conn:
            cur = conn.execute(query)
            cols = [c[0] for c in cur.description or []]
            rows = cur.fetchmany(200)
    except sqlite3.Error as exc:
        return f"Error de SQLite: {exc}"
    lines = [" | ".join(cols)] + [" | ".join("" if v is None else str(v) for v in r) for r in rows]
    text = "\n".join(lines)
    more = " (se ven las primeras 200 filas)" if len(rows) == 200 else ""
    return f"{len(rows)} filas{more}:\n{text[:SQL_OUTPUT_CHARS]}"
