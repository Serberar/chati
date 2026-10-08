import model_registry


def _make_tree(tmp_path):
    """Crea un arbol de carpetas de prueba con la misma forma que
    models/img y models/vid reales, sin tocar los datos de verdad."""
    img = tmp_path / "img"
    vid = tmp_path / "vid"
    (img / "checkpoints" / "sdxl").mkdir(parents=True)
    (img / "checkpoints" / "sd15").mkdir(parents=True)
    (img / "diffusion_models" / "flux").mkdir(parents=True)
    (vid / "checkpoints" / "ltxv").mkdir(parents=True)
    return img, vid


def test_list_image_models_finds_files_in_architecture_subfolders(tmp_path, monkeypatch):
    img, vid = _make_tree(tmp_path)
    (img / "checkpoints" / "sdxl" / "modelo_a.safetensors").write_bytes(b"x")
    (img / "diffusion_models" / "flux" / "modelo_b.gguf").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    models = model_registry.list_image_models()

    ids = {m.id for m in models}
    assert ids == {"sdxl:modelo_a", "flux:modelo_b"}


def test_list_image_models_ignores_wrong_extension(tmp_path, monkeypatch):
    img, vid = _make_tree(tmp_path)
    (img / "checkpoints" / "sdxl" / "notas.txt").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    assert model_registry.list_image_models() == []


def test_list_image_models_empty_when_no_folders_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(model_registry, "IMG_DIR", tmp_path / "no-existe-img")
    monkeypatch.setattr(model_registry, "VID_DIR", tmp_path / "no-existe-vid")

    assert model_registry.list_image_models() == []
    assert model_registry.list_video_models() == []


def test_model_entry_comfy_path_uses_native_separator(tmp_path, monkeypatch):
    import os
    img, vid = _make_tree(tmp_path)
    (img / "checkpoints" / "sdxl" / "modelo_a.safetensors").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    entry = model_registry.list_image_models()[0]

    assert entry.comfy_path == f"sdxl{os.sep}modelo_a.safetensors"


def test_get_image_model_automatic_prefers_flux(tmp_path, monkeypatch):
    img, vid = _make_tree(tmp_path)
    (img / "checkpoints" / "sdxl" / "modelo_sdxl.safetensors").write_bytes(b"x")
    (img / "diffusion_models" / "flux" / "modelo_flux.gguf").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    auto = model_registry.get_image_model(None)

    assert auto.architecture == "flux"


def test_get_image_model_automatic_falls_back_when_no_flux(tmp_path, monkeypatch):
    img, vid = _make_tree(tmp_path)
    (img / "checkpoints" / "sdxl" / "modelo_sdxl.safetensors").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    auto = model_registry.get_image_model(None)

    assert auto.architecture == "sdxl"


def test_get_image_model_returns_none_when_nothing_installed(tmp_path, monkeypatch):
    img, vid = _make_tree(tmp_path)
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    assert model_registry.get_image_model(None) is None


def test_get_image_model_by_specific_id(tmp_path, monkeypatch):
    img, vid = _make_tree(tmp_path)
    (img / "checkpoints" / "sdxl" / "modelo_a.safetensors").write_bytes(b"x")
    (img / "checkpoints" / "sd15" / "modelo_b.safetensors").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    found = model_registry.get_image_model("sd15:modelo_b")

    assert found.id == "sd15:modelo_b"


def test_get_image_model_returns_none_for_unknown_id(tmp_path, monkeypatch):
    img, vid = _make_tree(tmp_path)
    (img / "checkpoints" / "sdxl" / "modelo_a.safetensors").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    assert model_registry.get_image_model("sdxl:ya-no-existe") is None


def test_get_video_model_automatic_returns_first_available(tmp_path, monkeypatch):
    img, vid = _make_tree(tmp_path)
    (vid / "checkpoints" / "ltxv" / "modelo_video.safetensors").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    auto = model_registry.get_video_model(None)

    assert auto.id == "ltxv:modelo_video"


