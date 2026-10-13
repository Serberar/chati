"""Herramientas extra del agente de codigo (agent_tools.py) y su servidor MCP
(agent_mcp.py). Sin red: las consultas a PyPI/npm van simuladas."""
import json
from unittest.mock import MagicMock, patch

import agent_mcp
import agent_tools as t


def _use_tmp_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(t, "MEMORY_DIR", tmp_path / "memoria")
    monkeypatch.setattr(t, "MEMORY_INDEX", tmp_path / "memoria" / "MEMORY.md")


def test_memory_saves_indexes_updates_finds_and_deletes(tmp_path, monkeypatch):
    _use_tmp_memory(tmp_path, monkeypatch)
    assert "Guardado" in t.memory_save("Tests de Chati", "Se lanzan con pytest -m 'not live'.", "proyecto",
                                       "C:/AI", "Como lanzar los tests")
    index = t.memory_index()
    assert "tests-de-chati (proyecto) [C:/AI]: Como lanzar los tests" in index
    assert "Actualizado" in t.memory_save("Tests de Chati", "Ahora con -x.", "proyecto", "C:/AI")
    assert "Ahora con -x." in t.memory_read("Tests de Chati")
    assert "Ahora con -x." in t.memory_search("como se lanzan los tests")
    assert t.memory_search("cocina italiana") == "Nada en la memoria sobre eso."
    assert "Borrado" in t.memory_delete("tests-de-chati")
    assert "tests-de-chati" not in t.memory_index()


def test_memory_rejects_unknown_kinds_and_empty_content(tmp_path, monkeypatch):
    _use_tmp_memory(tmp_path, monkeypatch)
    assert "Tipo no valido" in t.memory_save("x", "algo", "secreto")
    assert t.memory_save("x", "   ") == "No hay nada que guardar."


def _response(status: int, payload: dict) -> MagicMock:
    resp = MagicMock(status_code=status, ok=status == 200)
    resp.json.return_value = payload
    return resp


def test_package_info_says_clearly_when_a_library_does_not_exist():
    with patch.object(t.requests, "get", return_value=_response(404, {})):
        assert t.package_info("pydantic-ultra-validator").startswith("NO EXISTE")
        assert t.package_info("left-padder-pro", "npm").startswith("NO EXISTE")
    info = {"info": {"name": "pydantic", "version": "3.1.0", "summary": "Data validation", "license": "MIT"},
            "releases": {"3.1.0": [{"upload_time": "2026-09-01T10:00:00"}]}}
    with patch.object(t.requests, "get", return_value=_response(200, info)):
        text = t.package_info("pydantic")
    assert "pydantic 3.1.0 (publicada 2026-09-01)" in text and "pip install pydantic" in text


def test_check_updates_compares_requirements_and_package_json(tmp_path):
    (tmp_path / "requirements.txt").write_text("fastapi==0.100.0\nrequests>=2.0  # comentario\nnoexiste==1.0\n",
                                               encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"dependencies": {"react": "^18.2.0"}}), encoding="utf-8")
    latest = {"fastapi": "0.143.0", "requests": "2.34.2", "react": "19.3.0", "noexiste": None}
    with patch.object(t, "_latest", side_effect=lambda name, eco: latest[name]):
        report = t.check_updates(str(tmp_path))
    assert "4 dependencias" in report
    assert "fastapi (python): pedida ==0.100.0 -> ultima 0.143.0" in report
    assert "react (npm): pedida ^18.2.0 -> ultima 19.3.0" in report
    assert "noexiste (python): no se encuentra" in report


def test_web_read_refuses_the_local_network():
    assert "no esta permitida" in t.web_read("http://127.0.0.1:8188/queue")
    assert "no esta permitida" in t.web_read("file:///C:/Windows/win.ini")


def test_read_repo_only_accepts_public_https_repositories():
    assert "Solo repositorios publicos" in t.read_repo("http://github.com/a/b")
    assert "Dame la direccion" in t.read_repo("https://github.com/solo-usuario")


def test_mcp_server_lists_and_calls_tools():
    init = agent_mcp.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["serverInfo"]["name"] == "chati"
    assert agent_mcp.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    names = [tool["name"] for tool in agent_mcp.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
             ["result"]["tools"]]
    assert {"memoria_guardar", "buscar_web", "info_paquete", "revisar_actualizaciones", "leer_repo"} <= set(names)
    with patch.object(t, "package_info", return_value="fastapi 1.0"):
        reply = agent_mcp.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                                  "params": {"name": "info_paquete", "arguments": {"nombre": "fastapi"}}})
    assert reply["result"] == {"content": [{"type": "text", "text": "fastapi 1.0"}], "isError": False}
    missing = agent_mcp.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                                "params": {"name": "info_paquete", "arguments": {}}})
    assert missing["result"]["isError"] and "nombre" in missing["result"]["content"][0]["text"]
    unknown = agent_mcp.handle({"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "borrar_todo"}})
    assert unknown["error"]["code"] == -32602


def test_the_mixed_code_model_moves_to_cpu_while_comfyui_generates():
    """qwen3-coder "-mixto" (12 capas en la GPU) pasa a "-cpu" si ComfyUI esta
    generando: en los 8 GB no caben los dos (2026-10-13)."""
    import llm_proxy
    body = json.dumps({"model": "qwen3-coder:30b-mixto", "messages": []}).encode()
    assert json.loads(llm_proxy.cpu_when_gpu_busy(body, True))["model"] == "qwen3-coder:30b-cpu"
    assert llm_proxy.cpu_when_gpu_busy(body, False) == body
    other = json.dumps({"model": "qwen3:8b"}).encode()
    assert llm_proxy.cpu_when_gpu_busy(other, True) == other


def test_code_questions_go_to_the_read_only_agent():
    """2026-10-13: con "¿que libreria me recomiendas?" el agente de construir
    se ponia a crear un proyecto. Las preguntas van al de planificar."""
    import main
    questions = ["Voy a empezar un proyecto en Python. ¿Que libreria me recomiendas? Responde en dos lineas.",
                 "como funciona el login de este proyecto", "Revisa si las dependencias estan al dia",
                 "explícame la carpeta src", "¿por que falla el test de pagos?"]
    orders = ["Crea un formulario de contacto", "arregla el test que falla", "Haz que valide antes, ¿vale?",
              "añade un endpoint /salud", "el login falla con usuarios nuevos"]
    assert all(main._is_code_question(q) for q in questions)
    assert not any(main._is_code_question(o) for o in orders)
