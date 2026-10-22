"""Â¿Estan arreglados los casos de Escritorio/pruebas? Una linea por caso."""
import ast
import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path.home() / "Desktop" / "pruebas"
user32 = ctypes.windll.user32


def has_window(pid: int) -> bool:
    found = []
    proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        if user32.IsWindowVisible(hwnd) and buf.value and owner.value in pids and "error" not in buf.value.lower() \
                and "exception" not in buf.value.lower():
            found.append(hwnd)
        return True
    pids = {pid} | {int(x) for x in subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId={pid}').ProcessId"],
        capture_output=True, text=True, check=False).stdout.split()}
    user32.EnumWindows(proc(cb), 0)
    return bool(found)


def gui_opens(cmd) -> bool:
    p = subprocess.Popen(cmd)
    time.sleep(6)
    ok = p.poll() is None and has_window(p.pid)
    subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, check=False)
    return ok


def run(script: Path) -> str:
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=60,
                         cwd=script.parent, stdin=subprocess.DEVNULL, check=False)
    return out.stdout + out.stderr


def button_ok(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.keyword) and node.arg == "command":
            return not isinstance(node.value, ast.Call)
    return False


results = {
    "notas (exe con ventana)": (ROOT / "notas" / "Notas.exe").exists() and gui_opens([str(ROOT / "notas" / "Notas.exe")]),
    "facturas (total 1312.49)": "1312.49" in run(ROOT / "facturas" / "calcular_total.py"),
    "contador (boton conectado y abre)": button_ok(ROOT / "contador" / "contador.py")
    and gui_opens([sys.executable.replace("python.exe", "pythonw.exe"), str(ROOT / "contador" / "contador.py")]),
    "informe (total 545)": "Total: 545" in run(ROOT / "informe" / "informe.py"),
}
for name, ok in results.items():
    print(("OK   " if ok else "MAL  ") + name)
print(f"{sum(results.values())}/{len(results)}")


