from unittest.mock import MagicMock, patch

import pytest
import requests

import opencode_client

BASE = "http://127.0.0.1:8901"


@pytest.fixture(autouse=True)
def _no_workdir(monkeypatch):
    # main.py la fija al importarse (lo hacen otros tests): aqui se prueba sin
    # ella salvo en los tests de la carpeta de trabajo
    monkeypatch.setattr(opencode_client, "WORKDIR", None)
    monkeypatch.setattr(opencode_client, "_session_dirs", {})


def _ok_response(json_data):
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = json_data
    return resp


@patch("opencode_client.requests.post")
def test_create_session_returns_id(mock_post):
    mock_post.return_value = _ok_response({"id": "ses_abc123"})

    session_id = opencode_client.create_session(BASE)

    assert session_id == "ses_abc123"
    mock_post.assert_called_once_with(f"{BASE}/session", json={}, timeout=opencode_client.TIMEOUT)


@patch("opencode_client.requests.post")
def test_send_prompt_async_sends_text_and_windows_context(mock_post):
    mock_post.return_value = _ok_response({})

    opencode_client.send_prompt_async(BASE, "ses_abc123", "arregla el bug X")

    body = mock_post.call_args.kwargs["json"]
    assert mock_post.call_args.args[0] == f"{BASE}/session/ses_abc123/prompt_async"
    assert body["parts"] == [{"type": "text", "text": "arregla el bug X"}]
    assert "Windows" in body["system"]
    assert "Escritorio" in body["system"]


@patch("opencode_client.send_prompt_async")
@patch("opencode_client.create_session")
def test_delegate_returns_text_and_task_id(mock_create, mock_send):
    mock_create.return_value = "ses_abc123"

    text, task_id = opencode_client.delegate(BASE, "arregla el bug X", "chati")

    mock_send.assert_called_once_with(BASE, "ses_abc123", "arregla el bug X", "chati", None)
    assert task_id == "ses_abc123"
    assert "agente" in text
    assert "http" not in text


@patch("opencode_client.create_session")
def test_delegate_reports_connection_failure_without_raising(mock_create):
    mock_create.side_effect = requests.ConnectionError("no se pudo conectar")

    text, task_id = opencode_client.delegate(BASE, "arregla el bug X")

    assert task_id is None
    assert "no se pudo contactar" in text.lower()


def _fake_get(responses):
    def fake(url, timeout):
        return _ok_response(responses[url.removeprefix(BASE)])
    return fake


@patch("opencode_client.requests.get")
def test_task_view_simplifies_steps_and_filters_pending_by_session(mock_get):
    mock_get.side_effect = _fake_get({
        "/session/ses_1": {"id": "ses_1", "title": "Crear archivo"},
        "/session/status": {"ses_1": {"type": "busy"}},
        "/session/ses_1/message": [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "crea hola.txt"}]},
            {"info": {"role": "assistant"}, "parts": [
                {"type": "step-start"},
                {"type": "text", "text": "Voy a crearlo."},
                {"type": "tool", "tool": "write", "state": {
                    "status": "completed", "title": "hola.txt",
                    "input": {"filePath": "C:\\Users\\x\\Desktop\\hola.txt"}}},
                {"type": "tool", "tool": "bash", "state": {
                    "status": "running", "input": {"command": "dir"}}},
                {"type": "tool", "tool": "question", "state": {"status": "running", "input": {}}},
            ]},
        ],
        "/question": [
            {"id": "que_1", "sessionID": "ses_1", "questions": [{"question": "¿Seguro?", "header": "Ok", "options": []}]},
            {"id": "que_2", "sessionID": "ses_otra", "questions": []},
        ],
        "/permission": [
            {"id": "per_1", "sessionID": "ses_1", "permission": "edit", "patterns": ["hola.txt"]},
            {"id": "per_2", "sessionID": "ses_otra", "permission": "bash", "patterns": ["rm *"]},
        ],
    })

    view = opencode_client.get_task_view(BASE, "ses_1")

    assert view["status"] == "busy"
    assert view["title"] == "Crear archivo"
    assert view["task"] == "crea hola.txt"
    assert view["steps"] == [
        {"kind": "text", "text": "Voy a crearlo."},
        {"kind": "tool", "tool": "write", "status": "completed", "title": "hola.txt",
         "detail": "C:\\Users\\x\\Desktop\\hola.txt", "error": None},
        {"kind": "tool", "tool": "bash", "status": "running", "title": "", "detail": "dir", "error": None,
         "output": "", "exit": None},
    ]
    assert [q["id"] for q in view["questions"]] == ["que_1"]
    assert view["permissions"] == [{"id": "per_1", "permission": "edit", "patterns": ["hola.txt"]}]


