from __future__ import annotations

import asyncio
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path

from ..capability import CapabilityService


LFM_REPO = "LiquidAI/LFM2.5-8B-A1B-GGUF"
LFM_LICENSE_URL = "https://huggingface.co/LiquidAI/LFM2.5-8B-A1B"


@dataclass(slots=True)
class LocalModelStatus:
    state: str = "not-prepared"
    progress: float = 0.0
    message: str = "Local fallback has not been prepared."
    model_path: str | None = None
    runtime_path: str | None = None
    license_url: str = LFM_LICENSE_URL
    compute_device: str = "cpu"
    transcription_ready: bool = False
    intelligence_ready: bool = False
    error: str | None = None
    calibration: dict | None = None
    checksum_sha256: str | None = None


class LocalModelService:
    """Owns the optional model files and emits preparation progress."""

    def __init__(self, data_dir: Path, publish):
        self.root = data_dir / "Models" / "LocalFallback"
        self.status_path = self.root / "status.json"
        self.publish = publish
        self.task: asyncio.Task | None = None
        self.cancel_requested = False
        self.offline_root = self._find_offline_root()
        self._install_bundled_transcription_models()
        self.status = self._load()

    @staticmethod
    def _find_offline_root() -> Path | None:
        configured = os.environ.get("DALISTENER_OFFLINE_ASSETS")
        candidates = [Path(configured)] if configured else []
        if getattr(sys, "frozen", False):
            candidates.append(Path(sys.executable).resolve().parent / "offline-assets")
        candidates.append(Path(__file__).resolve().parents[2] / "offline-assets")
        for candidate in candidates:
            root = candidate / "LocalFallback" if (candidate / "LocalFallback").is_dir() else candidate
            if root.is_dir() and any(root.glob("*.gguf")):
                return root
        return None

    def _install_bundled_transcription_models(self) -> None:
        """Copy the writable streaming cache; keep multi-GB LFM/runtime assets in place."""
        if not self.offline_root:
            return
        bundled = self.offline_root / "Moonshine"
        destination = self.root / "Moonshine"
        if bundled.is_dir() and not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(bundled, destination)

    def _load(self) -> LocalModelStatus:
        try:
            value = LocalModelStatus(**json.loads(self.status_path.read_text(encoding="utf-8")))
            if value.model_path and not Path(value.model_path).exists():
                value.state, value.intelligence_ready = "not-prepared", False
                if self.offline_root:
                    models = list(self.offline_root.glob("*.gguf"))
                    runtime = self._find_llama_server()
                    if models and runtime:
                        value.state, value.progress = "ready", 1.0
                        value.message = "Bundled offline transcription, LFM, and llama.cpp are ready."
                        value.model_path, value.runtime_path = str(models[0]), str(runtime)
                        value.transcription_ready, value.intelligence_ready = True, True
            return value
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            if self.offline_root:
                models = list(self.offline_root.glob("*.gguf"))
                runtime = self._find_llama_server()
                if models and runtime:
                    return LocalModelStatus(
                        state="ready", progress=1.0,
                        message="Bundled offline transcription, LFM, and llama.cpp are ready.",
                        model_path=str(models[0]), runtime_path=str(runtime),
                        compute_device="cuda" if "cuda" in str(runtime).lower() else "cpu",
                        transcription_ready=(self.root / "Moonshine").exists(), intelligence_ready=True,
                    )
            return LocalModelStatus()

    def _save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(json.dumps(asdict(self.status), indent=2), encoding="utf-8")
        self.publish("local-model.updated", None, asdict(self.status))

    def public_status(self) -> dict:
        capability = CapabilityService(self.root / "capability.json").inspect()
        value = asdict(self.status)
        value["capability"] = capability.to_dict()
        measured_tabs = ((self.status.calibration or {}).get("recommended_max_tabs"))
        value["recommended_max_tabs"] = measured_tabs or (4 if capability.gpu_refinement and (capability.gpu_vram_gb or 0) >= 12 else 2 if capability.rating.value in ("fast", "live-ish") else 1)
        value["transcription_delay_seconds"] = capability.final_delay_seconds
        intelligence = (self.status.calibration or {}).get("intelligence", {})
        value["intelligence_delay_seconds"] = [intelligence.get("first_token_seconds"), intelligence.get("completion_seconds")] if intelligence.get("completion_seconds") is not None else None
        return value

    def start_prepare(self, accepted_license: bool) -> dict:
        if not accepted_license:
            raise ValueError("Accept the LFM license before downloading local fallback")
        if self.task and not self.task.done():
            return self.public_status()
        self.cancel_requested = False
        self.task = asyncio.create_task(self._prepare())
        return self.public_status()

    def cancel(self) -> None:
        self.cancel_requested = True

    async def _prepare(self) -> None:
        self.status = LocalModelStatus(state="preparing", message="Inspecting CPU, GPU, and local runtimes…")
        self._save()
        try:
            report = await asyncio.to_thread(CapabilityService(self.root / "capability.json").inspect)
            self.status.compute_device = "cuda" if report.gpu_refinement else "metal" if platform.system() == "Darwin" else "cpu"
            self.status.progress = 0.05
            self.status.message = "Preparing Moonshine and faster-whisper English models…"
            self._save()
            await asyncio.to_thread(self._prepare_transcription, report.quality_mode.value)
            self.status.transcription_ready = True
            self.status.progress = 0.35
            self._save()
            model_path = await asyncio.to_thread(self._download_lfm)
            self.status.model_path = str(model_path)
            self.status.checksum_sha256 = await asyncio.to_thread(self._sha256, model_path)
            self.status.progress = 0.96
            runtime = await asyncio.to_thread(self._prepare_llama_runtime, report)
            self.status.runtime_path = str(runtime) if runtime else None
            self.status.intelligence_ready = bool(runtime)
            self.status.message = "Calibrating local transcription and intelligence latency…"
            self._save()
            self.status.calibration = await asyncio.to_thread(self._calibrate, report.quality_mode.value, runtime, model_path)
            self.status.state = "ready" if runtime else "partial"
            self.status.progress = 1.0
            self.status.message = (
                "Local transcription and LFM intelligence are ready."
                if runtime else "Local transcription and LFM weights are ready; install llama-server to enable local meeting intelligence."
            )
            self._save()
        except asyncio.CancelledError:
            self.status.state, self.status.message = "cancelled", "Local model preparation cancelled."
            self._save()
        except Exception as exc:
            self.status.state, self.status.error = "error", str(exc)
            self.status.message = f"Local model preparation failed: {exc}"
            self._save()

    def _prepare_transcription(self, quality: str) -> None:
        from ..transcription import MoonshineEngine
        engine = MoonshineEngine(self.root / "Moonshine", quality, lambda _event: None)
        try:
            engine.prepare(lambda message: self._progress_message(message))
        finally:
            engine.close()

    def _progress_message(self, message: str) -> None:
        self.status.message = message
        self._save()

    def _repo_files(self) -> list[str]:
        request = urllib.request.Request(f"https://huggingface.co/api/models/{LFM_REPO}", headers={"User-Agent": "DaListener/0.3"})
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return [item["rfilename"] for item in payload.get("siblings", [])]

    def _download_lfm(self) -> Path:
        if self.offline_root:
            bundled = next(iter(self.offline_root.glob("*.gguf")), None)
            if bundled:
                self.status.message = f"Using bundled LFM weights: {bundled.name}"
                self._save()
                return bundled
        configured = os.environ.get("DALISTENER_LFM_GGUF_URL")
        if configured:
            url, filename = configured, configured.rsplit("/", 1)[-1]
        else:
            files = self._repo_files()
            candidates = [name for name in files if name.lower().endswith(".gguf") and "q4_k_m" in name.lower()]
            if not candidates:
                candidates = [name for name in files if name.lower().endswith(".gguf") and "q4" in name.lower()]
            if not candidates:
                raise RuntimeError("The LFM repository does not currently expose a Q4 GGUF file")
            filename = sorted(candidates, key=len)[0]
            url = f"https://huggingface.co/{LFM_REPO}/resolve/main/{filename}"
        destination = self.root / Path(filename).name
        if destination.exists() and destination.stat().st_size > 0:
            self.status.message = f"Using existing LFM weights: {destination.name}"
            self._save()
            return destination
        partial = destination.with_suffix(destination.suffix + ".part")
        existing = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "DaListener/0.3"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("ab" if existing else "wb") as output:
            total = existing + int(response.headers.get("Content-Length", "0"))
            downloaded = existing
            started = time.monotonic()
            while True:
                if self.cancel_requested:
                    raise asyncio.CancelledError()
                block = response.read(1024 * 1024)
                if not block:
                    break
                output.write(block)
                downloaded += len(block)
                elapsed = max(time.monotonic() - started, 0.1)
                self.status.progress = 0.35 + (downloaded / total * 0.60 if total else 0)
                self.status.message = f"Downloading LFM: {downloaded / 1024**3:.2f} GB · {downloaded / elapsed / 1024**2:.1f} MB/s"
                self._save()
        partial.replace(destination)
        return destination

    def _find_llama_server(self) -> Path | None:
        configured = os.environ.get("DALISTENER_LLAMA_SERVER")
        found = configured or shutil.which("llama-server") or shutil.which("llama-server.exe")
        if found:
            return Path(found)
        candidates = list((self.root / "LlamaCpp").glob("**/llama-server.exe")) if platform.system() == "Windows" else []
        if self.offline_root and platform.system() == "Windows":
            offline = list((self.offline_root / "LlamaCpp").glob("**/llama-server.exe"))
            prefer_cuda = CapabilityService(self.root / "capability.json").inspect().gpu_refinement
            preferred = [path for path in offline if ("cuda" in str(path).lower()) == prefer_cuda]
            candidates = preferred + offline + candidates
        return candidates[0] if candidates else None

    def _prepare_llama_runtime(self, report) -> Path | None:
        existing = self._find_llama_server()
        if existing:
            return existing
        if platform.system() != "Windows":
            return None

        self.status.message = "Finding the official llama.cpp Windows runtime…"
        self._save()
        request = urllib.request.Request(
            "https://api.github.com/repos/ggml-org/llama.cpp/releases/latest",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "DaListener/0.3"},
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            release = json.loads(response.read().decode("utf-8"))
        assets = release.get("assets", [])
        use_cuda = bool(report.gpu_refinement)
        variant = "cuda-12.4" if use_cuda else "cpu"
        required = [asset for asset in assets if asset.get("name", "").startswith("llama-") and asset.get("name", "").endswith(f"bin-win-{variant}-x64.zip")]
        if use_cuda:
            required += [asset for asset in assets if asset.get("name", "").startswith("cudart-") and asset.get("name", "").endswith("win-cuda-12.4-x64.zip")]
        if not required:
            raise RuntimeError(f"The latest llama.cpp release has no Windows x64 {variant} runtime")

        tag = str(release.get("tag_name") or "latest").replace("/", "-")
        target = self.root / "LlamaCpp" / tag / variant
        downloads = self.root / "Downloads"
        downloads.mkdir(parents=True, exist_ok=True)
        target.mkdir(parents=True, exist_ok=True)
        total_size = max(1, sum(int(asset.get("size") or 0) for asset in required))
        completed = 0
        for asset in required:
            name = str(asset["name"])
            archive = downloads / name
            self._download_release_asset(asset, archive, completed, total_size)
            self._extract_zip_safely(archive, target)
            completed += int(asset.get("size") or archive.stat().st_size)
        servers = list(target.glob("**/llama-server.exe"))
        if not servers:
            raise RuntimeError("llama.cpp downloaded successfully but llama-server.exe was not found")
        return servers[0]

    def _download_release_asset(self, asset: dict, destination: Path, completed: int, total_size: int) -> None:
        expected = int(asset.get("size") or 0)
        if destination.exists() and expected and destination.stat().st_size == expected:
            digest = str(asset.get("digest") or "")
            if not digest.startswith("sha256:") or self._sha256(destination) == digest.removeprefix("sha256:"):
                return
            destination.unlink(missing_ok=True)
        partial = destination.with_suffix(destination.suffix + ".part")
        existing = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "DaListener/0.3"}
        if existing:
            headers["Range"] = f"bytes={existing}-"
        request = urllib.request.Request(str(asset["browser_download_url"]), headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            append = existing > 0 and getattr(response, "status", 200) == 206
            if not append:
                existing = 0
            with partial.open("ab" if append else "wb") as output:
                downloaded = existing
                started = time.monotonic()
                while True:
                    if self.cancel_requested:
                        raise asyncio.CancelledError()
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    output.write(block)
                    downloaded += len(block)
                    elapsed = max(time.monotonic() - started, 0.1)
                    overall = min(1.0, (completed + downloaded) / total_size)
                    self.status.progress = 0.96 + overall * 0.03
                    self.status.message = (
                        f"Downloading llama.cpp {destination.name}: {downloaded / 1024**2:.0f} MB · "
                        f"{downloaded / elapsed / 1024**2:.1f} MB/s"
                    )
                    self._save()
        if expected and partial.stat().st_size != expected:
            raise RuntimeError(f"Incomplete llama.cpp download: {destination.name}")
        partial.replace(destination)
        digest = str(asset.get("digest") or "")
        if digest.startswith("sha256:") and self._sha256(destination) != digest.removeprefix("sha256:"):
            destination.unlink(missing_ok=True)
            raise RuntimeError(f"llama.cpp checksum verification failed: {destination.name}")

    @staticmethod
    def _extract_zip_safely(archive: Path, target: Path) -> None:
        resolved_target = target.resolve()
        with zipfile.ZipFile(archive) as package:
            for member in package.infolist():
                destination = (target / member.filename).resolve()
                if not destination.is_relative_to(resolved_target):
                    raise RuntimeError(f"Unsafe path in llama.cpp archive: {member.filename}")
            package.extractall(target)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _calibrate(self, quality: str, runtime: Path | None, model_path: Path) -> dict:
        from ..transcription import MoonshineEngine
        stt: dict[str, dict] = {}
        engine = MoonshineEngine(self.root / "Moonshine", quality, lambda _event: None)
        try:
            engine.prepare()
            for streams in (1, 2, 4):
                started = time.perf_counter()
                seconds = engine.calibrate(streams)
                elapsed = time.perf_counter() - started
                rtf = round(elapsed / max(seconds, 0.001), 3)
                stt[str(streams)] = {"real_time_factor": rtf, "elapsed_seconds": round(elapsed, 2)}
        finally:
            engine.close()
        eligible = [count for count in (1, 2, 4) if stt[str(count)]["real_time_factor"] <= 0.70]
        result = {"transcription": stt, "recommended_max_tabs": max(eligible, default=1)}
        if runtime:
            from .local_provider import LocalLFMService
            local = LocalLFMService(model_path, runtime)
            fixture = (
                "[0s] Vladimir, please verify the migration plan.\n"
                "[8s] We will keep the existing schema and run the cutover Friday.\n"
                "[18s] Arjun owns the rollback checklist."
            )
            started = time.perf_counter()
            try:
                local.complete("Summarize only this transcript in one sentence:\n" + fixture)
                total = round(time.perf_counter() - started, 2)
                rating = "fast" if total < 15 else "live-ish" if total < 45 else "delayed" if total <= 120 else "not-recommended"
                result["intelligence"] = {"first_token_seconds": total, "completion_seconds": total, "rating": rating}
            except Exception as exc:
                result["intelligence"] = {"rating": "not-recommended", "error": str(exc)}
            finally:
                local.close()
        return result
