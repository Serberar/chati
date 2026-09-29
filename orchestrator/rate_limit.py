"""Limite de intentos fallidos (auditoria 2026-09-29): el login, la
recuperacion por pregunta de seguridad y la clave de registro se podian
probar sin fin. La recuperacion, ademas, borra los datos del usuario (clave
nueva), asi que adivinar la respuesta de otro era destruir su historial.

En memoria del proceso: al reiniciar se olvida, que es aceptable (el ataque
necesita miles de intentos, y reiniciar cuesta ~10 s)."""

import threading
import time
from collections import deque


class Limiter:
    def __init__(self, max_failures: int, window_seconds: float, lock_seconds: float):
        self.max_failures = max_failures
        self.window = window_seconds
        self.lock_seconds = lock_seconds
        self._failures: dict[str, deque] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int:
        """Segundos que faltan para poder volver a intentarlo (0 = puede)."""
        with self._lock:
            until = self._locked_until.get(key, 0)
            if until <= time.time():
                self._locked_until.pop(key, None)
                return 0
            return int(until - time.time()) + 1

    def fail(self, key: str) -> None:
        now = time.time()
        with self._lock:
            q = self._failures.setdefault(key, deque())
            q.append(now)
            while q and q[0] < now - self.window:
                q.popleft()
            if len(q) >= self.max_failures:
                self._locked_until[key] = now + self.lock_seconds
                q.clear()

    def succeed(self, key: str) -> None:
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)


def wait_message(seconds: int) -> str:
    minutes = max(1, round(seconds / 60))
    return f"Demasiados intentos fallidos. Espera {minutes} min antes de volver a probar."


login = Limiter(max_failures=5, window_seconds=15 * 60, lock_seconds=15 * 60)
password_reset = Limiter(max_failures=3, window_seconds=60 * 60, lock_seconds=60 * 60)
registration = Limiter(max_failures=5, window_seconds=15 * 60, lock_seconds=30 * 60)
