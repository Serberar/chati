"""Usuarios, contraseñas y cifrado por sobre (envelope encryption) zero-
knowledge: cada usuario tiene una clave de datos (DEK) aleatoria que cifra
sus conversaciones/archivos, y esa DEK se guarda envuelta (cifrada) con una
clave derivada de su contraseña via Argon2id (KDF, no solo para verificar
login). Ni el admin ni nadie sin la contraseña exacta puede desenvolver la
DEK y leer los datos.

Si se pierde la contraseña y se usa la pregunta de seguridad para recuperar
el acceso, se genera una DEK nueva (key_generation += 1): los datos
cifrados con la DEK anterior quedan permanentemente inaccesibles. Es
zero-knowledge de verdad, no un atajo - ver ROADMAP.md, punto 0 (decisiones
tomadas con Sergio el 2026-09-23, no reabrir sin que el lo pida)."""

import base64
import json
import os
import secrets
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from argon2.low_level import Type, hash_secret_raw
from cryptography.fernet import Fernet, InvalidToken

from paths import DATA_DIR

DB_PATH = DATA_DIR / "users.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

REGISTRATION_KEY_FILE = DATA_DIR / "registration_key.txt"
SECURITY_LOG_FILE = DATA_DIR / "security_events.jsonl"

_password_hasher = PasswordHasher()

# Costes de Argon2id para derivar la clave de cifrado (KEK) a partir de la
# contraseña - deliberadamente caros (segundos, no milisegundos) porque esto
# protege datos reales, no solo una sesion web.
KDF_TIME_COST = 3
KDF_MEMORY_COST = 65536  # KiB (64 MB)
KDF_PARALLELISM = 2
KDF_KEY_LEN = 32


class UserError(Exception):
    """Error de dominio (usuario duplicado, credenciales invalidas, etc.),
    para que quien llame pueda mostrar un mensaje claro sin exponer trazas."""


def _connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    with closing(_connect()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('admin', 'user')),
                password_hash TEXT NOT NULL,
                salt BLOB NOT NULL,
                wrapped_dek BLOB NOT NULL,
                key_generation INTEGER NOT NULL DEFAULT 1,
                security_question TEXT,
                security_answer_hash TEXT,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()


init_db()


def _derive_kek(secret: str, salt: bytes) -> bytes:
    """KDF Argon2id cruda (no el hash de verificacion de argon2-cffi, que
    lleva su propia sal aleatoria por diseño y no sirve para derivar una
    clave reproducible) - misma contraseña + misma sal = misma clave."""
    return hash_secret_raw(
        secret=secret.encode("utf-8"), salt=salt,
        time_cost=KDF_TIME_COST, memory_cost=KDF_MEMORY_COST,
        parallelism=KDF_PARALLELISM, hash_len=KDF_KEY_LEN, type=Type.ID,
    )


def _fernet_from_raw_key(raw_key: bytes) -> Fernet:
    return Fernet(base64.urlsafe_b64encode(raw_key))


def _row_to_dict(row) -> dict:
    return {
        "id": row[0], "username": row[1], "role": row[2], "password_hash": row[3],
        "salt": row[4], "wrapped_dek": row[5], "key_generation": row[6],
        "security_question": row[7], "security_answer_hash": row[8], "created_at": row[9],
    }


def get_user(username: str) -> dict | None:
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT id, username, role, password_hash, salt, wrapped_dek, key_generation, "
            "security_question, security_answer_hash, created_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_users() -> list[dict]:
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, username, role, created_at FROM users ORDER BY created_at"
        ).fetchall()
    return [{"id": r[0], "username": r[1], "role": r[2], "created_at": r[3]} for r in rows]


