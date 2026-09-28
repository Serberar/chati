import sqlite3
import uuid
from contextlib import closing

import crypto_utils
from paths import DATA_DIR

DB_PATH = DATA_DIR / "memory.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Se muestra en vez del contenido real cuando un mensaje se cifro con una
# key_generation de DEK distinta a la actual de la sesion (tras un
# restablecimiento de contraseña via pregunta de seguridad, ver users.py) -
# zero-knowledge real: no hay forma de recuperar ese contenido, ni deberia
# haberla. Ver ROADMAP.md, punto 0.
ORPHANED_PLACEHOLDER = "[contenido no disponible - se cifro con una clave anterior a un restablecimiento de contraseña]"


def _connect():
    return sqlite3.connect(DB_PATH)


def _ensure_column(conn, table: str, column: str, coltype: str) -> None:
    cols = [row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def init_db():
    with closing(_connect()) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,          -- 'user' o 'assistant'
                content TEXT NOT NULL,
                agent TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        # migracion aditiva para bases de datos creadas antes del sistema de
        # usuarios - nunca borra ni renombra nada existente
        _ensure_column(conn, "messages", "user_id", "TEXT")
        _ensure_column(conn, "messages", "key_generation", "INTEGER")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_session ON messages(session_id, id)")
        # nombre opcional de una conversacion (ver ROADMAP.md) - cifrado igual
        # que el contenido de los mensajes, un titulo puede ser tan revelador
        # como el propio contenido ("Como hackear WiFi" vs "Recetas de pasta")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS session_titles (
                session_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                user_id TEXT,
                key_generation INTEGER,
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.commit()


def new_session_id() -> str:
    return uuid.uuid4().hex


def _decrypt_row_content(content: str, row_key_generation: int | None,
                          dek: bytes | None, current_key_generation: int | None) -> str:
    """Sin dek (llamador sin sesion de usuario autenticada, o mensaje
    guardado antes del sistema de usuarios): el contenido esta en texto
    plano, se devuelve tal cual - compatibilidad con lo ya existente."""
    if dek is None or row_key_generation is None:
        return content
    if row_key_generation != current_key_generation:
        return ORPHANED_PLACEHOLDER
    decrypted = crypto_utils.decrypt_text(dek, content.encode("ascii"))
    return decrypted if decrypted is not None else ORPHANED_PLACEHOLDER


def add_message(session_id: str, role: str, content: str, agent: str | None = None,
                 dek: bytes | None = None, key_generation: int | None = None,
                 user_id: str | None = None) -> None:
    stored_content = content
    stored_key_generation = None
    if dek is not None:
        stored_content = crypto_utils.encrypt_text(dek, content).decode("ascii")
        stored_key_generation = key_generation
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, agent, user_id, key_generation) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, role, stored_content, agent, user_id, stored_key_generation),
        )
        conn.commit()


def get_history(session_id: str, limit: int = 12,
                 dek: bytes | None = None, key_generation: int | None = None) -> list[dict]:
    """Devuelve los ultimos `limit` mensajes de la sesion, en orden cronologico."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, role, content, agent, created_at, key_generation "
            "FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [
        {"id": i, "role": r, "content": _decrypt_row_content(c, kg, dek, key_generation),
         "agent": a, "created_at": t}
        for i, r, c, a, t, kg in reversed(rows)
    ]


def update_message(message_id: int, content: str,
                    dek: bytes | None = None, key_generation: int | None = None) -> bool:
    stored_content = content
    stored_key_generation = None
    if dek is not None:
        stored_content = crypto_utils.encrypt_text(dek, content).decode("ascii")
        stored_key_generation = key_generation
    with closing(_connect()) as conn:
        if dek is not None:
            cur = conn.execute(
                "UPDATE messages SET content = ?, key_generation = ? WHERE id = ?",
                (stored_content, stored_key_generation, message_id),
            )
        else:
            cur = conn.execute("UPDATE messages SET content = ? WHERE id = ?", (stored_content, message_id))
        conn.commit()
        return cur.rowcount > 0


def delete_message(message_id: int) -> bool:
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM messages WHERE id = ?", (message_id,))
        conn.commit()
        return cur.rowcount > 0


def list_sessions(limit: int = 30, user_id: str | None = None,
                   dek: bytes | None = None, key_generation: int | None = None) -> list[dict]:
    """Sin user_id: todas las sesiones (legado, retrocompatible). Con
    user_id: solo las suyas - para que cada usuario vea solo su propio
    historial, no el de los demas. dek/key_generation: para descifrar el
    titulo si se puso uno (ver set_session_title) - sin dek, title siempre
    sale None, igual que el contenido de los mensajes."""
    query = (
        "SELECT m.session_id, MIN(m.created_at) as started, MAX(m.created_at) as last_used, COUNT(*) as n, "
        "t.title, t.key_generation "
        "FROM messages m LEFT JOIN session_titles t ON t.session_id = m.session_id"
    )
    params: tuple = ()
    if user_id:
        query += " WHERE m.user_id = ?"
        params = (user_id,)
    query += " GROUP BY m.session_id ORDER BY last_used DESC LIMIT ?"
    params = params + (limit,)
    with closing(_connect()) as conn:
        rows = conn.execute(query, params).fetchall()
    return [
        {
            "session_id": r[0], "started": r[1], "last_used": r[2], "messages": r[3],
            "title": _decrypt_row_content(r[4], r[5], dek, key_generation) if r[4] is not None else None,
        }
        for r in rows
    ]


def set_session_title(session_id: str, title: str, user_id: str | None = None,
                       dek: bytes | None = None, key_generation: int | None = None) -> None:
    stored_title = title
    stored_key_generation = None
    if dek is not None:
        stored_title = crypto_utils.encrypt_text(dek, title).decode("ascii")
        stored_key_generation = key_generation
    with closing(_connect()) as conn:
        conn.execute(
            "INSERT INTO session_titles (session_id, title, user_id, key_generation, updated_at) "
            "VALUES (?, ?, ?, ?, datetime('now')) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "title = excluded.title, key_generation = excluded.key_generation, updated_at = excluded.updated_at",
            (session_id, stored_title, user_id, stored_key_generation),
        )
        conn.commit()


def session_owner(session_id: str) -> str | None:
    """user_id dueño de la conversacion (None si no existe o es de invitado/legado)."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT user_id FROM messages WHERE session_id = ? AND user_id IS NOT NULL LIMIT 1",
                           (session_id,)).fetchone()
    return row[0] if row else None


def message_session(message_id: int) -> str | None:
    with closing(_connect()) as conn:
        row = conn.execute("SELECT session_id FROM messages WHERE id = ?", (message_id,)).fetchone()
    return row[0] if row else None


def clear_session(session_id: str) -> None:
    with closing(_connect()) as conn:
        conn.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
        conn.execute("DELETE FROM session_titles WHERE session_id = ?", (session_id,))
        conn.commit()


def delete_user_messages(user_id: str) -> int:
    """Borra todos los mensajes de un usuario - se usa cuando un admin borra
    la cuenta (ver ROADMAP.md, punto 0). No hace falta descifrar nada para
    borrar, coherente con zero-knowledge."""
    with closing(_connect()) as conn:
        cur = conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM session_titles WHERE user_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount


init_db()
