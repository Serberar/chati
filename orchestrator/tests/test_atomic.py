from unittest.mock import patch

import pytest

import atomic
import session_docs


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path):
    target = tmp_path / "datos.json"
    atomic.write_text(target, '{"v": 1}')
    with patch("atomic.os.replace", side_effect=OSError("se fue la luz")):
        with pytest.raises(OSError):
            atomic.write_text(target, '{"v": 2}')
    assert target.read_text(encoding="utf-8") == '{"v": 1}'
    assert list(tmp_path.iterdir()) == [target]  # sin temporales sueltos


def test_a_broken_documents_file_does_not_break_the_conversation(tmp_path, monkeypatch):
    """Auditoria 2026-09-30: un meta.json a medias hacia fallar cada mensaje."""
    monkeypatch.setattr(session_docs, "SESSION_DOCS_DIR", tmp_path)
    d = tmp_path / "u1" / "s1"
    d.mkdir(parents=True)
    (d / "meta.json").write_text('{"doc.txt": {"chars": 1', encoding="utf-8")
    assert session_docs.list_docs("u1", "s1") == []
    assert session_docs.context_for("u1", "s1", "hola", b"x" * 44, 1) == ("", [])
