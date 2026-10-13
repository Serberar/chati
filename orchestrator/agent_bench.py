"""Banco de pruebas del agente de codigo (modo Codigo de Chati): tareas fijas y
realistas sobre un proyecto pequeño, con aciertos y tiempos medidos. Para
comparar modelos, instrucciones o herramientas midiendo en vez de adivinar
(Sergio, 2026-10-13).

Pasa por Chati igual que la interfaz (/agent/tasks): preparacion de la GPU,
eleccion de agente (pregunta -> planificar) y permisos, que aqui se conceden
solos ("once"). Cada tarea empieza con el proyecto de prueba recien creado.

Uso (con Chati arrancado y SIN usarlo a la vez: ocupa la GPU):
  python agent_bench.py --usuario U --clave C [--veces 2] [--solo tarea1 tarea2] [--modelo-rapido]
Resultados: data/agente_bench/resultados_<fecha>.json y una tabla al final."""
import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

import requests

import agent_tools
from paths import DATA_DIR

BASE = "http://127.0.0.1:8899"
BENCH_DIR = DATA_DIR / "agente_bench"
TASK_TIMEOUT = 1500

PRECIOS = '''def precio_con_iva(precio, iva=0.21):
    """Precio con IVA, redondeado a centimos."""
    return round(precio * (1 + iva), 2)


def descuento(precio, pct):
    """Precio con un descuento de pct por ciento (10 = 10 %)."""
    return precio - precio * pct
'''
PRECIOS_BIEN = PRECIOS.replace("precio - precio * pct", "precio - precio * pct / 100")
TESTS = '''from precios import descuento, precio_con_iva


def test_iva():
    assert precio_con_iva(100) == 121.0


def test_descuento():
    assert descuento(100, 10) == 90
'''
README = "# Tienda\nCalcula precios de la tienda: IVA y descuentos.\n"
REQUIREMENTS = "requests==2.20.0\nflask==2.0.0\n"


@dataclass
class Run:
    text: str = ""
    tools: list = field(default_factory=list)
    seconds: float = 0.0
    error: str | None = None
    check: str = ""  # lo que dijo el vigilante de Chati (tests)


@dataclass
class Task:
    id: str
    prompt: str
    check: Callable[[Path, Run, dict], tuple[bool, str]]
    fixed: bool = False  # proyecto sin el fallo de descuento


def _make_project(folder: Path, fixed: bool) -> None:
    # carpeta nueva siempre: OpenCode deja abiertas las de tareas anteriores
    # y en Windows no se pueden borrar mientras tanto
    folder.mkdir(parents=True)
    (folder / "precios.py").write_text(PRECIOS_BIEN if fixed else PRECIOS, encoding="utf-8")
    (folder / "test_precios.py").write_text(TESTS, encoding="utf-8")
    (folder / "README.md").write_text(README, encoding="utf-8")
    (folder / "requirements.txt").write_text(REQUIREMENTS, encoding="utf-8")


def _snapshot(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in folder.rglob("*")
            if p.is_file() and "__pycache__" not in p.parts and ".pytest_cache" not in p.parts}


def _tests_pass(folder: Path) -> bool:
    out = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"], cwd=folder,
                         capture_output=True, text=True, timeout=300)
    return out.returncode == 0


def _unchanged(folder: Path, before: dict) -> bool:
    return _snapshot(folder) == before


def _used(run: Run, tool: str) -> bool:
    return any(tool in t for t in run.tools)


# ---------- comprobaciones ----------

def _check_pregunta_libreria(folder, run, before):
    if not _unchanged(folder, before):
        return False, "ha tocado archivos en una pregunta"
    ok = re.search(r"openpyxl|pandas", run.text, re.IGNORECASE)
    return bool(ok), "recomienda openpyxl/pandas" if ok else "no recomienda una libreria real"


def _check_libreria_inventada(folder, run, before):
    ok = re.search(r"no existe|no hay ningun|no se encuentra|no encontr", run.text, re.IGNORECASE)
    return bool(ok and _unchanged(folder, before)), ("dice que no existe" if ok else "no dice que no existe")


