import pytest
from cryptography.fernet import Fernet

import session_docs


@pytest.fixture(autouse=True)
def _isolated_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(session_docs, "SESSION_DOCS_DIR", tmp_path / "session_docs")


DEK = Fernet.generate_key()


def test_short_document_goes_whole_into_the_context_and_is_encrypted_on_disk():
    session_docs.save("u1", "s1", "contrato.txt", "La permanencia es de 12 meses.".encode(), DEK, 1)

    context, evidence = session_docs.context_for("u1", "s1", "resumeme esto", DEK, 1)

    assert "La permanencia es de 12 meses." in context
    assert 'nombre="contrato.txt"' in context
    assert evidence == [{"source": "adjunto:contrato.txt", "text": "La permanencia es de 12 meses."}]
    on_disk = b"".join(p.read_bytes() for p in session_docs.SESSION_DOCS_DIR.rglob("*.enc"))
    assert b"permanencia" not in on_disk


def test_long_document_sends_only_the_chunks_that_match_the_question(monkeypatch):
    monkeypatch.setattr(session_docs, "FULL_TEXT_LIMIT", 200)
    monkeypatch.setattr(session_docs, "TOP_CHUNKS", 1)
    relleno = " ".join(f"Frase de relleno numero {i} sin nada especial." for i in range(80))
    texto = relleno + " La clausula de penalizacion por baja anticipada es de 300 euros. " + relleno
    session_docs.save("u1", "s1", "largo.txt", texto.encode(), DEK, 1)

    context, _ = session_docs.context_for("u1", "s1", "¿cual es la penalizacion por baja?", DEK, 1)

    assert "300 euros" in context
    assert "fragmentos relevantes" in context
    assert len(context) < len(texto)


def test_documents_are_per_user_and_per_conversation():
    session_docs.save("u1", "s1", "a.txt", b"secreto de u1", DEK, 1)
    assert session_docs.list_docs("u2", "s1") == []
    assert session_docs.list_docs("u1", "otra") == []
    assert session_docs.context_for("u1", "otra", "x", DEK, 1) == ("", [])


def test_remove_and_delete_session():
    session_docs.save("u1", "s1", "a.txt", b"uno", DEK, 1)
    session_docs.save("u1", "s1", "b.txt", b"dos", DEK, 1)
    session_docs.remove("u1", "s1", "a.txt")
    assert [d["name"] for d in session_docs.list_docs("u1", "s1")] == ["b.txt"]
    session_docs.delete_session("u1", "s1")
    assert session_docs.list_docs("u1", "s1") == []


def test_rejects_unsupported_or_empty_files():
    with pytest.raises(ValueError, match="Formato"):
        session_docs.save("u1", "s1", "foto.exe", b"MZ", DEK, 1)
    with pytest.raises(ValueError, match="texto"):
        session_docs.save("u1", "s1", "vacio.txt", b"   ", DEK, 1)


def test_unreadable_after_password_reset_changes_the_key():
    session_docs.save("u1", "s1", "a.txt", b"hola", DEK, 1)
    assert session_docs.context_for("u1", "s1", "x", Fernet.generate_key(), 2) == ("", [])
    assert session_docs.get_original("u1", "s1", "a.txt", DEK, 1) == b"hola"


def test_path_traversal_in_names_is_neutralised():
    session_docs.save("u1", "../../s1", "../../evil.txt", b"x", DEK, 1)
    assert all(session_docs.SESSION_DOCS_DIR in p.parents
               for p in session_docs.SESSION_DOCS_DIR.parent.rglob("*.enc"))
