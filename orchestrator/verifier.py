"""
Pipeline anti-alucinacion (ver manual.md).

Dos modos:
- Sin evidencia (todavia no hay RAG relevante para la pregunta): auto-evaluacion
  en dos pasadas, el propio modelo juzga si la respuesta contiene afirmaciones
  factuales de las que no puede estar razonablemente seguro. No es una garantia
  de veracidad, reduce alucinaciones obvias y fuerza incertidumbre explicita.
- Con evidencia (RAG encontro documentos relevantes): el verificador comprueba
  si la respuesta esta realmente respaldada por los fragmentos recuperados, que
  es una comprobacion mas fuerte que la auto-confianza. Esta es la rama
  "Respuesta + fuentes/evidencia" del diagrama del manual.
"""

from dataclasses import dataclass

from agents.ollama_client import OllamaClient

VERIFY_PROMPT_SELF = """Vas a auditar una respuesta, no a mejorarla ni repetirla.

Pregunta original: "{query}"

Respuesta a auditar:
---
{answer}
---

Basandote UNICAMENTE en tu propio conocimiento (no tienes acceso a ninguna
fuente externa ahora mismo), ¿contiene esta respuesta afirmaciones factuales
especificas (fechas, cifras, nombres, eventos) de las que NO puedes estar
razonablemente seguro?

Responde en dos lineas exactas:
VEREDICTO: SEGURO o INSEGURO
RAZON: <una frase breve>
"""

VERIFY_PROMPT_EVIDENCE = """Vas a auditar si una respuesta esta respaldada por
la evidencia recuperada, no a mejorarla ni repetirla.

Pregunta original: "{query}"

Evidencia recuperada de la base de conocimiento:
---
{evidence}
---

Respuesta a auditar:
---
{answer}
---

¿Esta el contenido factual de la respuesta realmente respaldado por la
evidencia de arriba? Si la respuesta afirma algo que NO aparece en la
evidencia, es INSEGURO.

Responde en dos lineas exactas:
VEREDICTO: SEGURO o INSEGURO
RAZON: <una frase breve>
"""

NO_SE_RESPONSE = (
    "No tengo suficiente informacion verificable para responder a esto con "
    "garantias. Prefiero decirlo en vez de arriesgarme a inventar un dato. "
    "(Motivo del verificador: {reason})"
)


@dataclass
class VerificationResult:
    answer: str
    gated: bool
    reason: str | None = None
    sources: list[str] | None = None


class Verifier:
    def __init__(self, client: OllamaClient, model: str, strict: bool = True):
        self.client = client
        self.model = model
        self.strict = strict

    def _parse_verdict(self, raw: str) -> tuple[bool, str]:
        insecure = "INSEGURO" in raw.upper()
        reason_line = next((l for l in raw.splitlines() if l.upper().startswith("RAZON")), "")
        reason = reason_line.split(":", 1)[-1].strip() if reason_line else "sin detalle"
        return insecure, reason

    def check(self, query: str, answer: str, is_factual: bool,
              evidence: list[dict] | None = None) -> VerificationResult:
        if evidence:
            evidence_text = "\n\n".join(f"[{e['source']}] {e['text']}" for e in evidence)
            raw = self.client.chat(
                self.model,
                [{"role": "user", "content": VERIFY_PROMPT_EVIDENCE.format(
                    query=query, evidence=evidence_text, answer=answer)}],
                temperature=0.0,
            )
            insecure, reason = self._parse_verdict(raw)
            sources = sorted(set(e["source"] for e in evidence))

            if insecure and self.strict:
                return VerificationResult(
                    answer=NO_SE_RESPONSE.format(reason=reason), gated=True, reason=reason
                )
            if insecure:
                return VerificationResult(
                    answer=f"{answer}\n\n[Aviso del verificador: no respaldado por la evidencia - {reason}]",
                    gated=False, reason=reason,
                )
            return VerificationResult(answer=answer, gated=False, sources=sources)

        if not is_factual:
            return VerificationResult(answer=answer, gated=False)

        raw = self.client.chat(
            self.model,
            [{"role": "user", "content": VERIFY_PROMPT_SELF.format(query=query, answer=answer)}],
            temperature=0.0,
        )
        insecure, reason = self._parse_verdict(raw)

        if insecure and self.strict:
            return VerificationResult(
                answer=NO_SE_RESPONSE.format(reason=reason), gated=True, reason=reason
            )
        if insecure:
            return VerificationResult(
                answer=f"{answer}\n\n[Aviso del verificador: posible imprecision factual - {reason}]",
                gated=False, reason=reason,
            )
        return VerificationResult(answer=answer, gated=False)
