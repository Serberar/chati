"""Varios usuarios a la vez (auditoria 2026-09-30): sesiones, mensajes en
SQLite, imagenes cifradas y dueños de tareas desde muchos hilos. Busca
"database is locked", datos cruzados entre usuarios o escrituras perdidas."""

import threading

from cryptography.fernet import Fernet

import auth_sessions
import media_store
import memory
import task_owners

THREADS = 12
ROUNDS = 25


def _run(worker):
    errors = []

    def wrapped(n):
        try:
            worker(n)
        except Exception as exc:  # noqa: BLE001 - se informa abajo
            errors.append(repr(exc))

    threads = [threading.Thread(target=wrapped, args=(n,)) for n in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors[:3]


def test_many_users_writing_messages_at_once_keep_their_own_history():
    keys = {n: Fernet.generate_key() for n in range(THREADS)}
    sids = {n: memory.new_session_id() for n in range(THREADS)}

    def worker(n):
        for i in range(ROUNDS):
            memory.add_message(sids[n], "user", f"usuario {n} mensaje {i}", dek=keys[n], key_generation=1,
                               user_id=f"u{n}")

    _run(worker)
    for n in range(THREADS):
        history = memory.get_history(sids[n], limit=ROUNDS, dek=keys[n], key_generation=1)
        assert len(history) == ROUNDS
        assert all(h["content"].startswith(f"usuario {n} ") for h in history)
        assert memory.session_owner(sids[n]) == f"u{n}"


def test_sessions_and_media_from_many_threads():
    created = {}

    def worker(n):
        key = Fernet.generate_key()
        for i in range(ROUNDS):
            token = auth_sessions.create_guest_session()
            assert auth_sessions.get_session(token) is not None
            name = media_store.save(f"{n}-{i}".encode(), ".png", key)
            assert media_store.load(name, key) == f"{n}-{i}".encode()
            created[(n, i)] = token
            auth_sessions.destroy_session(token)

    _run(worker)
    assert len(created) == THREADS * ROUNDS
    assert all(auth_sessions.get_session(t) is None for t in created.values())


def test_task_owners_are_not_lost_under_concurrent_writes():
    def worker(n):
        for i in range(ROUNDS):
            task_owners.record(f"ses_{n}_{i}", f"u{n}")

    _run(worker)
    for n in range(THREADS):
        for i in range(ROUNDS):
            assert task_owners.owner(f"ses_{n}_{i}") == f"u{n}"
