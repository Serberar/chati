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
import re
import subprocess
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
        except Exception:  # noqa: BLE001 - se queda lo que hubiera
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
            subprocess.run(["git", "-C", str(target), "pull", "--ff-only", "--depth", "1"], capture_output=True,
                           text=True, timeout=180)
        else:
            out = subprocess.run(["git", "clone", "--depth", "1", clone_url, str(target)], capture_output=True,
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