@patch("opencode_client.requests.get")
def test_task_view_idle_when_missing_from_status_and_surfaces_errors(mock_get):
    mock_get.side_effect = _fake_get({
        "/session/ses_1": {"id": "ses_1", "title": "x"},
        "/session/status": {},
        "/session/ses_1/message": [
            {"info": {"role": "assistant", "error": {"name": "APIError", "data": {"message": "Ollama caido"}}},
             "parts": []},
        ],
        "/question": [],
        "/permission": [],
    })

    view = opencode_client.get_task_view(BASE, "ses_1")

    assert view["status"] == "idle"
    assert view["error"] == "Ollama caido"


@patch("opencode_client.requests.get")
def test_list_tasks_newest_first_and_hides_subagent_sessions(mock_get):
    mock_get.side_effect = _fake_get({
        "/session": [
            {"id": "ses_old", "title": "vieja", "time": {"updated": 1}},
            {"id": "ses_new", "title": "nueva", "time": {"updated": 5}},
            {"id": "ses_child", "title": "sub", "parentID": "ses_new", "time": {"updated": 9}},
        ],
        "/session/status": {"ses_new": {"type": "busy"}},
    })

    tasks = opencode_client.list_tasks(BASE)

    assert [t["session_id"] for t in tasks] == ["ses_new", "ses_old"]
    assert tasks[0]["status"] == "busy"
    assert tasks[1]["status"] == "idle"


@patch("opencode_client.requests.post")
def test_replies_hit_the_right_endpoints(mock_post):
    mock_post.return_value = _ok_response({})

    opencode_client.reply_question(BASE, "que_1", [["Si"]])
    opencode_client.reply_permission(BASE, "per_1", "once")
    opencode_client.abort(BASE, "ses_1")

    # solo las de OpenCode: el parche de requests.post es global y en la bateria
    # completa algun hilo de otro test (p.ej. descargar un modelo de Ollama) se cuela
    calls = [(c.args[0], c.kwargs["json"]) for c in mock_post.call_args_list if c.args[0].startswith(BASE)]
    assert calls == [
        (f"{BASE}/question/que_1/reply", {"answers": [["Si"]]}),
        (f"{BASE}/permission/per_1/reply", {"reply": "once"}),
        (f"{BASE}/session/ses_1/abort", {}),
    ]


@patch("opencode_client.requests.get")
def test_busy_session_ids_ignores_tasks_waiting_for_the_user_and_silence_when_down(mock_get):
    mock_get.side_effect = _fake_get({
        "/session/status": {"ses_a": {"type": "busy"}, "ses_b": {"type": "idle"},
                            "ses_pregunta": {"type": "busy"}, "ses_permiso": {"type": "busy"}},
        "/question": [{"id": "que_1", "sessionID": "ses_pregunta"}],
        "/permission": [{"id": "per_1", "sessionID": "ses_permiso"}],
    })
    assert opencode_client.busy_session_ids(BASE) == ["ses_a"]
    mock_get.side_effect = requests.ConnectionError("caido")
    assert opencode_client.busy_session_ids(BASE) == []


@patch("opencode_client.requests.post")
@patch("opencode_client.requests.get")
def test_stop_task_rejects_what_is_pending_and_aborts(mock_get, mock_post):
    mock_get.side_effect = _fake_get({
        "/session/ses_1": {"directory": None},
        "/question": [{"id": "que_1", "sessionID": "ses_1"}, {"id": "que_x", "sessionID": "otra"}],
        "/permission": [{"id": "per_1", "sessionID": "ses_1"}],
    })
    mock_post.return_value = _ok_response({})

    opencode_client.stop_task(BASE, "ses_1")

    urls = [c.args[0] for c in mock_post.call_args_list]
    assert urls == [f"{BASE}/question/que_1/reject", f"{BASE}/permission/per_1/reply", f"{BASE}/session/ses_1/abort"]


@patch("opencode_client.requests.get")
def test_task_summary_uses_what_the_user_asked_when_there_is_no_title(mock_get):
    mock_get.side_effect = _fake_get({
        "/session/ses_1": {"title": "New session - 2026-09-25T07:55:13.453Z"},
        "/session/ses_1/message": [{"info": {"role": "user"}, "parts": [{"type": "text", "text": "crea hello.txt"}]}],
    })
    assert opencode_client.task_summary(BASE, "ses_1") == "crea hello.txt"


@patch("opencode_client.requests.post")
def test_send_prompt_async_picks_the_agent_only_when_given(mock_post):
    mock_post.return_value = _ok_response({})
    opencode_client.send_prompt_async(BASE, "ses_1", "x", "chati")
    opencode_client.send_prompt_async(BASE, "ses_1", "x")
    bodies = [c.kwargs["json"] for c in mock_post.call_args_list]
    assert bodies[0]["agent"] == "chati"
    assert "agent" not in bodies[1]


