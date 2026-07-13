from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import platform
import subprocess
import sys
import time
import ctypes
from dataclasses import replace
from pathlib import Path
from typing import Callable

import psutil

from .models import CapabilityReport, PerformanceRating, QualityMode


APP_VERSION = "0.2.0a4"
MODEL_VERSION = "moonshine-whisper-en-tl-auto-2026-v1"
_CUDA_DLL_DIR_HANDLES: list[object] = []
_CUDA_REGISTERED_DIRS: set[str] = set()


def _register_cuda_runtime_dirs() -> list[Path]:
    """Make system and pip-installed NVIDIA runtime DLLs discoverable."""
    if platform.system() != "Windows" or not hasattr(os, "add_dll_directory"):
        return []

    candidates: list[Path] = []
    for variable in ("CUDA_PATH", "CUDA_PATH_V12_9", "CUDNN_PATH"):
        if os.environ.get(variable):
            root = Path(os.environ[variable])
            candidates.extend((root / "bin", root / "bin" / "x64"))

    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    candidates.extend((
        site_packages / "nvidia" / "cublas" / "bin",
        site_packages / "nvidia" / "cudnn" / "bin",
        site_packages / "nvidia" / "cuda_nvrtc" / "bin",
    ))

    registered: list[Path] = []
    for directory in candidates:
        key = str(directory.resolve()).lower() if directory.exists() else ""
        if not key or key in _CUDA_REGISTERED_DIRS:
            continue
        try:
            _CUDA_DLL_DIR_HANDLES.append(os.add_dll_directory(str(directory)))
            _CUDA_REGISTERED_DIRS.add(key)
            registered.append(directory)
        except OSError:
            continue
    return registered


def _run_json(command: list[str]) -> object | None:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    return None


def _cpu_name() -> str:
    if platform.system() == "Windows":
        try:
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
            ) as key:
                value, _ = winreg.QueryValueEx(key, "ProcessorNameString")
                if str(value).strip():
                    return str(value).strip()
        except OSError:
            pass
        value = _run_json([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_Processor | Select-Object -First 1 -ExpandProperty Name | ConvertTo-Json",
        ])
        if isinstance(value, str) and value.strip():
            return value.strip()
    return platform.processor() or "Unknown CPU"


def _gpu_info() -> tuple[str | None, float | None]:
    # nvidia-smi reports dedicated VRAM accurately; WMI AdapterRAM is commonly
    # truncated on GPUs with more than 4 GB.
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode == 0 and result.stdout.strip():
            name, memory = result.stdout.splitlines()[0].rsplit(",", 1)
            return name.strip(), round(float(memory.strip()) / 1024, 1)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if platform.system() == "Windows":
        value = _run_json([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterRAM | ConvertTo-Json -Compress",
        ])
        rows = value if isinstance(value, list) else [value] if isinstance(value, dict) else []
        if rows:
            preferred = next((r for r in rows if "nvidia" in str(r.get("Name", "")).lower()), rows[0])
            raw = preferred.get("AdapterRAM") or 0
            return str(preferred.get("Name") or "Unknown GPU"), round(float(raw) / 1024**3, 1)
    return None, None


def _providers(gpu_name: str | None) -> list[str]:
    providers = ["CPUExecutionProvider"]
    try:
        import onnxruntime as ort
        providers = list(ort.get_available_providers())
    except ImportError:
        pass
    if gpu_name and "nvidia" in gpu_name.lower() and "CUDAExecutionProvider" not in providers:
        providers.append("CUDA (driver detected; runtime unavailable)")
    return providers


def _cuda_runtime_ready() -> bool:
    _register_cuda_runtime_dirs()
    if platform.system() == "Windows":
        required = ("cublas64_12.dll", "cudnn64_9.dll")
    else:
        required = ("libcublas.so.12", "libcudnn.so.9")
    handles = []
    try:
        for library in required:
            handles.append(ctypes.CDLL(library))
        return True
    except OSError:
        return False


