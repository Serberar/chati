"""Deteccion de hardware para el instalador (ver ROADMAP.md, "instalable
con deteccion de GPU") - un solo dato real (VRAM total via nvidia-smi) basta
para decidir que modelos recomendar sin preguntar nada tecnico al usuario.
Si no hay GPU NVIDIA (o nvidia-smi no esta disponible), cae a CPU-only sin
fallar - Ollama y el resto del sistema ya saben funcionar asi."""

import subprocess
from dataclasses import dataclass

# Cortes de VRAM (MiB) elegidos sobre datos reales de esta maquina (RTX 5060
# Laptop, 8151 MiB) y lo que sabemos que cabe: FLUX schnell/SDXL/LTX-Video 2B
# van bien en 8GB (probado en vivo toda esta sesion); variantes mas grandes
# necesitan mas margen. No es una tabla inventada de rendimiento, son los
# limites de lo que ya hemos verificado que funciona menos un margen de
# seguridad para el resto del sistema (Windows, navegador, etc).
_TIER_BREAKPOINTS = [
    (0, "cpu_only"),
    (4000, "minimo"),
    (6000, "8gb"),
    (10000, "12gb"),
    (14000, "16gb_plus"),
]


@dataclass
class GpuInfo:
    available: bool
    name: str | None
    vram_mib: int | None
    tier: str


def _run_nvidia_smi() -> str | None:
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except (subprocess.SubprocessError, OSError, FileNotFoundError):
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip().splitlines()[0]  # solo la primera GPU si hay varias


def _tier_for_vram(vram_mib: int) -> str:
    tier = "cpu_only"
    for breakpoint_mib, name in _TIER_BREAKPOINTS:
        if vram_mib >= breakpoint_mib:
            tier = name
    return tier


def detect_gpu() -> GpuInfo:
    line = _run_nvidia_smi()
    if line is None:
        return GpuInfo(available=False, name=None, vram_mib=None, tier="cpu_only")

    try:
        name, vram_str = [p.strip() for p in line.split(",", 1)]
        vram_mib = int(vram_str)
    except (ValueError, IndexError):
        return GpuInfo(available=False, name=None, vram_mib=None, tier="cpu_only")

    return GpuInfo(available=True, name=name, vram_mib=vram_mib, tier=_tier_for_vram(vram_mib))
