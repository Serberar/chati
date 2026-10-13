"""Servidor MCP (por stdin/stdout) con las herramientas extra del agente de
codigo: memoria, internet, librerias, actualizaciones y repositorios (la
logica esta en agent_tools.py). Lo arranca OpenCode (ver "mcp" en
opencode.json) y sus herramientas le salen como chati_<nombre>.

Protocolo MCP minimo (JSON-RPC 2.0, un mensaje por linea): initialize,
tools/list y tools/call. Sin librerias extra. Por stdout solo va el
protocolo; cualquier aviso, por stderr."""
import json
import sys
import traceback

import agent_tools as t

PROTOCOL = "2024-11-05"


def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required}


_STR = {"type": "string"}
TOOLS = [
    {"name": "memoria_guardar", "fn": lambda a: t.memory_save(a["titulo"], a["contenido"], a.get("tipo", "proyecto"),
                                                              a.get("proyecto", ""), a.get("resumen", "")),
     "description": "Guarda algo que convenga recordar en futuras conversaciones: preferencias del usuario, "
                    "como se compila o se prueba un proyecto, decisiones tomadas, errores ya resueltos y su causa. "
                    "Si ya hay un recuerdo con ese titulo, lo actualiza. No guardes lo que ya esta en el codigo o "
                    "en git, ni secretos (contraseñas, claves).",
     "inputSchema": _schema({"titulo": _STR, "contenido": _STR,
                             "tipo": {"type": "string", "enum": list(t.MEMORY_TYPES)},
                             "proyecto": {"type": "string", "description": "Carpeta del proyecto, si es de uno"},
                             "resumen": {"type": "string", "description": "Una linea para el indice"}},
                            ["titulo", "contenido"])},
    {"name": "memoria_leer", "fn": lambda a: t.memory_read(a["nombre"]),
     "description": "Lee entero un recuerdo del indice de memoria (por su nombre).",
     "inputSchema": _schema({"nombre": _STR}, ["nombre"])},
    {"name": "memoria_buscar", "fn": lambda a: t.memory_search(a["texto"]),
     "description": "Busca en la memoria recuerdos sobre un tema.",
     "inputSchema": _schema({"texto": _STR}, ["texto"])},
    {"name": "memoria_borrar", "fn": lambda a: t.memory_delete(a["nombre"]),
     "description": "Borra un recuerdo que ya no es cierto o que el usuario pide olvidar.",
     "inputSchema": _schema({"nombre": _STR}, ["nombre"])},
    {"name": "buscar_web", "fn": lambda a: t.web_search(a["consulta"], int(a.get("cantidad", 8))),
     "description": "Busca en internet (documentacion, errores, ejemplos, novedades). Devuelve titulo, direccion "
                    "y resumen de cada resultado; para leer uno, usa leer_web.",
     "inputSchema": _schema({"consulta": _STR, "cantidad": {"type": "integer"}}, ["consulta"])},
    {"name": "leer_web", "fn": lambda a: t.web_read(a["url"], int(a.get("desde", 0))),
     "description": "Lee el texto de una pagina publica de internet (documentacion, issues, articulos). Si es "
                    "larga, dice como pedir el trozo siguiente.",
     "inputSchema": _schema({"url": _STR, "desde": {"type": "integer"}}, ["url"])},
    {"name": "info_paquete", "fn": lambda a: t.package_info(a["nombre"], a.get("ecosistema", "python")),
     "description": "Comprueba si existe una libreria (PyPI o npm) y da su ultima version, fecha, descripcion y "
                    "licencia. Usala SIEMPRE antes de proponer o instalar una libreria que no este ya en el "
                    "proyecto: no te inventes librerias.",
     "inputSchema": _schema({"nombre": _STR, "ecosistema": {"type": "string", "enum": ["python", "npm"]}},
                            ["nombre"])},
    {"name": "revisar_actualizaciones", "fn": lambda a: t.check_updates(a["carpeta"]),
     "description": "Compara las dependencias de un proyecto (requirements*.txt, pyproject.toml, package.json) con "
                    "la ultima version publicada de cada una.",
     "inputSchema": _schema({"carpeta": _STR}, ["carpeta"])},
    {"name": "buscar_codigo", "fn": lambda a: t.code_search(a["carpeta"], a["consulta"], int(a.get("cantidad", 6))),
     "description": "Busca en el codigo de un proyecto por SIGNIFICADO, no por palabras exactas (\"donde se valida "
                    "el login\", \"que calcula los descuentos\"). Devuelve archivo, lineas y el principio de cada "
                    "trozo. Usala para encontrar donde esta algo antes de abrir archivos; para un nombre exacto, grep "
                    "es mejor. La primera vez en un proyecto grande tarda unos minutos en prepararse.",
     "inputSchema": _schema({"carpeta": _STR, "consulta": _STR, "cantidad": {"type": "integer"}},
                            ["carpeta", "consulta"])},
    {"name": "ver_web", "fn": lambda a: t.look_at_local_page(a["url"], a.get("pregunta", "")),
     "description": "Abre en un navegador la web que estas desarrollando (solo de este ordenador: "
                    "http://localhost:PUERTO), hace una captura y te dice lo que se ve y los errores de la consola de "
                    "JavaScript. Usala para comprobar como queda una pagina o por que falla. El servidor de desarrollo "
                    "tiene que estar arrancado. Tarda 1-2 minutos.",
     "inputSchema": _schema({"url": _STR, "pregunta": {"type": "string", "description": "Algo concreto que mirar"}},
                            ["url"])},
    {"name": "ver_imagen", "fn": lambda a: t.look_at_image(a["ruta"], a.get("pregunta", "")),
     "description": "Describe una imagen o captura de pantalla (la ruta de un archivo), por ejemplo una que te pase "
                    "el usuario para enseñarte un error. Tarda 1-2 minutos.",
     "inputSchema": _schema({"ruta": _STR, "pregunta": {"type": "string"}}, ["ruta"])},
    {"name": "leer_repo", "fn": lambda a: t.read_repo(a["url"]),
     "description": "Descarga (o actualiza) un repositorio git publico, por ejemplo de GitHub, y dice en que "
                    "carpeta queda, sus carpetas y el README. Despues se lee con read, grep y glob.",
     "inputSchema": _schema({"url": _STR}, ["url"])},
    {"name": "ver_nombre", "fn": lambda a: t.find_symbol(a["carpeta"], a["viejo"]),
     "description": "Antes de renombrar: dice en que archivos y lineas aparece un nombre (funcion, clase, variable) "
                    "como palabra completa en todo el proyecto. No cambia nada.",
     "inputSchema": _schema({"carpeta": _STR, "viejo": _STR}, ["carpeta", "viejo"])},
    {"name": "renombrar", "fn": lambda a: t.rename_symbol(a["carpeta"], a["viejo"], a["nuevo"], apply=True),
     "description": "Renombra una funcion, clase o variable en TODO el proyecto de una vez (solo palabras completas, "
                    "sin tocar venv ni node_modules). Mira antes con ver_nombre. Usala en vez de bash o de editar "
                    "archivo por archivo.",
     "inputSchema": _schema({"carpeta": _STR, "viejo": _STR, "nuevo": _STR}, ["carpeta", "viejo", "nuevo"])},
    {"name": "lanzar_programa", "fn": lambda a: t.start_program(a["carpeta"], a["comando"], int(a.get("espera", 8))),
     "description": "Arranca un programa que se queda abierto (un servidor web, una app, npm run dev...) en la carpeta "
                    "del proyecto, SIN esperar a que termine, y devuelve lo que escribe en los primeros segundos. "
                    "Para comandos que acaban solos usa bash. Paralo con parar_programa al terminar.",
     "inputSchema": _schema({"carpeta": _STR, "comando": _STR,
                             "espera": {"type": "integer", "description": "Segundos a esperar antes de leer (8)"}},
                            ["carpeta", "comando"])},
    {"name": "ver_salida", "fn": lambda a: t.program_output(a["id"], int(a.get("lineas", 80))),
     "description": "Lo ultimo que ha escrito un programa arrancado con lanzar_programa (errores incluidos) y si "
                    "sigue en marcha.",
     "inputSchema": _schema({"id": _STR, "lineas": {"type": "integer"}}, ["id"])},
    {"name": "parar_programa", "fn": lambda a: t.stop_program(a["id"]),
     "description": "Para un programa arrancado con lanzar_programa.",
     "inputSchema": _schema({"id": _STR}, ["id"])},
]
_BY_NAME = {tool["name"]: tool for tool in TOOLS}