def any_users_exist() -> bool:
    with closing(_connect()) as conn:
        return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def create_user(username: str, password: str, role: str,
                 security_question: str | None = None, security_answer: str | None = None) -> str:
    """Crea el usuario con una DEK nueva aleatoria, envuelta con la clave
    derivada de su contraseña. Devuelve el id del usuario creado."""
    username = username.strip()
    if not username:
        raise UserError("El nombre de usuario no puede estar vacio.")
    if len(password) < 8:
        raise UserError("La contraseña debe tener al menos 8 caracteres.")
    if role not in ("admin", "user"):
        raise UserError(f"Rol invalido: {role}")
    if get_user(username):
        raise UserError(f"Ya existe un usuario '{username}'.")

    salt = os.urandom(16)
    kek = _derive_kek(password, salt)
    dek = Fernet.generate_key()
    wrapped_dek = _fernet_from_raw_key(kek).encrypt(dek)
    password_hash = _password_hasher.hash(password)
    security_answer_hash = _password_hasher.hash(security_answer.strip().lower()) if security_answer else None

    user_id = uuid.uuid4().hex
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO users (id, username, role, password_hash, salt, wrapped_dek, "
            "key_generation, security_question, security_answer_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
            (user_id, username, role, password_hash, salt, wrapped_dek,
             security_question, security_answer_hash, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
    return user_id


def login(username: str, password: str) -> dict:
    """Verifica la contraseña y desenvuelve la DEK. Devuelve
    {id, username, role, key_generation, dek} - la DEK en crudo, para
    guardar SOLO en memoria del proceso durante la sesion, nunca en disco.
    Lanza UserError si las credenciales no son validas."""
    user = get_user(username)
    if not user:
        raise UserError("Usuario o contraseña incorrectos.")
    try:
        _password_hasher.verify(user["password_hash"], password)
    except VerifyMismatchError:
        raise UserError("Usuario o contraseña incorrectos.")

    kek = _derive_kek(password, user["salt"])
    try:
        dek = _fernet_from_raw_key(kek).decrypt(user["wrapped_dek"])
    except InvalidToken:
        # no deberia pasar si el hash de arriba verifico bien, pero por si
        # la DEK envuelta se corrompiese, fallar seguro en vez de devolver basura
        raise UserError("No se pudo desbloquear la clave de datos.")

    return {
        "id": user["id"], "username": user["username"], "role": user["role"],
        "key_generation": user["key_generation"], "dek": dek,
    }


def change_password(username: str, old_password: str, new_password: str) -> None:
    """Reenvuelve la MISMA dek con una clave derivada de la nueva contraseña
    - no hace falta re-cifrar ningun dato existente."""
    if len(new_password) < 8:
        raise UserError("La contraseña debe tener al menos 8 caracteres.")
    session = login(username, old_password)  # valida la contraseña antigua y desenvuelve la dek
    dek = session["dek"]

    new_salt = os.urandom(16)
    new_kek = _derive_kek(new_password, new_salt)
    new_wrapped_dek = _fernet_from_raw_key(new_kek).encrypt(dek)
    new_password_hash = _password_hasher.hash(new_password)

    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ?, wrapped_dek = ? WHERE username = ?",
            (new_password_hash, new_salt, new_wrapped_dek, username),
        )
        conn.commit()


def reset_via_security_question(username: str, security_answer: str, new_password: str) -> None:
    """Restablece el acceso respondiendo bien la pregunta de seguridad -
    genera una DEK NUEVA (key_generation += 1). Los datos cifrados con la
    DEK anterior quedan permanentemente inaccesibles a proposito: no se usa
    la respuesta como clave alternativa de descifrado (mas debil/adivinable
    que una contraseña real, ver ROADMAP.md punto 0)."""
    if len(new_password) < 8:
        raise UserError("La contraseña debe tener al menos 8 caracteres.")
    user = get_user(username)
    if not user or not user["security_answer_hash"]:
        raise UserError("No hay pregunta de seguridad configurada para ese usuario.")
    try:
        _password_hasher.verify(user["security_answer_hash"], security_answer.strip().lower())
    except VerifyMismatchError:
        raise UserError("Respuesta incorrecta.")

    salt = os.urandom(16)
    kek = _derive_kek(new_password, salt)
    dek = Fernet.generate_key()  # nueva DEK, no la anterior: datos viejos quedan huerfanos a proposito
    wrapped_dek = _fernet_from_raw_key(kek).encrypt(dek)
    password_hash = _password_hasher.hash(new_password)

    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ?, wrapped_dek = ?, "
            "key_generation = key_generation + 1 WHERE username = ?",
            (password_hash, salt, wrapped_dek, username),
        )
        conn.commit()
    log_security_event("password_reset_via_security_question", username)


def get_or_create_registration_key() -> str:
    """Clave que hay que conocer para poder registrar una cuenta nueva -
    mismo patron que la clave API en auth.py. El admin la consulta y se la
    da a quien quiera crear una cuenta."""
    if REGISTRATION_KEY_FILE.exists():
        existing = REGISTRATION_KEY_FILE.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    key = secrets.token_urlsafe(24)
    REGISTRATION_KEY_FILE.write_text(key, encoding="utf-8")
    return key


def log_security_event(event: str, username: str) -> None:
    """Aviso dentro de la app (panel de admin) en vez de email real -
    decision tomada con Sergio el 2026-09-23, ver ROADMAP.md punto 0."""
    entry = {"event": event, "username": username, "at": datetime.now(timezone.utc).isoformat()}
    with open(SECURITY_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def list_security_events(last_n: int = 100) -> list[dict]:
    if not SECURITY_LOG_FILE.exists():
        return []
    lines = SECURITY_LOG_FILE.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines[-last_n:]]


def delete_user(username: str) -> bool:
    """Borra la cuenta (no hace falta desenvolver su DEK para esto - borrar
    no requiere leer, es coherente con zero-knowledge). Quien llama es
    responsable de borrar tambien los datos asociados (mensajes, caras,
    documentos) en sus propios modulos."""
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()
        return cur.rowcount > 0
