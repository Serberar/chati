"""Crea el usuario administrador principal con los datos que se pidieron en
el asistente del instalador (setup/chati_installer.iss, pagina "Administrador
principal"). El instalador los deja en un archivo temporal - una linea por
campo: usuario, contraseña, pregunta de seguridad, respuesta - en vez de
pasarlos por linea de comandos, donde la contraseña quedaria visible en la
lista de procesos. Este script borra el archivo en cuanto lo lee.

Si ya hay usuarios (reinstalacion/actualizacion) no toca nada."""

import argparse
import sys
from pathlib import Path

import users


def create_admin_from_file(path: Path) -> str:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    path.unlink()
    lines += [""] * (4 - len(lines))
    username, password, question, answer = (l.strip() if i != 1 else l for i, l in enumerate(lines[:4]))

    users.init_db()
    if users.any_users_exist():
        return "Ya existen usuarios - no se crea un administrador nuevo."
    users.create_user(username, password, "admin", question or None, answer or None)
    return f"Administrador '{username}' creado."


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-file", required=True, type=Path)
    args = parser.parse_args()
    if not args.from_file.exists():
        print("No hay datos de administrador - se creara al registrarse en la app.")
        return
    try:
        print(create_admin_from_file(args.from_file))
    except users.UserError as exc:
        print(f"No se pudo crear el administrador: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