def handle(msg: dict) -> dict | None:
    """Respuesta a un mensaje (None para las notificaciones, que no la llevan)."""
    method, msg_id = msg.get("method"), msg.get("id")
    if msg_id is None:
        return None
    if method == "initialize":
        result = {"protocolVersion": PROTOCOL, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "chati", "version": "1.0"}}
    elif method == "tools/list":
        result = {"tools": [{k: v for k, v in tool.items() if k != "fn"} for tool in TOOLS]}
    elif method == "tools/call":
        params = msg.get("params") or {}
        tool = _BY_NAME.get(params.get("name"))
        if tool is None:
            return {"jsonrpc": "2.0", "id": msg_id,
                    "error": {"code": -32602, "message": f"Herramienta desconocida: {params.get('name')}"}}
        try:
            text, failed = tool["fn"](params.get("arguments") or {}), False
        except KeyError as exc:
            text, failed = f"Falta el parametro {exc}.", True
        except Exception as exc:  # noqa: BLE001 - el error se le cuenta al modelo
            traceback.print_exc(file=sys.stderr)
            text, failed = f"Error: {exc}", True
        result = {"content": [{"type": "text", "text": text}], "isError": failed}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Metodo no soportado: {method}"}}
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            reply = handle(json.loads(line))
        except json.JSONDecodeError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "JSON no valido"}}
        if reply is not None:
            sys.stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    finally:  # al cerrarse OpenCode, no dejar servidores del agente sueltos
        t.stop_all_programs()
