"""Entrenamiento de LoRA de persona - fase 1, solo SDXL (ver ROADMAP.md y
pendiente/pendiente.md, aportado por Sergio). FLUX usa el mismo sd-scripts,
queda para la siguiente etapa.

Usa sd-scripts (kohya-ss), instalado en su propio venv independiente
(C:/AI/sd-scripts/venv) con el mismo PyTorch nightly cu128 que ya funciona
con la RTX 5060 (Blackwell) en ComfyUI - ver el venv de C:/AI/ComfyUI como
referencia de esa misma version.

El entrenamiento en si tarda minutos u horas (varias epocas de SDXL en una
laptop de 8GB VRAM) - se lanza como subproceso en segundo plano y nunca se
espera aqui a que termine. El progreso se sigue con un archivo status.json
por persona/arquitectura, no con streaming en vivo: comprobar si el proceso
sigue vivo (por pid) y si el .safetensors final ya existe.

Deliberadamente sin captions por foto (pendiente.md: "el usuario no deberia
tener que conocer que es un LoRA ni como se entrena") - se usa un unico
"class_tokens" para todo el dataset, generado a partir del nombre de la
persona. Quien use la persona luego escribe ese token en el prompt para
invocarla."""

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import model_registry
from paths import DATA_ROOT

# El instalador lo clona en <carpeta de datos>/sd-scripts (install.ps1, paso
# 2b); en desarrollo DATA_ROOT es C:/AI, la misma ruta que antes.
SD_SCRIPTS_DIR = DATA_ROOT / "sd-scripts"
SD_SCRIPTS_PYTHON = SD_SCRIPTS_DIR / "venv" / "Scripts" / "python.exe"
ACCELERATE_EXE = SD_SCRIPTS_DIR / "venv" / "Scripts" / "accelerate.exe"
ACCELERATE_CONFIG = SD_SCRIPTS_DIR / "_accelerate_config.yaml"
DATASETS_DIR = SD_SCRIPTS_DIR / "_datasets"
# Dentro de la carpeta de LoRAs que lee ComfyUI (extra_model_paths.yaml:
# img/loras) - antes estaba en models/loras, donde ComfyUI no la veia.
PERSONAS_DIR = model_registry.IMG_DIR / "loras" / "personas"

# Config de accelerate minima para una sola GPU local, sin distribuido - se
# escribe a mano (no con "accelerate config", que es interactivo y colgaria
# el subproceso esperando entrada que nunca llega).
_ACCELERATE_CONFIG_YAML = """\
compute_environment: LOCAL_MACHINE
distributed_type: 'NO'
downcast_bf16: 'no'
gpu_ids: '0'
machine_rank: 0
main_training_function: main
mixed_precision: bf16
num_machines: 1
num_processes: 1
rdzv_backend: static
same_network: true
tpu_env: []
tpu_use_cluster: false
tpu_use_sudo: false
use_cpu: false
"""


class NoBaseModelError(Exception):
    """No hay ningun checkpoint SDXL instalado para entrenar sobre el -
    hace falta uno en models/img/checkpoints/sdxl/ (ver model_registry.py)."""


class TrainingAlreadyRunningError(Exception):
    """Solo una GPU, no tiene sentido entrenar dos LoRAs a la vez."""


def _sanitize_name(name: str) -> str:
    """Nombre de persona -> nombre de carpeta seguro, sin espacios ni
    caracteres raros. Se guarda el nombre original aparte en meta.json para
    mostrarlo tal cual en la interfaz."""
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "-", name.strip()).strip("-")
    return safe or f"persona-{uuid.uuid4().hex[:6]}"


def _class_token(persona_folder_name: str) -> str:
    """Token unico para invocar esta persona en el prompt (p.ej. "ohwx-sergio
    person") - "ohwx" es un token raro en el vocabulario normal, evita que el
    LoRA se mezcle con conceptos ya existentes del modelo base."""
    return f"ohwx-{persona_folder_name} person"


