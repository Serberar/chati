"""Herramientas que el agente de texto puede invocar durante una conversacion,
en vez de recibir contexto inyectado a ciegas en cada turno. El modelo decide
cuando las necesita; asi se evita tanto el gasto de una busqueda RAG en cada
mensaje como, mas importante, que el modelo invente una fecha o un dato que
podria haber consultado."""

import datetime
import os
import subprocess
import sys
import tempfile
from pathlib import Path

DIAS = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]

PYTHON_EXEC_TIMEOUT = 10
PYTHON_OUTPUT_LIMIT = 4000

TOOL_DEFS = [
    {
        "type": "function",
        "function": {
            "name": "fecha_actual",
            "description": (
                "Devuelve la fecha y hora actual real de este ordenador. Usala "
                "siempre que necesites saber que dia, mes o año es, o calcular "
                "cuanto falta o ha pasado desde una fecha, en vez de adivinarlo."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "buscar_en_memoria",
            "description": (
                "Busca en la memoria a largo plazo de conversaciones anteriores y "
                "en los documentos que el usuario ha subido. Usala cuando la "
                "pregunta pueda depender de algo que el usuario te haya contado "
                "antes, o de un documento suyo, y no lo tengas ya en el historial "
                "reciente de este chat."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "consulta": {"type": "string", "description": "que buscar, en pocas palabras"},
                },
                "required": ["consulta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_documentos",
            "description": "Lista los nombres de los documentos que el usuario ha subido a la base de conocimiento.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_personas",
            "description": (
                "Lista los nombres de las personas guardadas en la galeria de caras "
                "(usadas para generar imagenes o videos con una cara real)."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delegar_a_agente_de_codigo",
            "description": (
                "Envia una tarea al agente de codigo (OpenCode), que puede leer y "
                "editar archivos reales del proyecto, ejecutar comandos, etc. "
                "Usala SOLO cuando el usuario pida explicitamente crear, modificar "
                "o revisar archivos de un proyecto real - nunca para explicar codigo "
                "o escribir un fragmento de ejemplo, para eso responde tu directamente. "
                "OJO: tarda varios minutos en completarse y no devuelve el resultado "
                "aqui mismo - dile al usuario que se la has pasado al agente y que "
                "vera su progreso justo debajo, no inventes que ya esta hecho."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tarea": {"type": "string", "description": "descripcion clara y completa de que hacer"},
                },
                "required": ["tarea"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "actualizar_plan",
            "description": (
                "Crea o actualiza la lista de pasos visible para el usuario, para "
                "tareas de mas de 2-3 pasos. Llamala con la lista COMPLETA "
                "actualizada cada vez (no solo el paso que cambia) para que el "
                "usuario vea el progreso real. No la uses para tareas triviales "
                "de un solo paso."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pasos": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "texto": {"type": "string"},
                                "estado": {"type": "string", "enum": ["pendiente", "en_progreso", "hecho"]},
                            },
                            "required": ["texto", "estado"],
                        },
                    },
                },
                "required": ["pasos"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "leer_archivo",
            "description": (
                "Lee el contenido de un archivo de texto real del usuario. Solo "
                "funciona dentro de su Escritorio o Documentos - si la ruta cae "
                "fuera, se rechaza. Usala cuando el usuario pida leer, resumir o "
                "analizar un archivo suyo concreto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ruta": {"type": "string", "description": "ruta completa del archivo"},
                },
                "required": ["ruta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "listar_carpeta",
            "description": (
                "Lista los archivos y subcarpetas de una carpeta del usuario "
                "(solo dentro de su Escritorio o Documentos). Usala para ver que "
                "hay antes de pedir leer_archivo, si no sabes el nombre exacto."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "ruta": {"type": "string", "description": "ruta completa de la carpeta"},
                },
                "required": ["ruta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ejecutar_python",
            "description": (
                "Ejecuta codigo Python de verdad para comprobar un calculo, operacion "
                "matematica, fecha o procesamiento de datos, en vez de calcularlo de "
                "memoria. Usala siempre que la respuesta dependa de un calculo numerico "
                "concreto (sumas, multiplicaciones, porcentajes, estadisticas, "
                "conversiones de unidades) para no arriesgarte a un error de calculo. "
                "El codigo debe usar print() para mostrar el resultado, si no no se ve nada. "
                "Solo biblioteca estandar (math, statistics, fractions, decimal, datetime, "
                "json, re...): sin numpy, sin red y sin archivos del ordenador."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "codigo": {"type": "string", "description": "codigo Python a ejecutar; usa print() para el resultado"},
                },
                "required": ["codigo"],
            },
        },
    },
]


def fecha_actual() -> str:
    now = datetime.datetime.now()
    return f"Hoy es {DIAS[now.weekday()]}, {now.day} de {MESES[now.month - 1]} de {now.year}. Hora: {now.strftime('%H:%M')}."


SANDBOX = Path(__file__).with_name("python_sandbox.py")


def ejecutar_python(codigo: str) -> str:
    """Ejecuta codigo Python en un proceso aparte con timeout, en una carpeta
    temporal y dentro de la jaula de python_sandbox.py: sin red, sin lanzar
    programas y sin tocar archivos fuera de esa carpeta. El modelo la llama sin
    pedir confirmacion y lee textos de fuera (documentos, memoria), asi que
    una instruccion escondida en un documento no puede usarla para hacer
    nada en el ordenador (auditoria 2026-09-29)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        script_path = Path(tmpdir) / "snippet.py"
        script_path.write_text(codigo, encoding="utf-8")
        # -I: sin variables de entorno ni site del usuario; -B: sin escribir .pyc fuera
        env = {k: v for k, v in os.environ.items() if k.upper() in ("SYSTEMROOT", "WINDIR", "PATH")}
        env.update(TEMP=tmpdir, TMP=tmpdir, PYTHONIOENCODING="utf-8")
        try:
            result = subprocess.run(
                [sys.executable, "-I", "-B", str(SANDBOX), tmpdir],
                cwd=tmpdir,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                timeout=PYTHON_EXEC_TIMEOUT,
            )
        except subprocess.TimeoutExpired:
            return f"El codigo tardo mas de {PYTHON_EXEC_TIMEOUT}s y se interrumpio (probable bucle infinito)."

        output = result.stdout
        if result.returncode != 0:
            output += f"\n--- error (codigo {result.returncode}) ---\n{result.stderr}"
        if not output.strip():
            output = "(el codigo se ejecuto sin errores pero no imprimio nada - usa print() para ver resultados)"
        if len(output) > PYTHON_OUTPUT_LIMIT:
            output = output[:PYTHON_OUTPUT_LIMIT] + "\n... (salida truncada)"
        return output
