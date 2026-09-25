import json
import time
from contextlib import contextmanager

from paths import DATA_DIR

LOG_PATH = DATA_DIR / "metrics.jsonl"
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)


def log_event(agent: str, latency_ms: float, verifier_gated: bool = False,
              error: str | None = None) -> None:
    event = {
        "ts": time.time(),
        "agent": agent,
        "latency_ms": round(latency_ms, 1),
        "verifier_gated": verifier_gated,
        "error": error,
    }
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


@contextmanager
def timed_event(agent_holder: list):
    """Uso: with timed_event(...) as finish: ... finish(verifier_gated=True)
    Se usa como context manager para no tener que calcular start/end a mano
    en cada sitio que genera una respuesta."""
    start = time.perf_counter()
    result = {"agent": None, "verifier_gated": False, "error": None}

    def finish(agent: str, verifier_gated: bool = False, error: str | None = None):
        result["agent"] = agent
        result["verifier_gated"] = verifier_gated
        result["error"] = error

    try:
        yield finish
    finally:
        elapsed_ms = (time.perf_counter() - start) * 1000
        if result["agent"]:
            log_event(result["agent"], elapsed_ms, result["verifier_gated"], result["error"])


def _read_events(last_n: int | None = 2000) -> list[dict]:
    if not LOG_PATH.exists():
        return []
    lines = LOG_PATH.read_text(encoding="utf-8").strip().splitlines()
    if last_n:
        lines = lines[-last_n:]
    events = []
    for line in lines:
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def summary(last_n: int = 2000) -> dict:
    events = _read_events(last_n)
    if not events:
        return {"total_requests": 0, "by_agent": {}, "errors": 0, "verifier_gated": 0}

    by_agent: dict[str, dict] = {}
    errors = 0
    gated = 0
    for e in events:
        agent = e.get("agent", "desconocido")
        bucket = by_agent.setdefault(agent, {"count": 0, "total_latency_ms": 0.0, "errors": 0, "gated": 0})
        bucket["count"] += 1
        bucket["total_latency_ms"] += e.get("latency_ms", 0)
        if e.get("error"):
            bucket["errors"] += 1
            errors += 1
        if e.get("verifier_gated"):
            bucket["gated"] += 1
            gated += 1

    for agent, bucket in by_agent.items():
        bucket["avg_latency_ms"] = round(bucket["total_latency_ms"] / bucket["count"], 1)
        del bucket["total_latency_ms"]

    return {
        "total_requests": len(events),
        "by_agent": by_agent,
        "errors": errors,
        "verifier_gated": gated,
        "window": f"ultimos {len(events)} eventos",
    }
