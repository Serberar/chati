"""Arranque del Python de la herramienta ejecutar_python: pone una "jaula" con
audit hooks (PEP 578) ANTES de ejecutar el codigo del modelo.

Por que (auditoria 2026-09-29): la herramienta la llama el modelo sin pedir
confirmacion, y el modelo lee cosas de fuera (documentos adjuntos, memoria,
archivos). Un documento con "ejecuta este codigo..." podia acabar ejecutando
lo que fuera con los permisos de Windows del usuario. La herramienta es para
comprobar calculos, asi que dentro de la jaula no hay:
  - red (conectar, abrir puertos);
  - programas (subprocess, os.system, os.startfile...);
  - archivos fuera de su carpeta temporal (ni leer ni escribir), salvo leer
    el propio Python y sus librerias;
  - ctypes/winreg/_winapi (con ellos se podria saltar todo lo anterior).

Un audit hook no se puede quitar desde Python una vez puesto. No es un
sandbox perfecto contra alguien que ataque el interprete en C, pero cierra
todo lo que un modelo puede escribir por inyeccion de instrucciones.

Uso: python -I -B python_sandbox.py <carpeta_temporal>  (lee snippet.py de ahi)
"""

import os
import sys

ROOT = os.path.normcase(os.path.realpath(sys.argv[1]))
_READABLE = [ROOT] + sorted({os.path.normcase(os.path.realpath(p))
                             for p in (sys.prefix, sys.base_prefix, sys.exec_prefix, sys.base_exec_prefix)})

_BLOCKED_EVENTS = {
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.startfile", "os.kill",
    "os.fork", "os.forkpty", "socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
    "socket.getaddrinfo", "socket.gethostbyname", "ctypes.dlopen", "ctypes.dlsym", "ctypes.cdata",
    "ctypes.call_function", "winreg.OpenKey", "winreg.CreateKey", "_winapi.CreateProcess",
    "_winapi.CreateNamedPipe", "msvcrt.open_osfhandle", "webbrowser.open", "urllib.Request",
}
# ctypes tampoco: con el se salta todo lo demas. Consecuencia medida: numpy no
# funciona aqui (en Windows carga DLLs con ctypes); para comprobar calculos
# bastan math, statistics, fractions y decimal.
_BLOCKED_MODULES = {"ctypes", "_ctypes", "winreg", "_winapi", "_overlapped", "msvcrt", "multiprocessing",
                    "_multiprocessing", "subprocess", "_posixsubprocess", "socket", "_socket", "ssl", "_ssl"}
# cambiar archivos: solo dentro de la carpeta temporal
_PATH_EVENTS = {"os.remove", "os.rename", "os.rmdir", "os.mkdir", "os.chmod", "os.symlink", "os.link",
                "os.truncate", "os.utime", "shutil.rmtree", "shutil.copyfile", "shutil.move", "os.chdir"}
# recorrer carpetas: donde se puede leer (importar necesita listar las del propio Python)
_LIST_EVENTS = {"os.listdir", "os.scandir", "glob.glob"}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_busy = False


def _inside(path, allowed) -> bool:
    try:
        p = os.path.normcase(os.path.realpath(os.fspath(path) if not isinstance(path, bytes) else os.fsdecode(path)))
    except (TypeError, ValueError, OSError):
        return False
    return any(p == a or p.startswith(a.rstrip("\\/") + os.sep) for a in allowed)


def _deny(what: str):
    raise PermissionError(f"No permitido en ejecutar_python: {what} (solo sirve para calculos)")


def _hook(event: str, args) -> None:
    global _busy
    if _busy:
        return  # las comprobaciones de abajo tambien generan eventos
    _busy = True
    try:
        if event in _BLOCKED_EVENTS or event.startswith(("socket.", "subprocess.", "_winapi.", "winreg.")):
            _deny(event)
        if event == "import" and args and str(args[0]).split(".")[0] in _BLOCKED_MODULES:
            _deny(f"importar {args[0]}")
        if event == "open" and args:
            path, mode, flags = (list(args) + [None, None, 0])[:3]
            if isinstance(path, int):
                return
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                      (isinstance(flags, int) and flags & _WRITE_FLAGS)
            if not _inside(path, [ROOT] if writing else _READABLE):
                _deny(f"{'escribir' if writing else 'leer'} {path}")
        if event in _PATH_EVENTS and args:
            for target in args:
                if isinstance(target, (str, bytes, os.PathLike)) and not _inside(target, [ROOT]):
                    _deny(f"{event} {target}")
        if event in _LIST_EVENTS and args:
            target = args[0] if args[0] is not None else "."
            if isinstance(target, (str, bytes, os.PathLike)) and not _inside(target, _READABLE):
                _deny(f"{event} {target}")
    finally:
        _busy = False


def main() -> None:
    with open(os.path.join(ROOT, "snippet.py"), encoding="utf-8") as f:
        code = f.read()
    compiled = compile(code, "snippet.py", "exec")
    # vienen ya cargados al arrancar Python: importarlos no pasaria por el
    # hook (sus acciones si, pero mejor que ni se puedan importar)
    for name in ("_winapi", "winreg", "msvcrt"):
        sys.modules.pop(name, None)
    sys.addaudithook(_hook)
    exec(compiled, {"__name__": "__main__", "__builtins__": __builtins__})


if __name__ == "__main__":
    main()
