"""Crea Escritorio/pruebas con programas rotos (casos tipicos) para probar
si el agente de Chati los arregla solo. Se puede volver a ejecutar: los deja
como al principio."""
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path.home() / "Desktop" / "pruebas"
if ROOT.exists():
    shutil.rmtree(ROOT)

CASES = {
    "notas/notas.py": '''import json, os

ARCHIVO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notas.json")

def cargar():
    return json.load(open(ARCHIVO, encoding="utf-8")) if os.path.exists(ARCHIVO) else []

def guardar(notas):
    json.dump(notas, open(ARCHIVO, "w", encoding="utf-8"), ensure_ascii=False)

def main():
    notas = cargar()
    while True:
        print("\\n1. Ver notas  2. Nueva nota  3. Borrar nota  4. Salir")
        op = input("Opcion: ")
        if op == "1":
            for i, n in enumerate(notas, 1):
                print(i, n)
        elif op == "2":
            notas.append(input("Texto: "))
            guardar(notas)
        elif op == "3":
            notas.pop(int(input("Numero: ")) - 1)
            guardar(notas)
        elif op == "4":
            break

main()
''',
    "facturas/calcular_total.py": '''import csv, os

carpeta = os.path.dirname(os.path.abspath(__file__))
total = 0
with open(os.path.join(carpeta, "facturas.csv"), encoding="utf-8") as f:
    for fila in csv.DictReader(f, delimiter=";"):
        total += float(fila["importe"])
print(f"Total facturado: {total:.2f} EUR")
''',
    "facturas/facturas.csv": "cliente;importe\nAcme;12,50\nGlobex;1.200,00\nInitech;99,99\n",
    "contador/contador.py": '''import tkinter as tk

ventana = tk.Tk()
ventana.title("Contador")
cuenta = tk.IntVar(value=0)

def sumar():
    cuenta.set(cuenta.get() + 1)

tk.Label(ventana, textvariable=cuenta, font=("Segoe UI", 32)).pack(padx=40, pady=10)
tk.Button(ventana, text="Sumar 1", command=sumar()).pack(pady=10)
ventana.mainloop()
''',
    "informe/informe.py": '''lineas = open("C:/Users/Antonio/Documents/ventas.txt", encoding="utf-8").read().splitlines()
ventas = [int(x) for x in lineas if x.strip()]
print(f"Ventas: {len(ventas)}  Total: {sum(ventas)}  Media: {sum(ventas) / len(ventas):.1f}")
''',
    "informe/ventas.txt": "120\n80\n45\n300\n",
}
for name, text in CASES.items():
    path = ROOT / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")

# notas: el mismo fallo que HostManager (programa de consola compilado sin consola)
work = ROOT / "notas"
subprocess.run(["pyinstaller", "--noconfirm", "--onefile", "--windowed", "--log-level", "WARN", "--name", "Notas",
                "notas.py"], cwd=work, check=True)
shutil.move(str(work / "dist" / "Notas.exe"), work / "Notas.exe")
shutil.rmtree(work / "dist")
shutil.rmtree(work / "build")
(work / "Notas.spec").unlink()
print("listo:", sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*") if p.is_file()))
