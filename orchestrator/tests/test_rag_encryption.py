"""Tests deterministas del cifrado en rag.py - usan un cliente de embeddings
falso (vectores derivados del hash del texto, no semanticos de verdad) para
no depender de Ollama en vivo. La confirmacion de que la busqueda SEMANTICA
real sigue funcionando tras cifrar se hizo aparte, en vivo contra Chroma +
Ollama reales (ver ROADMAP.md, punto 0, fase 2)."""

import hashlib

from cryptography.fernet import Fernet

import rag


class FakeOllamaClient:
    """embed() determinista: mismo texto siempre da el mismo vector, textos
    distintos dan vectores distintos - suficiente para probar la mecanica
    de cifrado/filtrado sin necesitar embeddings semanticamente reales."""

    def embed(self, model, texts):
        vectors = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            vectors.append([b / 255.0 for b in digest[:16]])
        return vectors


def _kb(tmp_path, monkeypatch):
    monkeypatch.setattr(rag, "KB_DIR", tmp_path / "knowledge")
    monkeypatch.setattr(rag, "DOCS_DIR", tmp_path / "knowledge" / "documents")
    rag.KB_DIR.mkdir(parents=True, exist_ok=True)
    rag.DOCS_DIR.mkdir(exist_ok=True)
    return rag.KnowledgeBase(FakeOllamaClient())


