"""Documentos que ocupan poco pero se descomprimen en muchisimo (auditoria
2026-09-30): un .docx de 0,4 MB eran 150 MB de texto y 334 MB de memoria."""

import zipfile

import pytest
from docx import Document

import rag


def _docx_with_text(path, text):
    doc = Document()
    doc.add_paragraph(text)
    doc.save(path)


def test_normal_documents_still_work(tmp_path):
    _docx_with_text(tmp_path / "a.docx", "Informe anual. Ventas en alza.")
    assert "Ventas en alza" in rag._extract_text(tmp_path / "a.docx")
    (tmp_path / "b.txt").write_text("hola mundo", encoding="utf-8")
    assert rag._extract_text(tmp_path / "b.txt") == "hola mundo"


def test_a_docx_that_expands_to_too_much_is_rejected_before_opening(tmp_path):
    base, bomb = tmp_path / "base.docx", tmp_path / "bomba.docx"
    _docx_with_text(base, "x")
    with zipfile.ZipFile(base) as src, zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, src.read(item.filename))
        dst.writestr("word/relleno.xml", b"a" * (rag.MAX_DOCX_UNCOMPRESSED + 1))
    assert bomb.stat().st_size < 1_000_000  # ocupa poco...
    with pytest.raises(ValueError, match="demasiado grande"):
        rag._extract_text(bomb)  # ...pero no se abre


def test_huge_text_is_capped_and_chunks_stay_small(tmp_path):
    big = tmp_path / "grande.txt"
    big.write_text("sin puntos " * 400_000, encoding="utf-8")  # 4,4 M caracteres, ni un punto
    text = rag._extract_text(big)
    assert len(text) == rag.MAX_TEXT_CHARS
    chunks = rag._chunk_text(text[:50_000])
    assert chunks and max(len(c) for c in chunks) <= rag.CHUNK_SIZE + rag.CHUNK_SIZE // 3 + 10


def test_a_broken_docx_gives_a_clear_message(tmp_path):
    fake = tmp_path / "roto.docx"
    fake.write_bytes(b"esto no es un zip")
    with pytest.raises(ValueError, match="dañado"):
        rag._extract_text(fake)