def test_image_model_folder_returns_the_right_path(tmp_path, monkeypatch):
    monkeypatch.setattr(model_registry, "IMG_DIR", tmp_path / "img")

    folder = model_registry.image_model_folder("sdxl")

    assert folder == tmp_path / "img" / "checkpoints" / "sdxl"


def test_image_model_folder_returns_none_for_unknown_architecture():
    assert model_registry.image_model_folder("no-existe") is None


def test_list_image_architectures_includes_empty_ones(tmp_path, monkeypatch):
    """sd15 no tiene ningun archivo en el arbol de prueba, pero debe seguir
    apareciendo (con 0 instalados) - el "Añadir modelo" de la interfaz tiene
    que poder enseñar su carpeta aunque este vacia todavia."""
    img, vid = _make_tree(tmp_path)
    (img / "checkpoints" / "sdxl" / "modelo_a.safetensors").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    archs = {a["id"]: a for a in model_registry.list_image_architectures()}

    assert set(archs) == {"sdxl", "sd15", "flux", "qwen", "flux_kontext"}
    assert archs["sdxl"]["installed_count"] == 1
    assert archs["sd15"]["installed_count"] == 0
    assert archs["sdxl"]["folder"] == str(img / "checkpoints" / "sdxl")
    assert archs["sdxl"]["label"] == "SDXL"


def test_list_video_architectures_includes_empty_ones(tmp_path, monkeypatch):
    img, vid = _make_tree(tmp_path)
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    archs = {a["id"]: a for a in model_registry.list_video_architectures()}

    assert set(archs) == {"ltxv"}
    assert archs["ltxv"]["installed_count"] == 0
    assert archs["ltxv"]["folder"] == str(vid / "checkpoints" / "ltxv")


def test_image_model_path_returns_the_full_file_path(tmp_path, monkeypatch):
    img, vid = _make_tree(tmp_path)
    (img / "checkpoints" / "sdxl" / "modelo_a.safetensors").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    path = model_registry.image_model_path("sdxl:modelo_a")

    assert path == img / "checkpoints" / "sdxl" / "modelo_a.safetensors"
    assert path.exists()


def test_image_model_path_returns_none_for_unknown_id(tmp_path, monkeypatch):
    img, vid = _make_tree(tmp_path)
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    assert model_registry.image_model_path("sdxl:no-existe") is None


def test_video_model_path_returns_the_full_file_path(tmp_path, monkeypatch):
    img, vid = _make_tree(tmp_path)
    (vid / "checkpoints" / "ltxv" / "modelo_v.safetensors").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)

    path = model_registry.video_model_path("ltxv:modelo_v")

    assert path == vid / "checkpoints" / "ltxv" / "modelo_v.safetensors"


def test_the_photo_edit_model_is_not_offered_as_a_generator(tmp_path, monkeypatch):
    """FLUX Kontext solo edita una foto que se le da: si saliera en la lista,
    elegirlo para generar desde texto fallaria (ver photo_edit.py)."""
    img, vid = _make_tree(tmp_path)
    (img / "diffusion_models" / "flux_kontext").mkdir(parents=True)
    (img / "diffusion_models" / "flux_kontext" / "flux1-kontext-dev-Q4_K_S.gguf").write_bytes(b"x")
    (img / "diffusion_models" / "flux" / "flux1-schnell-Q4_K_S.gguf").write_bytes(b"x")
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    monkeypatch.setattr(model_registry, "VID_DIR", vid)
    assert [m.id for m in model_registry.list_image_models()] == ["flux:flux1-schnell-Q4_K_S"]
    assert model_registry.get_edit_model().comfy_path.endswith("flux1-kontext-dev-Q4_K_S.gguf")


def test_no_photo_edit_model_when_it_is_not_installed(tmp_path, monkeypatch):
    img, vid = _make_tree(tmp_path)
    monkeypatch.setattr(model_registry, "IMG_DIR", img)
    assert model_registry.get_edit_model() is None
