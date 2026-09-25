"""Exporta el historial de conversaciones de un usuario a formato ChatML
(JSONL), listo para fine-tuning con Unsloth Desktop (ver ROADMAP.md, punto 9).

Solo puede usarse dentro de una sesion autenticada real, con la DEK del
usuario en memoria - respeta el diseno zero-knowledge (ver ROADMAP.md, punto
0): nadie sin la contraseña del usuario puede generar este export, ni
siquiera el admin. Decidir si entrenar con sus propias conversaciones es
eleccion del propio usuario, no algo que el sistema haga por su cuenta."""

import json
from pathlib import Path

import memory

# Formato ChatML ({"messages": [{"role": ..., "content": ...}]}) - es el que
# la documentacion oficial de Unsloth recomienda para datasets conversacionales
# (https://unsloth.ai/docs/get-started/fine-tuning-llms-guide/datasets-guide,
# consultado 2026-09-24), y coincide exactamente con los roles que ya usa
# memory.py ("user"/"assistant"), sin necesidad de traducir nada.


def export_user_conversations_chatml(user_id: str, dek: bytes, key_generation: int,
                                      out_path: Path, min_turns: int = 2) -> int:
    """Escribe un JSONL en out_path, una conversacion (sesion) por linea.
    Descarta sesiones con menos de min_turns mensajes utiles (evita ruido de
    "hola" sueltos sin continuacion) y cualquier mensaje que no se pudiera
    descifrar (contenido de una key_generation anterior a un reset de
    contraseña, ver ROADMAP.md punto 0). Devuelve cuantas conversaciones
    se exportaron."""
    sessions = memory.list_sessions(limit=100000, user_id=user_id)
    exported = 0
    with open(out_path, "w", encoding="utf-8") as f:
        for session in sessions:
            history = memory.get_history(session["session_id"], limit=10000,
                                          dek=dek, key_generation=key_generation)
            messages = [
                {"role": m["role"], "content": m["content"]}
                for m in history
                if m["role"] in ("user", "assistant") and m["content"]
                and m["content"] != memory.ORPHANED_PLACEHOLDER
            ]
            # una conversacion tiene que terminar en una respuesta del asistente para
            # servir como ejemplo de entrenamiento - un turno de usuario sin respuesta
            # (p.ej. si la generacion se interrumpio o fallo a mitad) no es un ejemplo
            # valido, se descarta antes de contar los turnos utiles
            while messages and messages[-1]["role"] == "user":
                messages.pop()
            if len(messages) < min_turns:
                continue
            f.write(json.dumps({"messages": messages}, ensure_ascii=False) + "\n")
            exported += 1
    return exported
