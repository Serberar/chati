"""Comprobar el trabajo del agente de codigo sin fiarlo a que lo haga el: al
terminar un encargo que cambio archivos, Chati ejecuta los tests del proyecto
y, si fallan, se lo devuelve para que lo arregle (Sergio, 2026-10-13: el
modelo local a veces daba por bueno lo que no lo estaba).

Solo detecta y ejecuta; quien decide que hacer con el resultado es main.py
(_watch_code_task)."""
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

TEST_TIMEOUT = 600
OUTPUT_CHARS = 3000


def _python_for(folder: Path) -> str | None:
    """El Python del proyecto: su entorno virtual si tiene, si no el del
    sistema (el agente instala ahi lo que necesita)."""
    for venv in (".venv", "venv", "env"):
        exe = folder / venv / "Scripts" / "python.exe"
        if exe.exists():
            return str(exe)
    return shutil.which("python") or shutil.which("py")


def test_command(folder: Path) -> list[str] | None:
    """Como se lanzan los tests de este proyecto, o None si no tiene."""
    package = folder / "package.json"
    if package.exists():
        try:
            script = (json.loads(package.read_text(encoding="utf-8")).get("scripts") or {}).get("test", "")
        except ValueError:
            script = ""
        if script and "no test specified" not in script:
            npm = shutil.which("npm") or shutil.which("npm.cmd")
            return [npm, "test", "--silent"] if npm else None
    ignore = {"venv", ".venv", "env", "node_modules", ".git", "__pycache__"}
    has_tests = any(p.is_file() and not ignore.intersection(p.relative_to(folder).parts)
                    for pattern in ("test_*.py", "*_test.py") for p in folder.rglob(pattern))
    if has_tests:
        python = _python_for(folder)
        return [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"] if python else None
    return None


def run_tests(folder: Path) -> tuple[bool, str] | None:
    """(pasan, salida recortada), o None si el proyecto no tiene tests."""
    cmd = test_command(folder)
    if not cmd:
        return None
    # compilado nuevo en cada ejecucion: con los .pyc de siempre, un cambio del
    # mismo tamaño en el mismo segundo ejecutaba la version vieja del archivo
    fresh = tempfile.mkdtemp(prefix="chati_pyc_")
    env = {**os.environ, "PYTHONPYCACHEPREFIX": fresh, "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        out = subprocess.run(cmd, cwd=folder, capture_output=True, text=True, timeout=TEST_TIMEOUT,
                             encoding="utf-8", errors="replace", env=env)
    except subprocess.TimeoutExpired:
        return False, f"Los tests no terminaron en {TEST_TIMEOUT // 60} minutos."
    except OSError as exc:
        return False, f"No se pudieron lanzar los tests ({' '.join(cmd[:3])}): {exc}"
    finally:
        shutil.rmtree(fresh, ignore_errors=True)
    text = (out.stdout + "\n" + out.stderr).strip()
    if out.returncode == 5 and "no tests ran" in text:
        return None  # pytest sin tests de verdad
    return out.returncode == 0, text[-OUTPUT_CHARS:]


# Analizador de errores (no de estilo) sobre los archivos que cambio el agente:
# en Python, ruff solo con lo que es un fallo seguro (nombres sin definir,
# comparaciones mal escritas, errores de sintaxis; no imports sin usar ni estilo). En
# JavaScript, el "lint" del propio proyecto si lo tiene.
RUFF_RULES = "E9,F63,F7,F82"  # el conjunto "errores graves" clasico (sin imports sin usar ni estilo)


def _ruff(folder: Path) -> list[str] | None:
    """El ruff de Chati (va en sus dependencias): analiza los archivos sin
    ejecutarlos, asi que sirve para cualquier proyecto Python."""
    import sys
    if subprocess.run([sys.executable, "-m", "ruff", "--version"], capture_output=True).returncode == 0:
        return [sys.executable, "-m", "ruff"]
    exe = shutil.which("ruff")
    return [exe] if exe else None


def run_lint(folder: Path, changed: list[str]) -> tuple[bool, str] | None:
    """(sin errores, salida) del analizador sobre los archivos cambiados, o
    None si no hay analizador para ellos."""
    py_files = [f for f in changed if f.endswith(".py") and (folder / f).exists()]
    cmd = None
    if py_files:
        ruff = _ruff(folder)
        if ruff:
            cmd = ruff + ["check", "--select", RUFF_RULES, "--output-format", "concise", "--no-cache", *py_files]
    elif any(f.endswith((".js", ".jsx", ".ts", ".tsx", ".vue")) for f in changed):
        package = folder / "package.json"
        try:
            has_lint = package.exists() and "lint" in (json.loads(package.read_text(encoding="utf-8"))
                                                       .get("scripts") or {})
        except ValueError:
            has_lint = False
        npm = shutil.which("npm") or shutil.which("npm.cmd")
        if has_lint and npm:
            cmd = [npm, "run", "lint", "--silent"]
    if not cmd:
        return None
    try:
        out = subprocess.run(cmd, cwd=folder, capture_output=True, text=True, timeout=TEST_TIMEOUT,
                             encoding="utf-8", errors="replace")
    except (subprocess.TimeoutExpired, OSError) as exc:
        return None if isinstance(exc, OSError) else (False, "El analizador no termino a tiempo.")
    return out.returncode == 0, (out.stdout + "\n" + out.stderr).strip()[-OUTPUT_CHARS:]


# Modo "sin preguntar": lo que Chati concede solo y lo que sigue preguntando.
_DANGEROUS = re.compile(
    r"\b(rm|del|erase|rmdir|rd|Remove-Item|Clear-Content|format|shutdown|Stop-Computer|Restart-Computer|"
    r"reg|regedit|Set-ExecutionPolicy|runas|Start-Process)\b|"
    r"git\s+(push|reset\s+--hard|clean|checkout\s+--|rebase|filter-branch|branch\s+-D|tag\s+-d)|"
    r"(pip|npm|yarn|pnpm)\s+(uninstall|publish|unpublish)|"
    r"Invoke-(WebRequest|RestMethod|Expression)|\biex\b|curl\s.*\|\s*(sh|bash|iex)|>\s*[A-Za-z]:\\\\(Windows|Program)",
    re.IGNORECASE)


def auto_approvable(permission: str, patterns: list[str]) -> bool:
    """¿Lo puede conceder Chati solo en el modo "sin preguntar"? Editar y
    crear archivos del proyecto y los comandos normales, si; borrar, subir,
    deshacer git, desinstalar o tocar fuera del proyecto, no."""
    # lanzar_programa ya rechaza por su cuenta los comandos peligrosos
    if permission in ("edit", "write", "patch", "todowrite", "chati_lanzar_programa"):
        return True
    if permission == "bash":
        return not any(_DANGEROUS.search(p) for p in patterns)
    return False  # external_directory, webfetch y lo que no se conozca: pregunta


def changed_files(before: dict, after: dict) -> list[str]:
    """Archivos nuevos o cambiados entre dos folder_state."""
    return [f for f, h in after.items() if before.get(f) != h]


def lint_message(output: str, attempt: int, max_attempts: int) -> str:
    return (f"Chati ha pasado el analizador de errores a los archivos que has cambiado y ha encontrado FALLOS "
            f"(aviso {attempt} de {max_attempts}): nombres sin definir, imports que no existen o errores de "
            f"sintaxis. Arreglalos y di que has cambiado.\n\nSalida del analizador:\n{output}")


def failure_message(output: str, attempt: int, max_attempts: int) -> str:
    """Lo que se le devuelve al agente cuando los tests fallan."""
    return (f"Chati ha ejecutado los tests del proyecto despues de tus cambios y FALLAN (aviso {attempt} de "
            f"{max_attempts}). Arreglalo sin cambiar lo que comprueban los tests (salvo que el usuario lo pidiera), "
            f"vuelve a ejecutarlos y di que has cambiado. Si no sabes como arreglarlo, dilo y prepara la consulta "
            f"para copiar.\n\nSalida de los tests:\n{output}")


# El contexto de cada proyecto vive en su AGENTS.md, a la vista del usuario
# (lo lee, lo corrige y va con el proyecto en git), no en la memoria interna
# del agente. OpenCode lo carga solo al empezar cada conversacion. Sergio,
# 2026-10-13: "aqui tienes el contexto, leelo, dame opciones y seguimos...
# despues ejecuta y por ultimo actualiza".
CONTEXT_FILE = "AGENTS.md"
_CONTEXT_SECTIONS = ("Para que sirve; Lenguaje y librerias principales; Estructura (carpetas y archivos "
                     "importantes); Como se ejecuta; Como se prueba (comando exacto de los tests); Decisiones "
                     "tomadas y por que; Convenciones (nombres, estilo)")


def project_has_context(folder: str | Path) -> bool:
    return (Path(folder) / CONTEXT_FILE).exists()


def context_request() -> str:
    """Lo que se añade al primer encargo en un proyecto sin AGENTS.md."""
    return (f"\n\n[Nota de Chati] Este proyecto no tiene {CONTEXT_FILE}. ANTES de la tarea, miralo y crealo en la "
            f"raiz, corto y en español, con estos apartados: {_CONTEXT_SECTIONS}. Solo lo que hayas visto, sin "
            "inventar; lo que no sepas, dejalo como pendiente. Despues haz la tarea.")


def context_update_request() -> str:
    """Lo que Chati pide al terminar un encargo: actualizar el contexto."""
    return (f"[Chati] Tarea terminada. Ultimo paso: actualiza {CONTEXT_FILE} con lo que ha cambiado en esta tarea "
            "(estructura, comandos, decisiones nuevas y por que, convenciones). Cambia solo lo que ya no sea "
            f"verdad o falte; no lo reescribas entero ni toques ningun otro archivo. Si no existe, crealo con: "
            f"{_CONTEXT_SECTIONS}. Contesta en una linea que has actualizado.")


_APPROVAL = re.compile(
    r"^\W*((la|el|opci[oó]n|propuesta)\s*(n[uú]mero\s*)?\d+|(la|el)\s+(primera|segunda|tercera|primero|segundo|"
    r"tercero|ultima|última)|adelante|vale|venga|dale|s[ií]\b|ok\b|okay|de acuerdo|hazlo|hazla|perfecto|"
    r"me parece bien|me gusta|correcto)\b", re.IGNORECASE)
_REFUSAL = re.compile(r"\b(no|ninguna|ninguno|nada|mejor otra|otra opcion|otra opción)\b", re.IGNORECASE)


def is_approval(text: str) -> bool:
    """¿Contesta a las opciones del plan eligiendo una o dando el visto bueno
    ("la 2", "adelante", "vale, pero con X")? Una pregunta no lo es."""
    text = text.strip()
    # el "no" solo cuenta en la primera parte: "la 1 pero no toques la base de
    # datos" si elige; "vale no", "eso no me convence" no
    first = re.split(r"[,.;:]| pero ", text, maxsplit=1)[0]
    return bool(_APPROVAL.search(text)) and "?" not in text and not _REFUSAL.search(first)


_BIG = re.compile(r"\b(refactoriz\w*|reestructur\w*|migr\w*|aplicaci[oó]n (entera|completa)|desde cero|"
                  r"varios (archivos|ficheros|modulos)|todo el proyecto|nueva funcionalidad|sistema de)\b",
                  re.IGNORECASE)


def is_big_task(task: str) -> bool:
    """Encargo grande: mejor planificar primero y luego construir paso a paso
    (los modelos pequeños se pierden mucho menos en tareas cortas)."""
    return len(task) > 400 or bool(_BIG.search(task))


def folder_state(folder: Path) -> dict:
    """Huella del contenido de cada archivo del proyecto (sin entornos ni
    caches), para saber si el agente ha cambiado algo. Por contenido y no por
    fecha y tamaño: un cambio del mismo tamaño en el mismo instante no se veia."""
    import hashlib
    ignore = {"venv", ".venv", "env", "node_modules", ".git", "__pycache__", ".pytest_cache"}
    state = {}
    for p in folder.rglob("*"):
        if p.is_file() and not ignore.intersection(p.relative_to(folder).parts):
            state[p.relative_to(folder).as_posix()] = hashlib.sha1(p.read_bytes()).hexdigest()
    return state


# Datos que Chati comprueba el mismo antes de pasar el encargo al agente: el
# modelo local a veces contestaba sobre una libreria inventada sin mirar si
# existia, y no guardaba lo que se le pedia recordar (banco 2026-10-13).
_PKG_CONTEXT = re.compile(r"librer[ií]a|paquete|m[oó]dulo|package|library|\bpip\b|\bnpm\b|instal", re.IGNORECASE)
_PKG_NAME = re.compile(
    r"['\"`«]([A-Za-z0-9][A-Za-z0-9_.\-/@]{1,60})['\"`»]|"
    r"(?:librer[ií]a|paquete|m[oó]dulo|package|library)\s+(?:de\s+(?:python|npm|node)\s+)?"
    r"([A-Za-z0-9][A-Za-z0-9_.\-]*[\-_.0-9][A-Za-z0-9_.\-]*|[a-z][a-z0-9]{2,})|"
    r"(?:pip|npm)\s+install\s+([A-Za-z0-9@][A-Za-z0-9_.\-/]+)", re.IGNORECASE)
_NOT_PKG = {"que", "para", "con", "python", "npm", "node", "javascript", "excel", "una", "esta", "este", "cual",
            "mejor", "buena", "recomiendas", "nueva", "parecida", "como"}
_NPM_HINT = re.compile(r"\bnpm\b|node|javascript|\bjs\b|typescript|react|vue", re.IGNORECASE)


def package_mentions(text: str) -> list[tuple[str, str]]:
    """(nombre, ecosistema) de las librerias concretas que nombra el encargo."""
    if not _PKG_CONTEXT.search(text):
        return []
    eco = "npm" if _NPM_HINT.search(text) else "python"
    names = []
    for m in _PKG_NAME.finditer(text):
        name = next(g for g in m.groups() if g).strip(".")
        if name.lower() not in _NOT_PKG and name not in names and not name.endswith((".py", ".js", ".txt")):
            names.append(name)
    return [(n, eco) for n in names[:3]]


_REMEMBER = re.compile(r"^\W*(recuerda|acu[eé]rdate de|ap[uú]nta(te)?|no olvides|guarda en (la )?memoria|memoriza)\b"
                       r"\s*(que|esto|lo siguiente|de que)?\s*[:,]?\s*(?P<what>.+)", re.IGNORECASE | re.DOTALL)


def remember_request(text: str) -> str | None:
    """Lo que el usuario pide recordar ("recuerda que ..."), o None."""
    m = _REMEMBER.match(text.strip())
    return m.group("what").strip() if m and "?" not in text and len(m.group("what").strip()) > 5 else None


# qwen3-coder a veces escribe la llamada a una herramienta como texto
# ("<function=chati_info_paquete> <parameter=...") en vez de hacerla, y la tarea
# se queda parada ahi (banco 2026-10-13, 2 de 2 en "libreria_inventada").
_TEXT_TOOL_CALL = re.compile(r"<function=[\w.-]+>|<tool_call>|</tool_call>")
TOOL_CALL_NUDGES = 2


def broken_tool_call(steps: list[dict]) -> bool:
    """¿La tarea ha acabado con una llamada a herramienta escrita como texto?"""
    last = next((s for s in reversed(steps) if s.get("kind") in ("text", "tool", "user")), None)
    return bool(last and last.get("kind") == "text" and _TEXT_TOOL_CALL.search(last.get("text", "")))


def tool_call_nudge() -> str:
    return ("[Chati] Tu ultima llamada a una herramienta ha salido como texto (<function=...>) y no se ha "
            "ejecutado. Vuelve a llamarla de verdad, como herramienta, y sigue con la tarea.")


# Repaso del propio trabajo: tras un cambio grande, con los tests y el
# analizador ya bien, se le enseña al agente lo que ha cambiado para que lo
# revise como lo haria otra persona (Sergio, 2026-10-13).
REVIEW_MIN_LINES = 40
REVIEW_MIN_FILES = 3


def needs_review(diff: str) -> bool:
    lines = [ln for ln in diff.splitlines() if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    files = sum(ln.startswith("diff --git") for ln in diff.splitlines())
    return len(lines) >= REVIEW_MIN_LINES or files >= REVIEW_MIN_FILES


def review_request(diff: str) -> str:
    return ("[Chati] Antes de darlo por terminado, revisa tus propios cambios como si fueran de otra persona. "
            "Busca: fallos de logica, casos que no contemplas (vacios, nulos, errores de red o de archivo), "
            "cosas que rompan lo que ya funcionaba, codigo que sobra y problemas de seguridad. Si encuentras algo, "
            "arreglalo y di que has cambiado y por que; si esta bien, dilo en una linea. No cambies nada mas."
            f"\n\nTus cambios:\n```diff\n{diff}\n```")


def ended_without_answer(steps: list[dict]) -> bool:
    """¿Ha acabado despues de usar herramientas sin decir nada al usuario?
    (gpt-oss a veces se para asi: banco 2026-10-13, "actualizaciones")."""
    last = next((s for s in reversed(steps) if s.get("kind") in ("text", "tool", "user")), None)
    return bool(last and last.get("kind") == "tool")


def answer_nudge() -> str:
    return ("[Chati] Has terminado sin contestar al usuario. Respondele ahora con lo que has encontrado o hecho, "
            "en español y concreto (nombres, versiones, archivos). No repitas lo que ya has hecho.")