def test_encrypted_document_not_stored_as_plaintext_in_chroma(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    dek = Fernet.generate_key()

    kb.add_document("secreto.txt", "informacion confidencial de verdad".encode("utf-8"), dek=dek, key_generation=1)

    raw = kb.collection.get(where={"source": "secreto.txt"})
    assert "informacion confidencial" not in raw["documents"][0]


def test_query_with_correct_dek_decrypts_result(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    dek = Fernet.generate_key()
    kb.add_document("doc.txt", "contenido de prueba".encode("utf-8"), dek=dek, key_generation=1)

    results = kb.query("contenido de prueba", k=3, dek=dek, key_generation=1)

    assert results
    assert results[0]["text"] == "contenido de prueba"


def test_query_without_dek_never_returns_plaintext_of_encrypted_doc(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    dek = Fernet.generate_key()
    kb.add_document("doc.txt", "contenido de prueba".encode("utf-8"), dek=dek, key_generation=1)

    results = kb.query("contenido de prueba", k=3)  # sin dek

    for r in results:
        assert r["text"] != "contenido de prueba"


def test_query_excludes_orphaned_key_generation(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    old_dek = Fernet.generate_key()
    new_dek = Fernet.generate_key()
    kb.add_document("doc.txt", "contenido de antes del reset".encode("utf-8"), dek=old_dek, key_generation=1)

    # tras un reset de contraseña: nueva dek, key_generation distinta
    results = kb.query("contenido de antes del reset", k=3, dek=new_dek, key_generation=2)

    assert results == []


def test_legacy_unencrypted_document_still_queryable_without_dek(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    kb.add_document("legado.txt", "documento subido antes del sistema de usuarios".encode("utf-8"))  # sin dek

    results = kb.query("documento subido antes del sistema de usuarios", k=3)

    assert results
    assert results[0]["text"] == "documento subido antes del sistema de usuarios"


def test_legacy_unencrypted_document_still_queryable_even_when_dek_given(tmp_path, monkeypatch):
    # documentos guardados antes del cifrado no tienen key_generation en su
    # metadata - deben seguir siendo legibles tal cual, no tratarse como huerfanos
    kb = _kb(tmp_path, monkeypatch)
    kb.add_document("legado.txt", "documento legado".encode("utf-8"))  # sin dek

    results = kb.query("documento legado", k=3, dek=Fernet.generate_key(), key_generation=1)

    assert results
    assert results[0]["text"] == "documento legado"


def test_documents_isolated_by_user_id(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    kb.add_document("doc.txt", "contenido de usuario uno".encode("utf-8"), user_id="user-1")
    kb.add_document("doc.txt", "contenido de usuario dos".encode("utf-8"), user_id="user-2")

    docs_user1 = kb.list_documents(user_id="user-1")
    docs_user2 = kb.list_documents(user_id="user-2")
    assert {d["filename"] for d in docs_user1} == {"doc.txt"}
    assert {d["filename"] for d in docs_user2} == {"doc.txt"}

    results_user1 = kb.query("contenido de usuario uno", k=3, user_id="user-1")
    assert results_user1
    assert results_user1[0]["text"] == "contenido de usuario uno"


def test_query_scoped_to_user_does_not_leak_other_users_documents(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    kb.add_document("doc.txt", "secreto de otro usuario".encode("utf-8"), user_id="user-2")

    results = kb.query("secreto de otro usuario", k=3, user_id="user-1")

    assert results == []


def test_delete_document_scoped_to_user_does_not_affect_other_users(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    kb.add_document("doc.txt", "de usuario uno".encode("utf-8"), user_id="user-1")
    kb.add_document("doc.txt", "de usuario dos".encode("utf-8"), user_id="user-2")

    kb.delete_document("doc.txt", user_id="user-1")

    assert kb.list_documents(user_id="user-1") == []
    assert {d["filename"] for d in kb.list_documents(user_id="user-2")} == {"doc.txt"}


def test_delete_all_for_user_removes_documents_and_conversations(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    kb.add_document("doc.txt", "de usuario uno".encode("utf-8"), user_id="user-1")
    kb.add_conversation("sesion-x", "pregunta", "respuesta", user_id="user-1")
    kb.add_document("doc.txt", "de usuario dos".encode("utf-8"), user_id="user-2")

    kb.delete_all_for_user("user-1")

    assert kb.list_documents(user_id="user-1") == []
    assert {d["filename"] for d in kb.list_documents(user_id="user-2")} == {"doc.txt"}
    assert kb.collection.get(where={"session_id": "sesion-x"})["ids"] == []


def test_delete_all_for_user_does_not_raise_when_nothing_to_delete(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    kb.delete_all_for_user("usuario-sin-datos")  # no lanza


def test_add_conversation_encrypts_when_dek_given(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    dek = Fernet.generate_key()

    kb.add_conversation("sesion-1", "¿cual es la capital de Francia?", "Paris", dek=dek, key_generation=1)

    raw = kb.collection.get(where={"session_id": "sesion-1"})
    assert "Paris" not in raw["documents"][0]

    results = kb.query("capital de Francia", k=3, dek=dek, key_generation=1)
    assert results
    assert "Paris" in results[0]["text"]


def test_add_context_note_appears_in_list_memory_entries(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)

    kb.add_context_note("Trabajo como ingeniero de software, prefiero respuestas concisas.")

    entries = kb.list_memory_entries()
    assert len(entries) == 1
    assert entries[0]["type"] == "note"
    assert entries[0]["text"] == "Trabajo como ingeniero de software, prefiero respuestas concisas."


def test_list_memory_entries_includes_conversations_and_notes_but_not_documents(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    kb.add_document("doc.txt", "contenido de un documento".encode("utf-8"))
    kb.add_conversation("sesion-1", "hola", "hola, como estas?")
    kb.add_context_note("una nota manual")

    entries = kb.list_memory_entries()

    assert {e["type"] for e in entries} == {"conversation", "note"}
    assert len(entries) == 2


def test_list_memory_entries_decrypts_with_correct_dek(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    dek = Fernet.generate_key()
    kb.add_context_note("nota privada de verdad", dek=dek, key_generation=1)

    entries = kb.list_memory_entries(dek=dek, key_generation=1)

    assert entries[0]["text"] == "nota privada de verdad"


def test_list_memory_entries_isolated_by_user_id(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    kb.add_context_note("nota de usuario uno", user_id="user-1")
    kb.add_context_note("nota de usuario dos", user_id="user-2")

    entries_user1 = kb.list_memory_entries(user_id="user-1")

    assert len(entries_user1) == 1
    assert entries_user1[0]["text"] == "nota de usuario uno"


def test_update_memory_entry_replaces_note_content(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    entry_id = kb.add_context_note("texto original")

    ok = kb.update_memory_entry(entry_id, "texto editado")

    assert ok is True
    entries = kb.list_memory_entries()
    assert len(entries) == 1
    assert entries[0]["text"] == "texto editado"
    assert entries[0]["id"] == entry_id  # mismo id, no duplicado


def test_update_memory_entry_refuses_to_edit_a_conversation_entry(tmp_path, monkeypatch):
    """Un intercambio de conversacion indexado se edita desde el mensaje
    real (panel lateral), no como nota suelta - evita que se pueda
    desincronizar el contenido indexado del mensaje que el usuario ve."""
    kb = _kb(tmp_path, monkeypatch)
    kb.add_conversation("sesion-1", "hola", "hola, como estas?")
    conv_id = kb.collection.get(where={"session_id": "sesion-1"})["ids"][0]

    ok = kb.update_memory_entry(conv_id, "texto manipulado")

    assert ok is False


def test_update_memory_entry_refuses_when_owned_by_another_user(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    entry_id = kb.add_context_note("nota de otro usuario", user_id="user-2")

    ok = kb.update_memory_entry(entry_id, "intento de edicion", user_id="user-1")

    assert ok is False
    assert kb.list_memory_entries(user_id="user-2")[0]["text"] == "nota de otro usuario"


def test_delete_memory_entry_removes_it(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    entry_id = kb.add_context_note("nota a borrar")

    ok = kb.delete_memory_entry(entry_id)

    assert ok is True
    assert kb.list_memory_entries() == []


def test_delete_memory_entry_refuses_when_owned_by_another_user(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    entry_id = kb.add_context_note("nota de otro usuario", user_id="user-2")

    ok = kb.delete_memory_entry(entry_id, user_id="user-1")

    assert ok is False
    assert len(kb.list_memory_entries(user_id="user-2")) == 1


def test_delete_memory_entry_returns_false_for_unknown_id(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    assert kb.delete_memory_entry("no-existe") is False


def test_original_file_on_disk_is_encrypted_and_deleted_with_the_document(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    dek = Fernet.generate_key()
    kb.add_document("secreto.txt", "informacion confidencial de verdad".encode("utf-8"),
                    user_id="u1", dek=dek, key_generation=1)
    files = list((rag.DOCS_DIR / "u1").iterdir())
    assert [f.name for f in files] == ["secreto.txt.enc"]
    assert b"confidencial" not in files[0].read_bytes()
    kb.delete_document("secreto.txt", user_id="u1")
    assert not list((rag.DOCS_DIR / "u1").iterdir())


def test_plain_originals_from_before_are_encrypted_at_login(tmp_path, monkeypatch):
    kb = _kb(tmp_path, monkeypatch)
    d = rag.DOCS_DIR / "u1"
    d.mkdir(parents=True)
    (d / "viejo.txt").write_bytes(b"texto en claro")
    dek = Fernet.generate_key()
    assert kb.encrypt_plain_originals("u1", dek) == 1
    assert [f.name for f in d.iterdir()] == ["viejo.txt.enc"]
    assert kb.encrypt_plain_originals("u1", dek) == 0
