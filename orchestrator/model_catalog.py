"""Catalogo de modelos recomendados por nivel de hardware (ver
hardware_detect.py) - para el instalador: "hemos detectado tu GPU, te
recomendamos estos". Cada entrada es un modelo YA probado en este proyecto
(nada inventado) - texto via Ollama (nombre real de su registro oficial),
imagen/video con la ruta de descarga exacta que ya usa model_registry.py.

Deliberadamente conservador: solo hay entradas para lo que ya esta
verificado funcionando en este sistema (todo cabe en 8GB, probado en vivo).
Anadir un modelo mas pesado para 12gb/16gb_plus es cuestion de investigarlo
y verificarlo primero (Civitai/HuggingFace API, nunca una URL inventada) y
sumarlo aqui - la estructura ya esta lista para eso."""

from dataclasses import dataclass, field, replace


@dataclass
class CatalogEntry:
    id: str
    modality: str  # "texto" | "imagen" | "video" | "voz"
    label: str
    description: str
    # Para texto: nombre real en el registro de Ollama ("ollama pull <esto>").
    # Para imagen/video: arquitectura + URL de descarga verificada.
    ollama_model: str | None = None
    architecture: str | None = None
    download_url: str | None = None
    filename: str | None = None
    recommended_by_default: bool = True


# Mismo catalogo para todos los niveles con GPU (minimo/8gb/12gb/16gb_plus) -
# todo lo de aqui ya esta comprobado que cabe en 8GB, asi que no hay motivo
# para no ofrecerlo tambien en equipos con mas VRAM. cpu_only recibe un
# subconjunto mas ligero (sin video, que es lo mas pesado de generar en CPU).
_BASE_CATALOG: list[CatalogEntry] = [
    CatalogEntry(
        id="texto-rapido", modality="texto", label="Chat rapido",
        description="Respuestas breves, conversacion general - el modelo por defecto del router.",
        ollama_model="qwen2.5:7b",
    ),
    CatalogEntry(
        id="texto-calidad", modality="texto", label="Chat de mejor calidad",
        description="Mas lento, menos probable que invente datos - programacion y respuestas mas elaboradas.",
        ollama_model="qwen3-coder:30b-cpu",
    ),
    CatalogEntry(
        id="texto-vision", modality="texto", label="Vision (comentar fotos)",
        description="Necesario para poder subir una foto en el chat y que la describa.",
        ollama_model="qwen2.5vl:7b-gpu",  # cpu_only recibe la variante -cpu
    ),
    CatalogEntry(
        id="agente-rapido", modality="texto", label="Agente (tareas en el ordenador)",
        description="El modelo del modo Agente: crea, mueve, ordena y renombra archivos por ti.",
        ollama_model="qwen3:8b",
    ),
    CatalogEntry(
        id="imagen-flux", modality="imagen", label="FLUX (rapido, buena calidad general)",
        description="El generador de imagen recomendado por defecto.",
        architecture="flux",
        download_url="https://huggingface.co/city96/FLUX.1-schnell-gguf/resolve/main/flux1-schnell-Q4_K_S.gguf",
        filename="flux1-schnell-Q4_K_S.gguf",
    ),
    CatalogEntry(
        id="imagen-sdxl", modality="imagen", label="SDXL base",
        description="Necesario para preservar caras (FaceID), ControlNet, y editar zonas de una imagen (inpaint).",
        architecture="sdxl",
        download_url="https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors",
        filename="sd_xl_base_1.0.safetensors",
    ),
    CatalogEntry(
        id="video-ltxv", modality="video", label="LTX-Video (2B, destilado)",
        description="El generador de video recomendado por defecto - rapido para su tamaño.",
        architecture="ltxv",
        download_url="https://huggingface.co/Lightricks/LTX-Video/resolve/main/ltxv-2b-0.9.8-distilled-fp8.safetensors",
        filename="ltxv-2b-0.9.8-distilled-fp8.safetensors",
    ),
    CatalogEntry(
        id="voz-piper", modality="voz", label="Voz en español (Piper)",
        description="Necesario para que Chati responda hablando.",
        download_url="https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_ES/davefx/medium/es_ES-davefx-medium.onnx",
        filename="es_ES-davefx-medium.onnx",
    ),
]

# STT (faster-whisper) no tiene entrada aqui: se descarga solo la primera vez
# que se usa (ver agents/voice_agent.py), no hace falta que el instalador
# haga nada por el.

GPU_SUFFIX = "-gpu"  # variante forzada a GPU: sin GPU se ofrece la "-cpu"
_CPU_ONLY_EXCLUDED_MODALITIES = {"video"}  # lo mas lento de generar en CPU puro


def catalog_for_tier(tier: str) -> list[CatalogEntry]:
    if tier == "cpu_only":
        return [replace(e, ollama_model=e.ollama_model.removesuffix(GPU_SUFFIX) + "-cpu")
                if e.ollama_model and e.ollama_model.endswith(GPU_SUFFIX) else e
                for e in _BASE_CATALOG if e.modality not in _CPU_ONLY_EXCLUDED_MODALITIES]
    return list(_BASE_CATALOG)