@patch("opencode_client.requests.post")
@patch("opencode_client.requests.get")
def test_continue_task_keeps_the_agent_the_task_started_with(mock_get, mock_post, monkeypatch):
    mock_get.side_effect = _fake_get({"/session/ses_1": {"id": "ses_1", "agent": "chati-potente"}})
    mock_post.return_value = _ok_response({})
    freed = []
    monkeypatch.setattr(opencode_client, "before_task", lambda agent, model=None: freed.append(agent))

    opencode_client.continue_task(BASE, "ses_1", "ponlo en la carpeta Trabajo")

    body = mock_post.call_args.kwargs["json"]
    assert mock_post.call_args.args[0] == f"{BASE}/session/ses_1/prompt_async"
    assert body["agent"] == "chati-potente"
    assert body["parts"] == [{"type": "text", "text": "ponlo en la carpeta Trabajo"}]
    assert freed == ["chati-potente"]


def test_follow_up_messages_show_as_user_steps_but_not_the_original_task():
    steps, _ = opencode_client._simplify_parts([
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "crea hola.txt"}]},
        {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Hecho."}]},
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "ahora en la carpeta Trabajo"}]},
        {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Movido."}]},
    ])
    assert steps == [{"kind": "text", "text": "Hecho."},
                     {"kind": "user", "text": "ahora en la carpeta Trabajo"},
                     {"kind": "text", "text": "Movido."}]


def test_a_model_stuck_in_a_loop_is_reported_instead_of_done():
    _, error = opencode_client._simplify_parts([
        {"info": {"role": "user"}, "parts": [{"type": "text", "text": "x"}]},
        {"info": {"role": "assistant", "finish": "length"}, "parts": [{"type": "step-start"}, {"type": "step-finish"}]},
    ])
    assert "atascado" in error
    _, error = opencode_client._simplify_parts([
        {"info": {"role": "assistant", "finish": "length"}, "parts": [{"type": "text", "text": "respuesta larga..."}]},
    ])
    assert error is None


def _bash_part(state):
    return {"info": {"role": "assistant"}, "parts": [{"type": "tool", "tool": "bash", "state": state}]}


def test_bash_steps_carry_the_live_output_while_running_and_when_done():
    running = {"status": "running", "input": {"command": "dir"},
               "metadata": {"output": "\x1b[32mlinea 1\x1b[0m\r\nlinea 2\r\n"}}
    steps, _ = opencode_client._simplify_parts([_bash_part(running)])
    assert steps[0]["output"] == "linea 1\nlinea 2\n" and steps[0]["exit"] is None

    done = {"status": "completed", "input": {"command": "dir"}, "output": "fin\r\n",
            "metadata": {"output": "fin\r\n", "exit": 1}}
    steps, _ = opencode_client._simplify_parts([_bash_part(done)])
    assert steps[0]["output"] == "fin\n" and steps[0]["exit"] == 1


def test_long_bash_output_keeps_only_the_end():
    state = {"status": "running", "input": {"command": "x"},
             "metadata": {"output": "a" * 10000 + "ULTIMA"}}
    steps, _ = opencode_client._simplify_parts([_bash_part(state)])
    out = steps[0]["output"]
    assert out.endswith("ULTIMA") and out.startswith("…") and len(out) <= opencode_client.TERMINAL_MAX_CHARS + 2


def test_non_bash_tools_have_no_terminal_output():
    steps, _ = opencode_client._simplify_parts([
        {"info": {"role": "assistant"}, "parts": [{"type": "tool", "tool": "read",
                                                   "state": {"status": "completed", "input": {"filePath": "a.txt"},
                                                             "output": "contenido"}}]}])
    assert "output" not in steps[0]


def test_windows_paths_are_sent_with_forward_slashes():
    text = opencode_client.forward_slash_paths(r'crea "C:\Users\Ana Lopez\Desktop\a.txt" y mueve D:\fotos\x.jpg a C:\tmp')
    assert text == 'crea "C:/Users/Ana Lopez/Desktop/a.txt" y mueve D:/fotos/x.jpg a C:/tmp'
    assert opencode_client.forward_slash_paths(r"usa \d en un regex") == r"usa \d en un regex"  # sin unidad: intacto
    assert "\\" not in opencode_client.SYSTEM_CONTEXT


# --- carpeta de trabajo / ver cambios / deshacer (roadmap n.º 6) ---

