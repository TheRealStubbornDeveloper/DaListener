from __future__ import annotations

import threading
import time
import uuid
import os
import sys
from concurrent.futures import Future, ThreadPoolExecutor, wait
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import numpy as np

from .models import AudioFrame, SourceKind, Stability, TranscriptEvent


TranscriptCallback = Callable[[TranscriptEvent], None]
ProgressCallback = Callable[[str], None]
_DLL_DIR_HANDLES: list[object] = []


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

    def __init__(self, model_dir: Path, quality: str, callback: TranscriptCallback):
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
        self.transcriber = Transcriber(
            model_path=str(model_path), model_arch=model_arch, update_interval=0.5,
            options={"transcription_interval": "0.5", "vad_max_segment_duration": "15"},
        )
        progress("Moonshine: streaming model loaded successfully.")
        if self.quality == "best":
            try:
                progress("Whisper Turbo: checking the Hugging Face cache and CUDA runtime…")
                self.finalizer = WhisperFinalizer(progress)
                self.finalizer_error = None
                progress("Whisper Turbo: GPU finalizer loaded successfully.")
            except (ImportError, RuntimeError) as exc:
                self.finalizer = None
                self.finalizer_error = str(exc)
                progress(f"Whisper Turbo unavailable; continuing with Moonshine: {exc}")
        return str(model_path), model_arch

    def start(self, session_id: str, sources: list[SourceKind]) -> None:
        if self.transcriber is None:
            self.prepare()
        _, ListenerBase, _, _ = self._imports()
        self.session_id = session_id
        self.started_at_ms = time.monotonic_ns() // 1_000_000
        self.revisions.clear()

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
                stream = self.transcriber.create_stream(update_interval=0.5)
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
        emitted_stability = Stability.DRAFT if stability == Stability.FINAL and self.finalizer else stability
        transcript_event = TranscriptEvent(
            session_id=self.session_id, source_id=source, utterance_id=line_id, text=text,
            start_ms=max(0, int(start_seconds * 1000)),
            end_ms=max(0, int((start_seconds + duration) * 1000)),
            revision=revision, stability=emitted_stability,
        )
        self.callback(transcript_event)
        audio_data = getattr(line, "audio_data", None)
        if stability == Stability.FINAL and self.finalizer and audio_data:
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
        try:
            text = self.finalizer.finalize(audio)
        except Exception:
            text = draft.text
        self.callback(TranscriptEvent(
            session_id=draft.session_id, source_id=draft.source_id,
            utterance_id=draft.utterance_id, text=text or draft.text,
            start_ms=draft.start_ms, end_ms=draft.end_ms,
            revision=draft.revision + 1, stability=Stability.FINAL,
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

    def calibrate(self) -> float:
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
        streams = [self.transcriber.create_stream(update_interval=10.0) for _ in range(2)]
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
    def __init__(self, progress_callback: ProgressCallback | None = None):
        progress = progress_callback or (lambda _message: None)
        from .capability import _cuda_runtime_ready
        if not _cuda_runtime_ready():
            raise RuntimeError("Best mode requires CUDA 12 cuBLAS and cuDNN 9")
        from faster_whisper import WhisperModel
        try:
            progress("Whisper Turbo: downloading missing files or loading the cached model…")
            self.model = WhisperModel("large-v3-turbo", device="cuda", compute_type="int8_float16")
        except Exception as exc:
            raise RuntimeError(f"Whisper GPU finalizer unavailable: {exc}") from exc

    def finalize(self, audio: np.ndarray) -> str:
        segments, _ = self.model.transcribe(
            audio, language="en", beam_size=1, vad_filter=False, condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments).strip()
