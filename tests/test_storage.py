from pathlib import Path

from dalistener.models import CaptureSelection, SourceKind, Stability, TranscriptEvent
from dalistener.storage import SessionStore, TranscriptExporter


def event(text: str, revision: int, stability: Stability) -> TranscriptEvent:
    return TranscriptEvent(
        session_id="session", source_id=SourceKind.MICROPHONE, utterance_id="line-1",
        text=text, start_ms=1000, end_ms=2400, revision=revision, stability=stability,
    )


def test_final_transcript_is_immutable(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    store.start_session("session", CaptureSelection(), "model")
    assert store.save_event(event("draft", 1, Stability.DRAFT))
    assert store.save_event(event("settled", 2, Stability.FINAL))
    assert not store.save_event(event("late rewrite", 3, Stability.FINAL))
    assert store.events("session")[0]["text"] == "settled"


def test_all_export_formats(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    store.start_session("session", CaptureSelection(), "model")
    store.save_event(event("Hello world", 1, Stability.FINAL))
    exporter = TranscriptExporter(store)
    for suffix in (".txt", ".md", ".json", ".srt", ".vtt"):
        path = tmp_path / f"transcript{suffix}"
        exporter.export("session", path)
        assert path.exists()
        assert "Hello world" in path.read_text(encoding="utf-8")


def test_export_preserves_latest_draft_after_interruption(tmp_path: Path):
    store = SessionStore(tmp_path / "sessions.db")
    store.start_session("session", CaptureSelection(), "model")
    store.save_event(event("unfinished but valuable", 1, Stability.DRAFT))
    path = tmp_path / "transcript.txt"
    TranscriptExporter(store).export("session", path)
    assert "unfinished but valuable" in path.read_text(encoding="utf-8")