def _check_explicar(folder, run, before):
    if not _unchanged(folder, before):
        return False, "ha tocado archivos en una pregunta"
    ok = re.search(r"iva|21", run.text, re.IGNORECASE)
    return bool(ok), "explica el IVA" if ok else "no explica lo que hace"


def _check_actualizaciones(folder, run, before):
    if not _unchanged(folder, before):
        return False, "ha tocado archivos al revisar"
    names = all(re.search(n, run.text, re.IGNORECASE) for n in ("flask", "requests"))
    used = _used(run, "revisar_actualizaciones")
    return names and used, f"herramienta {'si' if used else 'NO'}, nombra flask y requests: {'si' if names else 'NO'}"


def _check_arreglar(folder, run, before):
    if (folder / "test_precios.py").read_bytes() != before["test_precios.py"]:
        return False, "ha cambiado el test"
    return (True, "tests pasan") if _tests_pass(folder) else (False, "los tests siguen fallando")


def _check_funcion_nueva(folder, run, before):
    code = (folder / "precios.py").read_text(encoding="utf-8")
    tests = (folder / "test_precios.py").read_text(encoding="utf-8")
    if "def precio_final" not in code:
        return False, "no existe precio_final"
    if tests.count("def test_") <= TESTS.count("def test_"):
        return False, "no ha añadido test"
    return (True, "funcion, test y tests pasan") if _tests_pass(folder) else (False, "los tests fallan")


def _check_renombrar(folder, run, before):
    leftovers = [p.name for p in folder.glob("*.py") if "precio_con_iva" in p.read_text(encoding="utf-8")]
    if leftovers:
        return False, f"queda el nombre viejo en {leftovers}"
    if "def con_iva" not in (folder / "precios.py").read_text(encoding="utf-8"):
        return False, "no existe con_iva"
    return (True, "renombrada y tests pasan") if _tests_pass(folder) else (False, "los tests fallan")


def _check_excel(folder, run, before):
    path = folder / "leer_excel.py"
    if not path.exists():
        return False, "no existe leer_excel.py"
    code = path.read_text(encoding="utf-8")
    ok = re.search(r"import openpyxl|from openpyxl|import pandas", code)
    return bool(ok), "usa openpyxl/pandas" if ok else "no usa una libreria real"


def _check_memoria(folder, run, before):
    index = agent_tools.memory_index()
    ok = "pytest" in index.lower()  # lo guarda Chati al ver "recuerda que..." o el agente
    return ok and _unchanged(folder, before), "guardado en memoria" if ok else "no lo ha guardado"


TASKS = [
    Task("pregunta_libreria", "¿Que libreria me recomiendas para leer ficheros Excel en Python? Responde en dos lineas.",
         _check_pregunta_libreria),
    Task("libreria_inventada", "¿Existe la libreria de Python 'excel-turbo-reader-pro'? ¿Cual es su ultima version?",
         _check_libreria_inventada),
    Task("explicar", "¿Que hace la funcion precio_con_iva de este proyecto?", _check_explicar),
    Task("actualizaciones", "Revisa si las dependencias de este proyecto estan al dia.", _check_actualizaciones),
    Task("arreglar_bug", "El test test_descuento falla. Arreglalo sin cambiar el test.", _check_arreglar),
    Task("funcion_nueva", "Añade a precios.py una funcion precio_final(precio, pct_descuento) que aplique el descuento "
                          "y despues el IVA, con su test en test_precios.py. Ejecuta los tests.", _check_funcion_nueva,
         fixed=True),
    Task("renombrar", "Renombra la funcion precio_con_iva a con_iva en todo el proyecto y comprueba que los tests "
                      "pasan.", _check_renombrar, fixed=True),
    Task("excel", "Crea leer_excel.py con una funcion leer_filas(ruta) que lea un fichero .xlsx y devuelva sus filas "
                  "como listas, usando la libreria adecuada.", _check_excel, fixed=True),
    Task("memoria", "Recuerda que en este proyecto los tests se lanzan con: python -m pytest -q", _check_memoria),
]


# ---------- ejecucion ----------

