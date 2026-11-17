"""ONNX Runtime and GPU helpers shared by face scanning (face_scan.py) and search by description (clip_search.py).

Only the standard library is imported at the top, so this module loads even when ONNX Runtime is not installed.
"""
from __future__ import annotations

import importlib.metadata as importlib_metadata
import shutil
import subprocess

GPU_PROVIDER = "CUDAExecutionProvider"  # NVIDIA
GPU_SETUP_STEPS = (
    "To use your NVIDIA GPU, swap in the GPU build of ONNX Runtime in this same virtual environment:\n"
    "    pip uninstall -y onnxruntime onnxruntime-gpu\n"
    '    pip install "onnxruntime-gpu[cuda,cudnn]"\n'
    "(InsightFace installs the CPU build, and the two builds cannot be installed together.)\n"
    "Then check it with: python sort_photos.py gpucheck"
)
GPU_TROUBLE = (
    "Common causes: an out-of-date NVIDIA driver; the CUDA/cuDNN libraries missing (install the GPU build "
    'with the extras: pip install "onnxruntime-gpu[cuda,cudnn]"); or a GPU too old for this build. '
    "The current onnxruntime-gpu targets CUDA 13, which needs a recent driver and a GTX 16-series/RTX 20-series "
    'or newer GPU. For an older GPU try the CUDA 12 build: pip install "onnxruntime-gpu[cuda,cudnn]==1.26.0".'
)
PROVIDER_LABELS = {
    "CUDAExecutionProvider": "NVIDIA GPU (CUDA)",
    "DmlExecutionProvider": "GPU (DirectML)",
    "CPUExecutionProvider": "CPU",
}


def label(provider: str) -> str:
    return PROVIDER_LABELS.get(provider, provider)


def hardware_mode(args) -> str:
    """'cpu' (--cpu), 'gpu' (--gpu: NVIDIA required) or 'auto' (use a GPU if one is set up)."""
    if getattr(args, "cpu", False):
        return "cpu"
    return "gpu" if getattr(args, "gpu", False) else "auto"


def pick_providers(mode: str = "auto") -> tuple[list[str], bool]:
    """Choose the ONNX Runtime providers. Returns (providers, uses_gpu).

    Raises RuntimeError in 'gpu' mode if this ONNX Runtime has no NVIDIA CUDA support.
    """
    import onnxruntime as ort

    available = ort.get_available_providers()
    if mode == "cpu":
        return ["CPUExecutionProvider"], False
    if GPU_PROVIDER in available:
        return [GPU_PROVIDER, "CPUExecutionProvider"], True
    if mode == "gpu":
        raise RuntimeError(
            "--gpu was given, but the installed ONNX Runtime has no NVIDIA CUDA support "
            f"(it offers: {', '.join(available)}).\n{GPU_SETUP_STEPS}"
        )
    if "DmlExecutionProvider" in available:  # DirectML build: any DirectX 12 GPU
        return ["DmlExecutionProvider", "CPUExecutionProvider"], True
    return ["CPUExecutionProvider"], False


def preload_cuda_libraries() -> str | None:
    """Ask ONNX Runtime to load the CUDA/cuDNN libraries (including ones pip installed). Returns a problem or None."""
    import onnxruntime as ort

    preload = getattr(ort, "preload_dlls", None)
    if preload is None:
        return None
    try:
        preload()
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    return None


def installed_onnxruntime_packages() -> dict[str, str]:
    found = {}
    for name in ("onnxruntime", "onnxruntime-gpu", "onnxruntime-directml"):
        try:
            found[name] = importlib_metadata.version(name)
        except importlib_metadata.PackageNotFoundError:
            pass
    return found


def nvidia_gpu_summary() -> str | None:
    """GPU name, driver and memory from nvidia-smi, or None if it is not available."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    try:
        result = subprocess.run(
            [exe, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return "; ".join(lines) if result.returncode == 0 and lines else None
