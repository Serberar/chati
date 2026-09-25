import json

import model_updates


def test_model_layer_digest_finds_the_model_layer():
    manifest = {
        "layers": [
            {"mediaType": "application/vnd.ollama.image.template", "digest": "sha256:aaa"},
            {"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:bbb"},
            {"mediaType": "application/vnd.ollama.image.license", "digest": "sha256:ccc"},
        ]
    }
    assert model_updates._model_layer_digest(manifest) == "sha256:bbb"


def test_model_layer_digest_returns_none_when_missing():
    assert model_updates._model_layer_digest({"layers": []}) is None


def test_installed_official_models_reads_manifest_tree(tmp_path, monkeypatch):
    lib = tmp_path / "manifests" / "registry.ollama.ai" / "library"
    (lib / "qwen2.5").mkdir(parents=True)
    (lib / "qwen2.5" / "7b").write_text("{}", encoding="utf-8")
    (lib / "qwen2.5" / "14b").write_text("{}", encoding="utf-8")
    (lib / "qwen3-coder").mkdir(parents=True)
    (lib / "qwen3-coder" / "30b").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))

    found = model_updates.installed_official_models()

    assert set(found) == {("qwen2.5", "7b"), ("qwen2.5", "14b"), ("qwen3-coder", "30b")}


def test_installed_official_models_empty_when_no_ollama_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path / "no-existe"))
    assert model_updates.installed_official_models() == []


def test_local_digest_reads_the_manifest_file(tmp_path, monkeypatch):
    lib = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "qwen2.5"
    lib.mkdir(parents=True)
    manifest = {"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:local123"}]}
    (lib / "7b").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))

    assert model_updates.local_digest("qwen2.5", "7b") == "sha256:local123"


def test_local_digest_none_when_not_installed(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    assert model_updates.local_digest("no-instalado", "7b") is None


def test_check_updates_flags_differing_digests(tmp_path, monkeypatch):
    lib = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "qwen2.5"
    lib.mkdir(parents=True)
    manifest = {"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:old"}]}
    (lib / "7b").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    monkeypatch.setattr(model_updates, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(model_updates, "remote_digest", lambda name, tag, timeout=10: "sha256:new")

    results = model_updates.check_updates(force=True)

    assert results == [{"model": "qwen2.5:7b", "update_available": True, "error": None}]


def test_check_updates_up_to_date_when_digests_match(tmp_path, monkeypatch):
    lib = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "qwen2.5"
    lib.mkdir(parents=True)
    manifest = {"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:same"}]}
    (lib / "7b").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    monkeypatch.setattr(model_updates, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(model_updates, "remote_digest", lambda name, tag, timeout=10: "sha256:same")

    results = model_updates.check_updates(force=True)

    assert results == [{"model": "qwen2.5:7b", "update_available": False, "error": None}]


def test_check_updates_reports_network_errors_without_raising(tmp_path, monkeypatch):
    lib = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "qwen2.5"
    lib.mkdir(parents=True)
    manifest = {"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:x"}]}
    (lib / "7b").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    monkeypatch.setattr(model_updates, "CACHE_PATH", tmp_path / "cache.json")

    def _boom(name, tag, timeout=10):
        raise ConnectionError("sin red")
    monkeypatch.setattr(model_updates, "remote_digest", _boom)

    results = model_updates.check_updates(force=True)

    assert results == [{"model": "qwen2.5:7b", "update_available": False, "error": "sin red"}]


def test_check_updates_skips_registry_for_local_cpu_variants(tmp_path, monkeypatch):
    """Las variantes '-cpu' (fix de Blackwell, ver AGENTS.md) se crean con
    `ollama create` a partir de un modelo ya descargado - nunca han existido
    en el registro, comprobarlas contra el produce un 404 seguro. Deben
    detectarse y saltarse sin llamar a la red."""
    lib = tmp_path / "manifests" / "registry.ollama.ai" / "library" / "qwen3-coder"
    lib.mkdir(parents=True)
    manifest = {"layers": [{"mediaType": "application/vnd.ollama.image.model", "digest": "sha256:x"}]}
    (lib / "30b-cpu").write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    monkeypatch.setattr(model_updates, "CACHE_PATH", tmp_path / "cache.json")

    def _should_not_be_called(name, tag, timeout=10):
        raise AssertionError("no deberia llamar al registro para una variante local")
    monkeypatch.setattr(model_updates, "remote_digest", _should_not_be_called)

    results = model_updates.check_updates(force=True)

    assert results == [{"model": "qwen3-coder:30b-cpu", "update_available": False, "error": None, "local_only": True}]


def test_check_updates_uses_cache_within_ttl(tmp_path, monkeypatch):
    monkeypatch.setenv("OLLAMA_MODELS", str(tmp_path))
    cache_path = tmp_path / "cache.json"
    monkeypatch.setattr(model_updates, "CACHE_PATH", cache_path)
    cache_path.write_text(json.dumps({
        "checked_at_epoch": __import__("time").time(),
        "results": [{"model": "cacheado:1b", "update_available": False, "error": None}],
    }), encoding="utf-8")

    def _should_not_be_called(name, tag, timeout=10):
        raise AssertionError("no deberia llamar a la red con cache valida")
    monkeypatch.setattr(model_updates, "remote_digest", _should_not_be_called)

    results = model_updates.check_updates(force=False)

    assert results == [{"model": "cacheado:1b", "update_available": False, "error": None}]
