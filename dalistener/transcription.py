from __future__ import annotations

import threading
import time
import uuid
import os
import sys
from dataclasses import dataclass
from concurrent.futures import Future, ThreadPoolExecutor, wait
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .models import AudioFrame, SourceKind, Stability, TranscriptEvent, TranscriptionLanguage


TranscriptCallback = Callable[[TranscriptEvent], None]
ProgressCallback = Callable[[str], None]
_DLL_DIR_HANDLES: list[object] = []


@dataclass(frozen=True, slots=True)
class FinalizationResult:
    text: str
    language: str
    probability: float


def _register_windows_runtime_dirs() -> None:
    if os.name != "nt" or not hasattr(os, "add_dll_directory") or _DLL_DIR_HANDLES:
        return
    candidates = [
        Path(sys.executable).parent,
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "System32" / "downlevel",
    ]
    try:
        import PySide6
        candidates.append(Path(PySide6.__file__).parent)
    except ImportError:
        pass
    try:
        from .capability import _register_cuda_runtime_dirs
        _register_cuda_runtime_dirs()
    except ImportError:
        pass
    for directory in candidates:
        if directory.exists():
            _DLL_DIR_HANDLES.append(os.add_dll_directory(str(directory)))


class MoonshineEngine:
    """One Moonshine model with independent microphone and system streams."""

    def __init__(self, model_dir: Path, quality: str, callback: TranscriptCallback, enable_finalizer: bool = True):
        self.model_dir = model_dir
        self.quality = quality
        self.callback = callback
        self.transcriber = None
        self.streams: dict[SourceKind, object] = {}
        self.listeners: list[object] = []
        self.session_id = ""
        self.started_at_ms = 0
        self.revisions: dict[tuple[SourceKind, str], int] = defaultdict(int)
        self.lock = threading.RLock()
        self.finalizer = None
        self.finalizer_error: str | None = None
        self.finalizer_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="whisper-finalizer")
        self.pending_finalizers: dict[Future, TranscriptEvent] = {}
        self.language = TranscriptionLanguage.AUTO
        self.enable_finalizer = enable_finalizer
        if quality == "best":
            self.finalizer_model_name = "large-v3-turbo"
            self.finalizer_device = "cuda"
            self.finalizer_compute_type = "int8_float16"
        elif quality == "balanced":
            self.finalizer_model_name = "small"
            self.finalizer_device = "cpu"
            self.finalizer_compute_type = "int8"
        else:
            self.finalizer_model_name = "tiny"
            self.finalizer_device = "cpu"
            self.finalizer_compute_type = "int8"

    @staticmethod
    def _imports():
        _register_windows_runtime_dirs()
        from moonshine_voice import ModelArch, Transcriber, get_model_for_language
        from moonshine_voice.transcriber import TranscriptEventListener
        return Transcriber, TranscriptEventListener, get_model_for_language, ModelArch

    def prepare(self, progress_callback: ProgressCallback | None = None) -> tuple[str, object]:
        progress = progress_callback or (lambda _message: None)
        Transcriber, _, get_model_for_language, ModelArch = self._imports()
        self.model_dir.mkdir(parents=True, exist_ok=True)
        wanted_arch = (
            ModelArch.MEDIUM_STREAMING
            if self.quality in ("balanced", "best")
            else ModelArch.SMALL_STREAMING
        )
        progress("Moonshine: checking local model files and download cache…")
        model_path, model_arch = get_model_for_language(
            "en", wanted_arch, cache_root=self.model_dir,
        )
        progress(f"Moonshine: model files ready at {model_path}")
        progress("Moonshine: initializing the ONNX Runtime streaming engine…")
        update_interval = 0.20 if self.quality == "best" else 0.30 if self.quality == "balanced" else 0.50
        max_segment_seconds = 8 if self.quality in ("best", "balanced") else 12
        self.transcriber = Transcriber(
            model_path=str(model_path), model_arch=model_arch, update_interval=update_interval,
            options={
                "transcription_interval": str(update_interval),
                "vad_max_segment_duration": str(max_segment_seconds),
            },
        )
        progress("Moonshine: streaming model loaded successfully.")
        if not self.enable_finalizer:
            progress("Whisper finalizer disabled for this transcription job.")
            return str(model_path), model_arch
        try:
            label = "Whisper Turbo" if self.quality == "best" else "Multilingual Whisper"
            progress(f"{label}: checking the model cache and {self.finalizer_device.upper()} runtime…")
            self.finalizer = WhisperFinalizer(
                self.finalizer_model_name,
                self.finalizer_device,
                self.finalizer_compute_type,
                progress,
            )
            self.finalizer_error = None
            progress(f"{label}: finalizer loaded successfully.")
        except (ImportError, RuntimeError) as exc:
            self.finalizer = None
            self.finalizer_error = str(exc)
            progress(f"Multilingual finalizer unavailable; English Moonshine only: {exc}")
        return str(model_path), model_arch

    def start(
        self,
        session_id: str,
        sources: list[SourceKind],
        language: TranscriptionLanguage = TranscriptionLanguage.AUTO,
    ) -> None:
        if self.transcriber is None:
            self.prepare()
        _, ListenerBase, _, _ = self._imports()
        self.session_id = session_id
        self.started_at_ms = time.monotonic_ns() // 1_000_000
        self.revisions.clear()
        self.language = language

        engine = self

        class Listener(ListenerBase):
            def __init__(self, source: SourceKind):
                self.source = source

            def on_line_started(self, event):
                engine._emit(self.source, event, Stability.DRAFT)

            def on_line_text_changed(self, event):
                engine._emit(self.source, event, Stability.DRAFT)

            def on_line_completed(self, event):
                engine._emit(self.source, event, Stability.FINAL)

        with self.lock:
            self.streams.clear()
            self.listeners.clear()
            for source in sources:
                stream = self.transcriber.create_stream(
                    update_interval=0.20 if self.quality == "best" else 0.30 if self.quality == "balanced" else 0.50,
                )
                listener = Listener(source)
                stream.add_listener(listener)
                stream.start()
                self.streams[source] = stream
                self.listeners.append(listener)

    def _emit(self, source: SourceKind, event, stability: Stability) -> None:
        line = event.line
        text = str(getattr(line, "text", "")).strip()
        if not text:
            return
        line_id = str(
            getattr(line, "line_id", None)
            or getattr(line, "lineId", None)
            or getattr(line, "id", None)
            or uuid.uuid4()
        )
        key = (source, line_id)
        with self.lock:
            self.revisions[key] += 1
            revision = self.revisions[key]
        start_seconds = float(getattr(line, "start_time", getattr(line, "start", 0.0)) or 0.0)
        duration = float(getattr(line, "duration", 0.0) or 0.0)
        audio_data = getattr(line, "audio_data", None)
        has_audio = audio_data is not None and len(audio_data) > 0
        with self.lock:
            use_finalizer = stability == Stability.FINAL and self.finalizer is not None and has_audio and not self.pending_finalizers
        emitted_stability = Stability.DRAFT if use_finalizer else stability
        transcript_event = TranscriptEvent(
            session_id=self.session_id, source_id=source, utterance_id=line_id, text=text,
            start_ms=max(0, int(start_seconds * 1000)),
            end_ms=max(0, int((start_seconds + duration) * 1000)),
            revision=revision, stability=emitted_stability,
        )
        self.callback(transcript_event)
        if use_finalizer:
            future = self.finalizer_pool.submit(
                self._finalize, transcript_event, np.asarray(audio_data, dtype=np.float32),
            )
            with self.lock:
                self.pending_finalizers[future] = transcript_event
            future.add_done_callback(self._finalizer_done)

    def _finalizer_done(self, future: Future) -> None:
        with self.lock:
            self.pending_finalizers.pop(future, None)

    def _finalize(self, draft: TranscriptEvent, audio: np.ndarray) -> None:
        detected_language = None
        language_probability = None
        try:
            result = self.finalizer.finalize(audio, self.language)
            text = result.text
            detected_language = result.language
            language_probability = result.probability
        except Exception:
            text = draft.text
        self.callback(TranscriptEvent(
            session_id=draft.session_id, source_id=draft.source_id,
            utterance_id=draft.utterance_id, text=text or draft.text,
            start_ms=draft.start_ms, end_ms=draft.end_ms,
            revision=draft.revision + 1, stability=Stability.FINAL,
            detected_language=detected_language,
            language_probability=language_probability,
        ))

    def accept(self, frame: AudioFrame) -> None:
        with self.lock:
            stream = self.streams.get(frame.source_id)
        if stream:
            stream.add_audio(np.asarray(frame.samples, dtype=np.float32), frame.sample_rate)

    def stop(self) -> None:
        with self.lock:
            for stream in self.streams.values():
                try:
                    stream.stop()
                except Exception:
                    pass
            self.streams.clear()
            self.listeners.clear()

        # Stream.stop() completes active lines and may enqueue GPU refinement.
        # Give those jobs a bounded window, then promote their local draft so a
        # stopped session never loses its last phrase.
        with self.lock:
            pending = dict(self.pending_finalizers)
        if pending:
            _, unfinished = wait(pending, timeout=10.0)
            for future in unfinished:
                draft = pending[future]
                self.callback(TranscriptEvent(
                    session_id=draft.session_id, source_id=draft.source_id,
                    utterance_id=draft.utterance_id, text=draft.text,
                    start_ms=draft.start_ms, end_ms=draft.end_ms,
                    revision=draft.revision + 1, stability=Stability.FINAL,
                ))

    def close(self) -> None:
        self.stop()
        self.finalizer_pool.shutdown(wait=False, cancel_futures=True)

    def calibrate(self, stream_count: int = 2) -> float:
        """Exercise the loaded model with bundled real speech."""
        if self.transcriber is None:
            self.prepare()
        import moonshine_voice
        from moonshine_voice import load_wav_file

        sample_path = Path(moonshine_voice.__file__).parent / "assets" / "two_cities.wav"
        samples, sample_rate = load_wav_file(sample_path)
        audio = np.ascontiguousarray(samples[: sample_rate * 5], dtype=np.float32)
        seconds = len(audio) / sample_rate
        # Benchmark the default worst case: microphone and system recognition
        # are independent, so two simultaneous lanes roughly double ASR work.
        streams = [self.transcriber.create_stream(update_interval=10.0) for _ in range(max(1, stream_count))]
        for stream in streams:
            stream.start()
        for offset in range(0, len(audio), 1600):
            chunk = audio[offset:offset + 1600]
            for stream in streams:
                stream.add_audio(chunk, sample_rate)
        for stream in streams:
            stream.stop()
        return seconds


