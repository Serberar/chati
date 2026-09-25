"""Planes de tareas visibles para conversaciones largas (equivalente al
TodoWrite de un agente de codigo) - estado en memoria del proceso, no es
memoria a largo plazo: un plan describe "lo que queda por hacer ahora" en
esta conversacion, no algo que deba sobrevivir a un reinicio del servidor.
Ver ROADMAP.md, punto 5."""

VALID_STATES = {"pendiente", "en_progreso", "hecho"}

_plans: dict[str, list[dict]] = {}


def set_plan(session_id: str, pasos: list[dict]) -> None:
    limpios = []
    for p in pasos:
        texto = str(p.get("texto", "")).strip()
        estado = p.get("estado", "pendiente")
        if estado not in VALID_STATES:
            estado = "pendiente"
        if texto:
            limpios.append({"texto": texto, "estado": estado})
    _plans[session_id] = limpios


def get_plan(session_id: str) -> list[dict]:
    return _plans.get(session_id, [])