@patch("opencode_client.requests.post")
def test_with_a_workdir_every_call_goes_to_that_folder_and_it_becomes_a_git_repo(mock_post, tmp_path, monkeypatch):
    work = tmp_path / "Chati"
    monkeypatch.setattr(opencode_client, "WORKDIR", work)
    mock_post.return_value = _ok_response({"id": "ses_1"})
    opencode_client.create_session(BASE)
    assert mock_post.call_args.kwargs["params"] == {"directory": str(work)}
    assert (work / ".git").exists() and opencode_client.workdir_undoable()


@patch("opencode_client.requests.post")
def test_the_agent_is_told_its_workdir(mock_post, tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_client, "WORKDIR", tmp_path / "Chati")
    mock_post.return_value = _ok_response({})
    opencode_client.send_prompt_async(BASE, "ses_1", "crea hola.txt")
    assert (tmp_path / "Chati").as_posix() in mock_post.call_args.kwargs["json"]["system"]


def test_without_git_nothing_is_undoable(tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_client, "WORKDIR", tmp_path)
    assert not opencode_client.workdir_undoable()
    monkeypatch.setattr(opencode_client, "WORKDIR", None)
    assert not opencode_client.workdir_undoable()


_DIFFED_MESSAGES = [
    {"info": {"role": "user", "id": "msg_1", "summary": {"diffs": [
        {"file": "nuevo.txt", "status": "added", "additions": 1, "deletions": 0, "patch": "+hola"},
        {"file": "a.txt", "status": "modified", "additions": 2, "deletions": 1, "patch": "-x\n+y\n+z"}]}},
     "parts": [{"type": "text", "text": "crea nuevo.txt y cambia a.txt"}]},
    {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "Hecho."}]},
    {"info": {"role": "user", "id": "msg_2", "summary": {"diffs": [
        {"file": "a.txt", "status": "modified", "additions": 1, "deletions": 0, "patch": "+w"}]}},
     "parts": [{"type": "text", "text": "y otra linea"}]},
]


def test_changes_summary_counts_distinct_files_and_lines():
    assert opencode_client._changes_summary(_DIFFED_MESSAGES) == {"files": 2, "additions": 4, "deletions": 1}


@patch("opencode_client._get")
def test_get_changes_lists_the_diffs_of_each_turn(mock_get):
    mock_get.side_effect = lambda base, path, **kw: _DIFFED_MESSAGES if path.endswith("/message") else {"revert": None}
    info = opencode_client.get_changes(BASE, "ses_1")
    assert [t["message_id"] for t in info["turns"]] == ["msg_1", "msg_2"]
    assert info["turns"][0]["files"][0] == {"file": "nuevo.txt", "status": "added", "additions": 1,
                                            "deletions": 0, "patch": "+hola"}
    assert info["reverted"] is False


@patch("opencode_client.requests.post")
@patch("opencode_client._get")
def test_revert_undoes_from_the_first_message_of_the_task(mock_get, mock_post):
    mock_get.return_value = _DIFFED_MESSAGES
    mock_post.return_value = _ok_response({})
    opencode_client.revert_task(BASE, "ses_1")
    assert mock_post.call_args.args[0] == f"{BASE}/session/ses_1/revert"
    assert mock_post.call_args.kwargs["json"] == {"messageID": "msg_1"}


# --- modo Codigo: tareas en la carpeta de un proyecto ---

@patch("opencode_client.requests.post")
def test_a_project_task_goes_to_its_folder_and_is_told_so(mock_post, tmp_path, monkeypatch):
    monkeypatch.setattr(opencode_client, "WORKDIR", tmp_path / "Chati")
    project = str(tmp_path / "mi-proyecto")
    mock_post.return_value = _ok_response({"id": "ses_p"})
    opencode_client.start_task(BASE, "revisa la estructura", "chati-code", directory=project)
    create, prompt = mock_post.call_args_list
    assert create.kwargs["params"] == {"directory": project}
    assert prompt.kwargs["params"] == {"directory": project}
    assert "proyecto de la carpeta" in prompt.kwargs["json"]["system"]
    assert project in opencode_client.known_dirs()
    assert not (tmp_path / "mi-proyecto" / ".git").exists()  # el proyecto del usuario no se toca


@patch("opencode_client.requests.post")
def test_replies_go_to_the_folder_that_has_the_request(mock_post, monkeypatch):
    monkeypatch.setattr(opencode_client, "_session_dirs", {"ses_p": "C:/proyecto"})
    not_found, ok = MagicMock(status_code=404), _ok_response({})
    ok.status_code = 200
    mock_post.side_effect = [not_found, ok]
    opencode_client.reply_permission(BASE, "per_9", "once")
    assert [c.kwargs.get("params") for c in mock_post.call_args_list] == [None, {"directory": "C:/proyecto"}]
