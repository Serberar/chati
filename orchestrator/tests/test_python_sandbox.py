"""La herramienta ejecutar_python la llama el modelo sin pedir permiso, y el
modelo lee textos de fuera: una instruccion escondida en un documento no debe
poder usarla para tocar el ordenador (auditoria 2026-09-29)."""

import pytest

import tools


def _last(out: str) -> str:
    lines = out.strip().splitlines()
    return lines[-1] if lines else ""


def test_calculations_still_work():
    assert "1267650600228229401496703205376" in tools.ejecutar_python("print(2**100)")
    out = tools.ejecutar_python("import statistics, fractions, decimal, math\n"
                                "print(statistics.mean([1, 2, 3]), fractions.Fraction(1, 3) + fractions.Fraction(1, 6))")
    assert "2 1/2" in out
    assert "hola" in tools.ejecutar_python("open('a.txt', 'w').write('hola'); print(open('a.txt').read())")


@pytest.mark.parametrize("code", [
    "import os; os.system('whoami')",
    "import subprocess; subprocess.run('whoami')",
    "m = __import__('sub' + 'process'); m.run('whoami')",
    "import os; print(os.popen('whoami').read())",
    "import os; os.startfile('notepad')",
    "import socket; socket.create_connection(('127.0.0.1', 8188))",
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8899')",
    "import ctypes; ctypes.windll.kernel32.WinExec(b'calc', 1)",
    "import winreg",
    "import _winapi",
    "import sys; sys.modules['_winapi'].CreateProcess(None, 'calc', None, None, 0, 0, None, None, None)",
    "import os; print(os.listdir('C:/'))",
    "print(open('C:/Users/Public/nada.txt', 'w'))",
    "print(open('C:/Windows/win.ini').read())",
    "import os; os.remove('C:/no-existe.txt')",
])
def test_nothing_outside_the_sandbox(code):
    last = _last(tools.ejecutar_python(code))
    # bloqueado por la jaula, o el modulo ni siquiera esta (KeyError)
    assert "No permitido en ejecutar_python" in last or last.startswith("KeyError"), last


def test_the_cage_has_a_memory_ceiling():
    # sin tope, pedir decenas de GB dejaba el PC colgado (auditoria 2026-10-05)
    out = tools.ejecutar_python("x = bytearray(1024**3)\nprint('reservado')")
    assert "reservado" not in out
    assert "memoria" in out
    assert "ok" in tools.ejecutar_python("x = bytearray(20 * 1024**2)\nprint('ok')")


def test_endless_output_does_not_pile_up_in_memory():
    out = tools.ejecutar_python("print('y' * 10**7)")
    assert len(out) < tools.PYTHON_OUTPUT_LIMIT + 100
