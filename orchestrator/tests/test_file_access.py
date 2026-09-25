import file_access


def _set_allowed(monkeypatch, tmp_path):
    desktop = tmp_path / "Desktop"
    documents = tmp_path / "Documents"
    desktop.mkdir()
    documents.mkdir()
    monkeypatch.setattr(file_access, "ALLOWED_DIRS", [desktop, documents])
    return desktop, documents


def test_leer_archivo_reads_file_inside_allowed_dir(monkeypatch, tmp_path):
    desktop, _ = _set_allowed(monkeypatch, tmp_path)
    (desktop / "notas.txt").write_text("contenido de prueba", encoding="utf-8")

    result = file_access.leer_archivo(str(desktop / "notas.txt"))

    assert result == "contenido de prueba"


def test_leer_archivo_rejects_path_outside_allowed_dirs(monkeypatch, tmp_path):
    _set_allowed(monkeypatch, tmp_path)
    outside = tmp_path / "fuera" / "secreto.txt"
    outside.parent.mkdir()
    outside.write_text("no deberias ver esto", encoding="utf-8")

    result = file_access.leer_archivo(str(outside))

    assert "no tengo permiso" in result.lower()


def test_leer_archivo_rejects_traversal_escape(monkeypatch, tmp_path):
    desktop, _ = _set_allowed(monkeypatch, tmp_path)
    outside = tmp_path / "fuera" / "secreto.txt"
    outside.parent.mkdir()
    outside.write_text("no deberias ver esto", encoding="utf-8")

    result = file_access.leer_archivo(str(desktop / ".." / "fuera" / "secreto.txt"))

    assert "no tengo permiso" in result.lower()


def test_leer_archivo_reports_missing_file(monkeypatch, tmp_path):
    desktop, _ = _set_allowed(monkeypatch, tmp_path)

    result = file_access.leer_archivo(str(desktop / "no-existe.txt"))

    assert "no existe" in result.lower()


def test_leer_archivo_truncates_huge_files(monkeypatch, tmp_path):
    desktop, _ = _set_allowed(monkeypatch, tmp_path)
    monkeypatch.setattr(file_access, "MAX_FILE_CHARS", 10)
    (desktop / "grande.txt").write_text("a" * 1000, encoding="utf-8")

    result = file_access.leer_archivo(str(desktop / "grande.txt"))

    assert "truncado" in result


def test_listar_carpeta_lists_entries_inside_allowed_dir(monkeypatch, tmp_path):
    desktop, _ = _set_allowed(monkeypatch, tmp_path)
    (desktop / "uno.txt").write_text("x", encoding="utf-8")
    (desktop / "subcarpeta").mkdir()

    result = file_access.listar_carpeta(str(desktop))

    assert "uno.txt" in result
    assert "[carpeta] subcarpeta" in result


def test_listar_carpeta_rejects_path_outside_allowed_dirs(monkeypatch, tmp_path):
    _set_allowed(monkeypatch, tmp_path)
    outside = tmp_path / "fuera"
    outside.mkdir()

    result = file_access.listar_carpeta(str(outside))

    assert "no tengo permiso" in result.lower()
