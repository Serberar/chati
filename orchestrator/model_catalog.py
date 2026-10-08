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

from dataclasses import dataclass, replace


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
    # Archivos que el modelo necesita ademas del suyo: (url, ruta dentro de
    # models/img, sha256). Sin ellos no funcionaba nada de imagen en un equipo
    # limpio: el instalador bajaba el unet de FLUX pero no sus codificadores ni
    # su VAE (2026-10-01). Tamaños comprobados contra los que funcionan en este equipo.
    extra_files: tuple[tuple[str, str, str], ...] = ()
    # Huella del archivo de download_url. Las URLs van fijadas a un commit de
    # Hugging Face (no a "main", que el autor puede cambiar) y lo descargado se
    # comprueba contra esta huella: un archivo cambiado o manipulado por el
    # camino no se instala (auditoria 2026-10-05; huellas comprobadas contra los
    # archivos que funcionan en este equipo).
    sha256: str | None = None


_CLIP_L = ("https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/6af2a98e3f615bdfa612fbd85da93d1ed5f69ef5/clip_l.safetensors",
           "text_encoders/clip_l.safetensors",
           "660c6f5b1abae9dc498ac2d21e1347d2abdb0cf6c0c0c8576cd796491d9a6cdd")
_T5XXL = ("https://huggingface.co/comfyanonymous/flux_text_encoders/resolve/6af2a98e3f615bdfa612fbd85da93d1ed5f69ef5/t5xxl_fp8_e4m3fn.safetensors",
          "text_encoders/t5xxl_fp8_e4m3fn.safetensors",
          "7d330da4816157540d6bb7838bf63a0f02f573fc48ca4d8de34bb0cbfd514f09")
_FLUX_VAE = ("https://huggingface.co/Kijai/flux-fp8/resolve/2ef6f85f9f4b94634f995ba0bfa84fd3ab865c1d/flux-vae-bf16.safetensors",
             "vae/flux-vae-bf16.safetensors",
             "0c0c8ac49850d6bd174107d4001685f4f6c876768a1c84dde751f2aed69f46ce")
_MODNET = ("https://huggingface.co/Xenova/modnet/resolve/fa2fa546052fba4c08921230a26cc69a333fca12/onnx/model.onnx", "matting/modnet.onnx",
           "07c308cf0fc7e6e8b2065a12ed7fc07e1de8febb7dc7839d7b7f15dd66584df9")
_REALESRGAN = ("https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth",
               "upscale_models/RealESRGAN_x4plus.pth",
               "4fa0d38905f75ac06eb49a7951b426670021be3018265fd191d2125df9d682f1")

# Lo que usa el editor de fotos ademas del modelo que edita (photo_edit.py,
# face_swap.py): reconocer la ropa, las manos, rellenar huecos y poner los
# rasgos de una cara. Mismo editor con Qwen o con Kontext (2026-10-08).
_QWEN_TEXT_ENCODER = (
    "https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/1f12b17be14c89b026c51a91d67c32f84bb047bc/split_files/text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors",
    "text_encoders/qwen_2.5_vl_7b_fp8_scaled.safetensors",
    "cb5636d852a0ea6a9075ab1bef496c0db7aef13c02350571e388aea959c5c0b4")
_QWEN_VAE = (
    "https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/1f12b17be14c89b026c51a91d67c32f84bb047bc/split_files/vae/qwen_image_vae.safetensors",
    "vae/qwen_image_vae.safetensors",
    "a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f")
_QWEN_LIGHTNING = (
    "https://huggingface.co/lightx2v/Qwen-Image-Edit-2511-Lightning/resolve/d74eba145674fd7e31b949324e148e21e7118abd/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors",
    "loras/qwen/Qwen-Image-Edit-2511-Lightning-4steps-V1.0-bf16.safetensors",
    "22226e8d05d354bb356627d428809f5afd7819399b077238a2b70a82883a904f")
_CLOTHES_PARSER = (
    "https://huggingface.co/mattmdjaga/segformer_b2_clothes/resolve/584abc1e1d260e23c0fc627c5217a09b2b461046/onnx/model.onnx",
    "parsing/segformer_b2_clothes.onnx",
    "a93a8dac171b5c1fcc53632a8bfc180bfd9759ea69a3e207451bb07f76add54f")
_HANDS = (
    "https://huggingface.co/opencv/palm_detection_mediapipe/resolve/233e619dcea1759bf6de707b9b904fe30881ea55/palm_detection_mediapipe_2023feb.onnx",
    "hands/palm_detection_mediapipe_2023feb.onnx",
    "78ff51c38496b7fc8b8ebdb6cc8c1abb02fa6c38427c6848254cdaba57fcce7c")
_LAMA = (
    "https://huggingface.co/opencv/inpainting_lama/resolve/aee6d22f0a13e5e35af1c9a1c3afd62841fc6f3f/inpainting_lama_2025jan.onnx",
    "inpaint/inpainting_lama_2025jan.onnx",
    "7df918ac3921d3daf0aae1d219776cf0dc4e4935f035af81841b40adcf74fdf2")
_INSWAPPER = (
    "https://github.com/facefusion/facefusion-assets/releases/download/models-3.0.0/inswapper_128.onnx",
    "faceswap/inswapper_128.onnx",
    "a290273ed497312095dac48cdef20feec9d5208298223dd01288ab202b54bea7")
