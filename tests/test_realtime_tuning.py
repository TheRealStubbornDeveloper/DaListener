from types import SimpleNamespace

import numpy as np

from dalistener.models import SourceKind, Stability, TranscriptEvent
from dalistener.transcription import MoonshineEngine


def test_best_mode_uses_low_latency_moonshine_updates(tmp_path, monkeypatch):
    captured = {}

    class FakeTranscriber:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    class FakeArch:
        MEDIUM_STREAMING = "medium"
        SMALL_STREAMING = "small"

    engine = MoonshineEngine(tmp_path, "best", lambda _event: None)
    monkeypatch.setattr(
        engine,
        "_imports",
        lambda: (FakeTranscriber, object, lambda *_args, **_kwargs: (tmp_path / "model", "medium"), FakeArch),
    )
    monkeypatch.setattr(
        "dalistener.transcription.WhisperFinalizer",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("test runtime unavailable")),
    )

    engine.prepare()

    assert captured["update_interval"] == 0.20
    assert captured["options"]["transcription_interval"] == "0.2"
    assert captured["options"]["vad_max_segment_duration"] == "8"
    engine.close()


def test_busy_whisper_finalizer_never_delays_next_moonshine_final(tmp_path):
    events = []
    engine = MoonshineEngine(tmp_path, "best", events.append)
    engine.session_id = "meeting"
    engine.finalizer = object()
    previous = TranscriptEvent(
        session_id="meeting", source_id=SourceKind.SYSTEM, utterance_id="previous",
        text="previous", start_ms=0, end_ms=1, revision=1, stability=Stability.DRAFT,
    )
    engine.pending_finalizers[object()] = previous
    line = SimpleNamespace(
        text="ship the realtime fix", line_id="next", start_time=1.0, duration=0.8,
        audio_data=np.ones(1600, dtype=np.float32),
    )

    engine._emit(SourceKind.SYSTEM, SimpleNamespace(line=line), Stability.FINAL)

    assert len(events) == 1
    assert events[0].stability == Stability.FINAL
    assert events[0].text == "ship the realtime fix"
    engine.pending_finalizers.clear()
    engine.close()