# Cada usuario tiene sus personas en su propia carpeta (auditoria
# 2026-09-29: eran comunes, y cualquier usuario podia generar imagenes con la
# cara que otro habia entrenado).
def _owner_folder(owner: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9_-]+", "", owner or "")
    if not safe:
        raise ValueError("Falta el usuario dueño de la persona.")
    return safe


def persona_dir(owner: str, name: str) -> Path:
    return PERSONAS_DIR / _owner_folder(owner) / _sanitize_name(name)


def _dataset_dir(owner: str, name: str) -> Path:
    return DATASETS_DIR / _owner_folder(owner) / _sanitize_name(name)


def _status_path(owner: str, name: str, architecture: str) -> Path:
    return persona_dir(owner, name) / architecture / "status.json"


def _write_status(owner: str, name: str, architecture: str, status: dict) -> None:
    path = _status_path(owner, name, architecture)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_status(owner: str, name: str, architecture: str) -> dict | None:
    path = _status_path(owner, name, architecture)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _pid_alive(pid: int) -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=10,
        )
        return str(pid) in result.stdout
    except (subprocess.SubprocessError, OSError):
        return False


# Barra de tqdm de sd-scripts: "steps:  45%|####  | 27/60 [01:10<01:25, 2.6s/it, avr_loss=0.1]"
_STEPS_LINE = re.compile(r"steps:\s*(\d+)%\|[^|]*\|\s*(\d+)/(\d+)\s*\[([\d:]+)<([\d:?]+)")


def _eta_text(eta: str) -> str:
    parts = [int(x) for x in eta.split(":") if x.isdigit()]
    if not parts or "?" in eta:
        return ""
    secs = 0
    for x in parts:
        secs = secs * 60 + x
    if secs < 60:
        return "menos de un minuto"
    mins = round(secs / 60)
    return f"unos {mins} min" if mins < 60 else f"unas {mins // 60} h {mins % 60} min"


def training_progress(log_path: Path) -> dict:
    """{"percent", "eta"} del ultimo paso del registro de sd-scripts, o
    {"phase": "preparando"} mientras carga el modelo y prepara las fotos
    (antes del primer paso, que en SDXL tarda varios minutos)."""
    try:
        with open(log_path, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 20000))
            tail = f.read().decode("utf-8", errors="ignore")
    except OSError:
        return {"phase": "preparando"}
    matches = list(_STEPS_LINE.finditer(tail.replace("\r", "\n")))
    if not matches:
        return {"phase": "preparando"}
    m = matches[-1]
    return {"percent": int(m.group(1)), "step": int(m.group(2)), "steps": int(m.group(3)),
            "eta": _eta_text(m.group(5))}


def _lora_path(owner: str, name: str, architecture: str) -> Path:
    folder = _sanitize_name(name)
    return persona_dir(owner, name) / architecture / f"{folder}_{architecture}.safetensors"


def lora_for(owner: str, name: str, architecture: str = "sdxl") -> tuple[str, str] | None:
    """(nombre del LoRA para el nodo LoraLoader de ComfyUI, palabra clave para
    el prompt) si la persona de ESTE usuario esta lista; None si no."""
    path = _lora_path(owner, name, architecture)
    if not path.exists():
        return None
    # separador nativo del SO, como lo lista ComfyUI ("personas\<usuario>\x\sdxl\x_sdxl.safetensors")
    return str(path.relative_to(PERSONAS_DIR.parent)), _class_token(_sanitize_name(name))


def _drop_dataset(owner: str, name: str) -> None:
    """Las fotos de entrenamiento se copian en claro para sd-scripts: en cuanto
    termina (bien o mal) se borran."""
    shutil.rmtree(_dataset_dir(owner, name), ignore_errors=True)