def _run_task(session: requests.Session, task: Task, folder: Path, rapido: bool) -> Run:
    t0 = time.time()
    resp = session.post(f"{BASE}/agent/tasks", json={"task": task.prompt, "project": str(folder), "rapido": rapido},
                        timeout=120)
    data = resp.json()
    sid = data.get("session_id")
    if not sid:
        return Run(error=f"no arranco: {data}", seconds=time.time() - t0)
    view = {}
    while time.time() - t0 < TASK_TIMEOUT:
        time.sleep(5)
        try:
            view = session.get(f"{BASE}/agent/tasks/{sid}", timeout=60).json()
        except (requests.RequestException, ValueError):
            continue  # Chati u OpenCode reiniciandose: se vuelve a preguntar
        for perm in view.get("permissions") or []:
            session.post(f"{BASE}/agent/permissions/{perm['id']}", json={"reply": "once"}, timeout=60)
        if view.get("questions"):
            break
        chati_working = (view.get("verificacion") or "").endswith("…")  # tests, plan -> construir
        if view.get("status") == "idle" and view.get("steps") and not chati_working:
            # el vigilante de Chati tarda unos segundos en empezar a comprobar
            if time.time() - t0 > 40 or view.get("verificacion"):
                break
    else:
        session.post(f"{BASE}/agent/tasks/{sid}/abort", timeout=60)
        view["error"] = view.get("error") or "tiempo agotado"
    steps = view.get("steps") or []
    texts = [s["text"] for s in steps if s["kind"] == "text"]
    return Run(text="\n".join(texts[-2:]), tools=[s.get("tool", "") for s in steps if s["kind"] == "tool"],
               seconds=time.time() - t0, error=view.get("error"), check=view.get("verificacion") or "")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--usuario", required=True)
    parser.add_argument("--clave", required=True)
    parser.add_argument("--veces", type=int, default=2)
    parser.add_argument("--solo", nargs="*")
    parser.add_argument("--modelo-rapido", action="store_true", help="el agente con el modelo rapido (qwen3:8b)")
    parser.add_argument("--etiqueta", default="")
    args = parser.parse_args()

    session = requests.Session()
    login = session.post(f"{BASE}/auth/login", json={"username": args.usuario, "password": args.clave}, timeout=60)
    session.headers["X-Session-Token"] = login.json()["token"]
    tasks = [t for t in TASKS if not args.solo or t.id in args.solo]
    memory_before = set(p.name for p in agent_tools.MEMORY_DIR.glob("*.md"))
    shutil.rmtree(BENCH_DIR / "proyecto", ignore_errors=True)  # las de otras veces, si se dejan
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    results = []
    for round_ in range(args.veces):
        for task in tasks:
            folder = BENCH_DIR / "proyecto" / f"{stamp}_{round_ + 1}_{task.id}"
            _make_project(folder, task.fixed)
            before = _snapshot(folder)
            run = _run_task(session, task, folder, args.modelo_rapido)
            ok, why = (False, f"error: {run.error}") if run.error and not run.text else task.check(folder, run, before)
            results.append({"tarea": task.id, "vuelta": round_ + 1, "ok": ok, "motivo": why,
                            "segundos": round(run.seconds), "vigilante": run.check, "herramientas": run.tools, "respuesta": run.text[-600:]})
            print(f"{'OK ' if ok else 'MAL'} {task.id:20s} {round(run.seconds):5d} s  {why}", flush=True)
    # la memoria del banco no se queda en la del usuario
    for name in set(p.name for p in agent_tools.MEMORY_DIR.glob("*.md")) - memory_before:
        if name != agent_tools.MEMORY_INDEX.name:
            agent_tools.memory_delete(Path(name).stem)

    BENCH_DIR.mkdir(parents=True, exist_ok=True)
    out = BENCH_DIR / f"resultados_{stamp}{('_' + args.etiqueta) if args.etiqueta else ''}.json"
    out.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
    total = len(results)
    hits = sum(r["ok"] for r in results)
    print(f"\n{hits}/{total} aciertos, {sum(r['segundos'] for r in results) // max(total, 1)} s de media. {out}")
    for task in tasks:
        mine = [r for r in results if r["tarea"] == task.id]
        print(f"  {task.id:20s} {sum(r['ok'] for r in mine)}/{len(mine)}  "
              f"{sum(r['segundos'] for r in mine) // max(len(mine), 1)} s")


if __name__ == "__main__":
    main()
