from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path

from .audio import CaptureManager
from .models import CaptureMode, CaptureSelection, SourceKind, Stability, TranscriptEvent
from .storage import SessionStore
from .transcription import MoonshineEngine


class SessionController:
    def __init__(
        self,
        store: SessionStore,
        model_dir: Path,
        quality: str,
        model_name: str,
        event_callback: Callable[[TranscriptEvent], None],
        status_callback: Callable[[SourceKind, str], None],
        level_callback: Callable[[SourceKind, float], None] | None = None,
    ):
        self.store = store
        self.model_name = model_name
        self.event_callback = event_callback
        self.status_callback = status_callback
        self.level_callback = level_callback or (lambda _source, _level: None)
        self.engine = MoonshineEngine(model_dir, quality, self._on_transcript)
        self.capture = CaptureManager()
        self.session_id: str | None = None
        self.started_at = 0.0
        self.paused = False
        self._last_level_at: dict[SourceKind, float] = {}

    def prepare(self) -> tuple[str, object]:
        return self.engine.prepare()

    def start(self, selection: CaptureSelection) -> str:
        if self.session_id:
            raise RuntimeError("A session is already running")
        session_id = str(uuid.uuid4())
        sources = []
        if selection.mode in (CaptureMode.MICROPHONE, CaptureMode.BOTH):
            sources.append(SourceKind.MICROPHONE)
        if selection.mode in (CaptureMode.SYSTEM, CaptureMode.BOTH):
            sources.append(SourceKind.SYSTEM)
        self.store.start_session(session_id, selection, self.model_name)
        self.session_id = session_id
        self.started_at = time.monotonic()
        self.paused = False
        try:
            self.engine.start(session_id, sources)
            self.capture.start(selection, self._on_frame, self._on_status)
        except Exception:
            self.capture.stop()
            self.engine.stop()
            self.store.abort_session(session_id)
            self.session_id = None
            self.started_at = 0.0
            raise
        return session_id

    def _on_frame(self, frame) -> None:
        self.engine.accept(frame)
        now = time.monotonic()
        if now - self._last_level_at.get(frame.source_id, 0.0) >= 0.1:
            import numpy as np
            rms = float(np.sqrt(np.mean(np.square(frame.samples)))) if len(frame.samples) else 0.0
            self.level_callback(frame.source_id, min(1.0, rms * 8.0))
            self._last_level_at[frame.source_id] = now

    def _on_status(self, source: SourceKind, text: str) -> None:
        self.status_callback(source, text)
        if self.session_id and "stopped" in text.lower():
            elapsed_ms = int((time.monotonic() - self.started_at) * 1000)
            event = TranscriptEvent(
                session_id=self.session_id, source_id=SourceKind.STATUS,
                utterance_id=f"interruption-{source.value}-{elapsed_ms}",
                text=f"{source.value.title()} interruption: {text}",
                start_ms=elapsed_ms, end_ms=elapsed_ms, revision=1,
                stability=Stability.FINAL,
            )
            if self.store.save_event(event):
                self.event_callback(event)

    def _on_transcript(self, event: TranscriptEvent) -> None:
        if self.store.save_event(event):
            self.event_callback(event)

    def pause(self) -> bool:
        if not self.session_id:
            return False
        self.paused = not self.paused
        self.capture.pause(self.paused)
        self.status_callback(SourceKind.STATUS, "Paused" if self.paused else "Listening")
        return self.paused

    def bookmark(self, note: str = "") -> None:
        if self.session_id:
            self.store.add_bookmark(
                self.session_id, int((time.monotonic() - self.started_at) * 1000), note,
            )

    def stop(self) -> str | None:
        session_id = self.session_id
        if not session_id:
            return None
        self.capture.stop()
        self.engine.stop()
        self.store.end_session(session_id)
        self.session_id = None
        self.paused = False
        return session_id

    def close(self) -> None:
        if self.session_id:
            self.stop()
        self.engine.close()
