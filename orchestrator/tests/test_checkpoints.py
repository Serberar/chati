"""Copias del proyecto para el boton Deshacer del modo Codigo (checkpoints.py)."""
from unittest.mock import patch

import checkpoints


def test_undo_restores_edits_and_removes_new_files(tmp_path):
    project = tmp_path / "proyecto"
    (project / "venv").mkdir(parents=True)
    (project / "app.py").write_text("print('hola')\n", encoding="utf-8")
    (project / "borrame.txt").write_text("x", encoding="utf-8")
    (project / "venv" / "lib.py").write_text("no se toca", encoding="utf-8")
    with patch.object(checkpoints, "COPIES_DIR", tmp_path / "copias"), \
         patch.object(checkpoints, "SESSIONS_FILE", tmp_path / "copias" / "s.json"):
        assert checkpoints.undo("ses") == {"ok": False, "detail": "No hay nada que deshacer en esta tarea."}
        assert checkpoints.snapshot("ses", project, "cambia el saludo")
        assert checkpoints.available("ses") == 1
        # el agente edita, crea y borra
        (project / "app.py").write_text("print('adios')\n", encoding="utf-8")
        (project / "nuevo.py").write_text("x = 1\n", encoding="utf-8")
        (project / "borrame.txt").unlink()
        (project / "venv" / "otro.py").write_text("tampoco", encoding="utf-8")
        result = checkpoints.undo("ses")
        assert result["ok"] and result["quedan"] == 0
        assert (project / "app.py").read_text(encoding="utf-8") == "print('hola')\n"
        assert not (project / "nuevo.py").exists() and (project / "borrame.txt").exists()
        assert (project / "venv" / "otro.py").exists()  # lo ignorado no se toca
        assert sorted(result["archivos"]) == ["borrado nuevo.py", "recuperado borrame.txt", "restaurado app.py"]
        assert checkpoints.available("ses") == 0


def test_diff_since_last_shows_what_the_agent_changed(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    (project / "a.py").write_text("x = 1\n", encoding="utf-8")
    with patch.object(checkpoints, "COPIES_DIR", tmp_path / "copias"), \
         patch.object(checkpoints, "SESSIONS_FILE", tmp_path / "copias" / "s.json"):
        assert checkpoints.diff_since_last("ses") == ""
        checkpoints.snapshot("ses", project, "cambia x")
        (project / "a.py").write_text("x = 2\n", encoding="utf-8")
        (project / "b.py").write_text("y = 3\n", encoding="utf-8")
        diff = checkpoints.diff_since_last("ses")
    assert "-x = 1" in diff and "+x = 2" in diff and "+y = 3" in diff