class WhisperFinalizer:
    def __init__(
        self,
        model_name: str = "large-v3-turbo",
        device: str = "cuda",
        compute_type: str = "int8_float16",
        progress_callback: ProgressCallback | None = None,
    ):
        progress = progress_callback or (lambda _message: None)
        if device == "cuda":
            from .capability import _cuda_runtime_ready
            if not _cuda_runtime_ready():
                raise RuntimeError("Best mode requires CUDA 12 cuBLAS and cuDNN 9")
        from faster_whisper import WhisperModel
        try:
            progress(f"Whisper {model_name}: downloading missing files or loading the cached model…")
            self.model = WhisperModel(model_name, device=device, compute_type=compute_type)
        except Exception as exc:
            raise RuntimeError(f"Whisper multilingual finalizer unavailable: {exc}") from exc

    def finalize(
        self,
        audio: np.ndarray,
        language_mode: TranscriptionLanguage = TranscriptionLanguage.AUTO,
    ) -> FinalizationResult:
        if language_mode == TranscriptionLanguage.AUTO:
            detected, detected_probability, probabilities = self.model.detect_language(
                audio=audio,
                vad_filter=False,
                language_detection_segments=2,
            )
            allowed = {code: probability for code, probability in (probabilities or []) if code in ("en", "tl")}
            language = max(allowed, key=allowed.get) if allowed else detected if detected in ("en", "tl") else "en"
            probability = float(allowed.get(language, detected_probability if language == detected else 0.0))
        else:
            language = language_mode.value
            probability = 1.0

        segments, info = self.model.transcribe(
            audio,
            language=language,
            task="transcribe",
            beam_size=1,
            vad_filter=False,
            condition_on_previous_text=False,
        )
        text = " ".join(segment.text.strip() for segment in segments).strip()
        return FinalizationResult(
            text=text,
            language=str(getattr(info, "language", language) or language),
            probability=probability,
        )