def _any_training_running() -> bool:
    """De cualquier persona y usuario: solo comprobaba la misma persona, y dos
    entrenamientos a la vez en 8 GB de VRAM no caben (auditoria 2026-10-05)."""
    for status_file in PERSONAS_DIR.glob("*/*/sdxl/status.json"):
        try:
            status = json.loads(status_file.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if status.get("status") == "training" and _pid_alive(status.get("pid", -1))                 and not any(status_file.parent.glob("*.safetensors")):
            return True
    return False


def get_training_status(owner: str, name: str, architecture: str = "sdxl") -> dict:
    """Estado real, no solo lo que dice el archivo: si el .safetensors final
    ya existe se considera terminado aunque el proceso ya no este vivo (pudo
    cerrarse limpio); si el archivo de estado dice "training" pero el
    proceso ya no esta vivo y no hay .safetensors, es que fallo a medio
    camino."""
    lora_path = _lora_path(owner, name, architecture)
    if lora_path.exists():
        _drop_dataset(owner, name)
        return {"status": "listo", "file": str(lora_path)}

    status = _read_status(owner, name, architecture)
    if status is None:
        return {"status": "sin_empezar"}
    if status.get("status") == "training" and not _pid_alive(status.get("pid", -1)):
        _drop_dataset(owner, name)
        return {"status": "error", "error": "El entrenamiento se interrumpio antes de terminar."}
    if status.get("status") == "training" and status.get("log_file"):
        status = {**status, **training_progress(Path(status["log_file"]))}
    return status


def _pick_sdxl_base_checkpoint() -> Path:
    """Modelo SDXL YA instalado sobre el que entrenar - no se descarga uno
    aparte solo para entrenar. Prefiere 'sd_xl_base' (el modelo vainilla,
    mas estandar para entrenar LoRAs) si esta, si no el primero que haya."""
    sdxl_models = [m for m in model_registry.list_image_models() if m.architecture == "sdxl"]
    if not sdxl_models:
        raise NoBaseModelError(
            "No hay ningun modelo SDXL instalado - hace falta uno en "
            f"{model_registry.image_model_folder('sdxl')} para poder entrenar un LoRA."
        )
    preferred = next((m for m in sdxl_models if "base" in m.label.lower()), sdxl_models[0])
    return model_registry.image_model_folder("sdxl") / Path(preferred.comfy_path).name


def _write_dataset_toml(dataset_dir: Path, class_tokens: str, num_repeats: int) -> Path:
    toml_path = dataset_dir.parent / "dataset.toml"
    toml_path.write_text(
        "[general]\n"
        "resolution = 1024\n"
        "shuffle_caption = false\n"
        "\n"
        "[[datasets]]\n"
        "[[datasets.subsets]]\n"
        f"image_dir = '{dataset_dir.as_posix()}'\n"
        f"class_tokens = '{class_tokens}'\n"
        f"num_repeats = {num_repeats}\n",
        encoding="utf-8",
    )
    return toml_path


def start_training(owner: str, name: str, photo_paths: list[Path], epochs: int = 10) -> dict:
    """Prepara el dataset y lanza el entrenamiento SDXL en segundo plano.
    Devuelve inmediatamente (no espera a que termine) - usa
    get_training_status() para seguir el progreso. photo_paths: fotos YA
    guardadas en disco sin cifrar (llamador se encarga de eso, ver
    main.py - igual que el resto de referencias de cara)."""
    if not ACCELERATE_EXE.exists() or not SD_SCRIPTS_PYTHON.exists():
        raise RuntimeError(f"sd-scripts no esta instalado en {SD_SCRIPTS_DIR} (ver ROADMAP.md).")

    existing = get_training_status(owner, name, "sdxl")
    if existing.get("status") == "training":
        raise TrainingAlreadyRunningError(f"Ya hay un entrenamiento en curso para '{name}'.")
    if _any_training_running():
        raise TrainingAlreadyRunningError("Ya se esta entrenando otra persona: espera a que termine (solo hay una GPU).")

    base_checkpoint = _pick_sdxl_base_checkpoint()
    folder_name = _sanitize_name(name)
    class_tokens = _class_token(folder_name)

    dataset_images_dir = _dataset_dir(owner, name) / "images"
    dataset_images_dir.mkdir(parents=True, exist_ok=True)
    for old in dataset_images_dir.iterdir():
        old.unlink()
    for i, src in enumerate(photo_paths):
        dest = dataset_images_dir / f"{i:03d}{src.suffix.lower()}"
        dest.write_bytes(src.read_bytes())

    dataset_toml = _write_dataset_toml(dataset_images_dir, class_tokens, num_repeats=10)

    if not ACCELERATE_CONFIG.exists():
        ACCELERATE_CONFIG.write_text(_ACCELERATE_CONFIG_YAML, encoding="utf-8")

    output_dir = persona_dir(owner, name) / "sdxl"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_name = f"{folder_name}_sdxl"
    log_path = output_dir / "train.log"

    cmd = [
        str(ACCELERATE_EXE), "launch", "--config_file", str(ACCELERATE_CONFIG),
        "--num_cpu_threads_per_process", "1",
        str(SD_SCRIPTS_DIR / "sdxl_train_network.py"),
        f"--pretrained_model_name_or_path={base_checkpoint}",
        f"--dataset_config={dataset_toml}",
        f"--output_dir={output_dir}",
        f"--output_name={output_name}",
        "--save_model_as=safetensors",
        "--network_module=networks.lora",
        "--network_dim=32",
        "--network_alpha=16",
        "--learning_rate=1e-4",
        "--unet_lr=1e-4",
        "--text_encoder_lr", "1e-5", "1e-5",  # dos valores: Text Encoder 1 y 2 de SDXL (nargs, no "=")
        "--optimizer_type=AdamW8bit",
        "--lr_scheduler=constant",
        f"--max_train_epochs={epochs}",
        "--save_every_n_epochs=999",  # solo el resultado final, no checkpoints intermedios
        "--mixed_precision=bf16",
        # modelo base en fp8: SDXL entero no cabe en 8GB de VRAM junto al
        # entrenamiento - sin esto se desbordaba a memoria compartida y cada
        # paso tardaba ~130s (medido el 2026-09-28, RTX 5060 8GB)
        "--fp8_base",
        "--sdpa",  # atencion eficiente via PyTorch (xformers no esta instalado, no hace falta)
        "--gradient_checkpointing",
        "--cache_text_encoder_outputs",
        "--cache_latents",
        "--network_train_unet_only",  # obligatorio junto a --cache_text_encoder_outputs (ver docs)
        "--seed=42",
    ]

    with open(log_path, "w", encoding="utf-8") as log_file:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"}
        proc = subprocess.Popen(
            cmd, cwd=str(SD_SCRIPTS_DIR), stdout=log_file, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW, env=env,
        )

    _write_status(owner, name, "sdxl", {
        "status": "training", "pid": proc.pid, "started_at": time.time(),
        "photos": len(photo_paths), "epochs": epochs, "log_file": str(log_path),
    })
    return {"status": "training", "persona": name, "architecture": "sdxl"}


def list_personas(owner: str) -> list[dict]:
    """Las personas de ESTE usuario, con el estado de cada arquitectura que
    tenga (por ahora solo sdxl existe de verdad, ver fase 2 para flux)."""
    base = PERSONAS_DIR / _owner_folder(owner)
    if not base.exists():
        return []
    result = []
    for entry in sorted(base.iterdir()):
        if not entry.is_dir():
            continue
        result.append({
            "name": entry.name,
            "sdxl": get_training_status(owner, entry.name, "sdxl"),
        })
    return result


def delete_persona(owner: str, name: str) -> bool:
    """Borra el LoRA (la "cara" aprendida) y lo que quede de sus fotos. No se
    puede borrar mientras entrena (el proceso tiene los archivos abiertos)."""
    if get_training_status(owner, name).get("status") == "training":
        raise TrainingAlreadyRunningError("Esta persona sigue entrenando: espera a que termine para borrarla.")
    target = persona_dir(owner, name)
    existed = target.exists()
    shutil.rmtree(target, ignore_errors=True)
    _drop_dataset(owner, name)
    return existed


def delete_all_for_user(owner: str) -> None:
    shutil.rmtree(PERSONAS_DIR / _owner_folder(owner), ignore_errors=True)
    shutil.rmtree(DATASETS_DIR / _owner_folder(owner), ignore_errors=True)
