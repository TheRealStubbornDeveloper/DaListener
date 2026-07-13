from pathlib import Path

import pytest

from dalistener.models import CaptureSelection
from dalistener.session import SessionController
from dalistener.storage import SessionStore


def test_failed_capture_start_aborts_empty_session(tmp_path: Path, monkeypatch):
    store = SessionStore(tmp_path / "sessions.db")
    controller = SessionController(
        store, tmp_path / "models", "efficient", "model",
        lambda _event: None, lambda _source, _text: None,
    )
    monkeypatch.setattr(controller.engine, "start", lambda _session, _sources, _language: None)
    monkeypatch.setattr(controller.engine, "stop", lambda: None)
    monkeypatch.setattr(
        controller.capture, "start",
        lambda _selection, _frame, _status: (_ for _ in ()).throw(RuntimeError("device failed")),
    )

    with pytest.raises(RuntimeError, match="device failed"):
        controller.start(CaptureSelection())

    assert controller.session_id is None
    assert store.list_sessions() == []
    controller.close()
    store.close()