_GFPGAN = (
    "https://github.com/facefusion/facefusion-assets/releases/download/models-3.0.0/gfpgan_1.4.onnx",
    "faceswap/gfpgan_1.4.onnx",
    "accc4757b26bdb89b32b4d3500d4f79c9dff97c1dd7c7104bf9dcb95e3311385")
_ARCFACE = (
    "https://github.com/facefusion/facefusion-assets/releases/download/models-3.0.0/arcface_w600k_r50.onnx",
    "faceswap/arcface_w600k_r50.onnx",
    "f1f79dc3b0b79a69f94799af1fffebff09fbd78fd96a275fd8f0cbbea23270d1")
_EDITOR_HELPERS = (_MODNET, _REALESRGAN, _CLOTHES_PARSER, _HANDS, _LAMA, _INSWAPPER, _GFPGAN, _ARCFACE)


# Mismo catalogo para todos los niveles con GPU (minimo/8gb/12gb/16gb_plus) -
# todo lo de aqui ya esta comprobado que cabe en 8GB, asi que no hay motivo
# para no ofrecerlo tambien en equipos con mas VRAM. cpu_only recibe un
# subconjunto mas ligero (sin video, que es lo mas pesado de generar en CPU).
_BASE_CATALOG: list[CatalogEntry] = [
    CatalogEntry(
        id="texto-rapido", modality="texto", label="Chat rapido",
        description="Respuestas breves, conversacion general - el modelo por defecto del router.",
        ollama_model="qwen3:8b",  # el mismo que el agente rapido: uno solo para todo
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
        download_url="https://huggingface.co/city96/FLUX.1-schnell-gguf/resolve/f495746ed9c5efcf4661f53ef05401dceadc17d2/flux1-schnell-Q4_K_S.gguf",
        filename="flux1-schnell-Q4_K_S.gguf",
        sha256="4fd16477b3a5296d0cf722c4b92a9fd7f30d09ac7495826e4465d8de9c9fd973",
        extra_files=(_CLIP_L, _T5XXL, _FLUX_VAE),
    ),
    CatalogEntry(
        id="imagen-editar-qwen", modality="imagen", label="Editar fotos (Qwen-Image-Edit)",
        description="Edita tus fotos con una frase: ropa, fondo, postura, mirar a camara... manteniendo las caras. "
                    "Unos 24 GB.",
        architecture="qwen",
        download_url="https://huggingface.co/unsloth/Qwen-Image-Edit-2511-GGUF/resolve/0d33d9692b4b26212297240d87b0d4719aa4fd06/qwen-image-edit-2511-Q4_K_S.gguf",
        filename="qwen-image-edit-2511-Q4_K_S.gguf",
        sha256="df952ef0d2b46463bd95d9afbb78e045ec5412316f453a7ad5a3d7bcbb111b72",
        extra_files=(_QWEN_TEXT_ENCODER, _QWEN_VAE, _QWEN_LIGHTNING) + _EDITOR_HELPERS,
    ),
    CatalogEntry(
        id="imagen-editar", modality="imagen", label="Editar fotos con FLUX Kontext (respaldo)",
        description="El editor anterior: solo hace falta si no se instala Qwen-Image-Edit.",
        architecture="flux_kontext",
        download_url="https://huggingface.co/QuantStack/FLUX.1-Kontext-dev-GGUF/resolve/2d083027732f81e5548620b57ac47da22d30aa5a/flux1-kontext-dev-Q4_K_S.gguf",
        filename="flux1-kontext-dev-Q4_K_S.gguf",
        sha256="cc22ff7a2debb02e63765fa53af8c5ae0b6883b462d0601b9b55f51a15cdd6da",
        extra_files=(_CLIP_L, _T5XXL, _FLUX_VAE) + _EDITOR_HELPERS,
        recommended_by_default=False,
    ),
    CatalogEntry(
        id="imagen-sdxl", modality="imagen", label="SDXL base",
        description="Necesario para preservar caras (FaceID), ControlNet, y editar zonas de una imagen (inpaint).",
        architecture="sdxl",
        download_url="https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/462165984030d82259a11f4367a4eed129e94a7b/sd_xl_base_1.0.safetensors",
        filename="sd_xl_base_1.0.safetensors",
        sha256="31e35c80fc4829d14f90153f4c74cd59c90b779f6afe05a74cd6120b893f7e5b",
    ),
    CatalogEntry(
        id="video-ltxv", modality="video", label="LTX-Video (2B, destilado)",
        description="El generador de video recomendado por defecto - rapido para su tamaño.",
        architecture="ltxv",
        download_url="https://huggingface.co/Lightricks/LTX-Video/resolve/8984fa25007f376c1a299016d0957a37a2f797bb/ltxv-2b-0.9.8-distilled-fp8.safetensors",
        filename="ltxv-2b-0.9.8-distilled-fp8.safetensors",
        sha256="d6d8fa8ed3a98346787c2503ac80fb5d7cebcf80e356b79a2ba361fbadf97e15",
        extra_files=(_T5XXL,),
    ),
    CatalogEntry(
        id="voz-piper", modality="voz", label="Voz en español (Piper)",
        description="Necesario para que Chati responda hablando.",
        download_url="https://huggingface.co/rhasspy/piper-voices/resolve/c10ece1aade47bb51c153c893d14e5bf8e5b7117/es/es_ES/davefx/medium/es_ES-davefx-medium.onnx",
        filename="es_ES-davefx-medium.onnx",
        sha256="6658b03b1a6c316ee4c265a9896abc1393353c2d9e1bca7d66c2c442e222a917",
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