class CapabilityService:
    def __init__(self, cache_path: Path):
        self.cache_path = cache_path

    def inspect(self) -> CapabilityReport:
        memory = psutil.virtual_memory()
        cpu = _cpu_name()
        gpu_name, gpu_vram = _gpu_info()
        physical = psutil.cpu_count(logical=False) or max(1, (os.cpu_count() or 2) // 2)
        logical = psutil.cpu_count(logical=True) or os.cpu_count() or 1
        total_ram = round(memory.total / 1024**3, 1)
        available_ram = round(memory.available / 1024**3, 1)
        providers = _providers(gpu_name)
        finalizer_installed = importlib.util.find_spec("faster_whisper") is not None
        cuda_runtime_ready = _cuda_runtime_ready() if finalizer_installed else False
        raw_fingerprint = "|".join([
            platform.platform(), cpu, str(physical), str(logical), str(round(total_ram)),
            str(gpu_name), str(gpu_vram), ",".join(providers), str(finalizer_installed),
            str(cuda_runtime_ready),
            APP_VERSION, MODEL_VERSION,
        ])
        fingerprint = hashlib.sha256(raw_fingerprint.encode()).hexdigest()[:20]

        cached = self.load_cached(fingerprint)
        if cached:
            return cached

        reasons: list[str] = []
        nvidia_refinement = bool(
            gpu_name and "nvidia" in gpu_name.lower() and (gpu_vram or 0) >= 4 and total_ram >= 8
            and finalizer_installed and cuda_runtime_ready
        )
        if total_ram < 4 or physical < 2:
            quality = QualityMode.EFFICIENT
            rating = PerformanceRating.NOT_RECOMMENDED
            model = "Moonshine Small Streaming + Whisper tiny multilingual" if finalizer_installed else "Moonshine Small Streaming"
            delay = (2.0, 5.0)
            final = (3.0, 7.0)
            estimated_memory = 1050 if finalizer_installed else 700
            reasons.append("Less than 4 GB RAM or fewer than 2 physical CPU cores")
        elif total_ram < 8 or physical < 4:
            quality = QualityMode.EFFICIENT
            rating = PerformanceRating.DELAYED
            model = "Moonshine Small Streaming + Whisper tiny multilingual" if finalizer_installed else "Moonshine Small Streaming"
            delay = (1.5, 3.5)
            final = (2.0, 5.0)
            estimated_memory = 1050 if finalizer_installed else 700
            reasons.append("Balanced mode needs at least 8 GB RAM and 4 physical CPU cores")
        elif nvidia_refinement:
            quality = QualityMode.BEST
            rating = PerformanceRating.FAST
            model = "Moonshine Medium Streaming + Whisper large-v3-turbo"
            delay = (0.3, 1.0)
            final = (0.8, 2.5)
            estimated_memory = 2900
        else:
            quality = QualityMode.BALANCED
            rating = PerformanceRating.LIVEISH if physical < 6 else PerformanceRating.FAST
            model = "Moonshine Medium Streaming + Whisper small multilingual" if finalizer_installed else "Moonshine Medium Streaming"
            delay = (0.5, 2.0) if physical < 6 else (0.3, 1.0)
            final = (1.5, 4.0) if finalizer_installed else ((1.0, 2.5) if physical < 6 else (0.5, 1.8))
            estimated_memory = 1900 if finalizer_installed else 1100
            if gpu_name and "nvidia" in gpu_name.lower() and (gpu_vram or 0) < 4:
                reasons.append("NVIDIA GPU has less than 4 GB VRAM; GPU finalization disabled")
            elif gpu_name and "nvidia" in gpu_name.lower() and (gpu_vram or 0) >= 4 and not finalizer_installed:
                reasons.append("GPU refinement is supported by this PC but the optional Best-mode runtime is not installed")
            elif gpu_name and "nvidia" in gpu_name.lower() and (gpu_vram or 0) >= 4 and not cuda_runtime_ready:
                reasons.append("GPU refinement is supported by this PC but CUDA 12 cuBLAS and cuDNN 9 are unavailable")

        return CapabilityReport(
            fingerprint=fingerprint,
            os_name=platform.platform(), cpu_name=cpu, architecture=platform.machine(),
            physical_cores=physical, logical_cores=logical,
            total_ram_gb=total_ram, available_ram_gb=available_ram,
            gpu_name=gpu_name, gpu_vram_gb=gpu_vram, providers=providers,
            quality_mode=quality, model_name=model, rating=rating,
            draft_delay_seconds=delay, final_delay_seconds=final,
            estimated_memory_mb=estimated_memory, gpu_refinement=nvidia_refinement,
            downgrade_reasons=reasons,
        )

    def verify(self, report: CapabilityReport, benchmark: Callable[[], float]) -> CapabilityReport:
        started = time.perf_counter()
        audio_seconds = float(benchmark())
        elapsed = max(time.perf_counter() - started, 0.001)
        rtf = elapsed / max(audio_seconds, 0.001)
        if rtf <= 0.35:
            rating, delay, final = PerformanceRating.FAST, (0.3, 1.0), (0.5, 1.8)
        elif rtf <= 0.75:
            rating, delay, final = PerformanceRating.LIVEISH, (0.8, 2.0), (1.2, 3.0)
        elif rtf <= 1.0:
            rating, delay, final = PerformanceRating.DELAYED, (2.0, 5.0), (3.0, 7.0)
        else:
            rating, delay, final = PerformanceRating.NOT_RECOMMENDED, (5.0, 10.0), (7.0, 15.0)
        verified = replace(
            report, rating=rating, draft_delay_seconds=delay, final_delay_seconds=final,
            verified=True, real_time_factor=round(rtf, 3),
        )
        self.save(verified)
        return verified

    def save(self, report: CapabilityReport) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")

    def load_cached(self, fingerprint: str) -> CapabilityReport | None:
        try:
            report = CapabilityReport.from_dict(json.loads(self.cache_path.read_text(encoding="utf-8")))
            return report if report.fingerprint == fingerprint else None
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
