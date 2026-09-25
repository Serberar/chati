"""Pruebas deterministas de model_catalog.py."""

import model_catalog


def test_catalog_for_gpu_tiers_includes_all_modalities():
    for tier in ["minimo", "8gb", "12gb", "16gb_plus"]:
        entries = model_catalog.catalog_for_tier(tier)
        modalities = {e.modality for e in entries}
        assert modalities == {"texto", "imagen", "video", "voz"}


def test_catalog_for_cpu_only_excludes_video():
    entries = model_catalog.catalog_for_tier("cpu_only")
    modalities = {e.modality for e in entries}
    assert "video" not in modalities
    assert "texto" in modalities and "imagen" in modalities and "voz" in modalities


def test_every_entry_has_either_ollama_model_or_download_url():
    for entry in model_catalog.catalog_for_tier("8gb"):
        assert entry.ollama_model or entry.download_url, f"{entry.id} no tiene forma de instalarse"


def test_image_and_video_entries_have_architecture_and_filename():
    for entry in model_catalog.catalog_for_tier("8gb"):
        if entry.modality in ("imagen", "video"):
            assert entry.architecture, entry.id
            assert entry.filename, entry.id
            assert entry.download_url, entry.id
