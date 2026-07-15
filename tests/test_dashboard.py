import numpy as np
from unittest.mock import AsyncMock
from types import SimpleNamespace
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from dalistener.dashboard.intelligence import watched_name
from dalistener.dashboard.local_models import LocalModelService
from dalistener.dashboard.openai_realtime import OpenAIRealtimeTranscriber
from dalistener.dashboard.server import create_app
from dalistener.dashboard.settings import OpenAISettings, OpenAISettingsStore
from dalistener.dashboard.sources import CaptureCategory, classify_shared_label, classify_source


def test_watched_name_uses_word_boundaries():
    assert watched_name("Vladimir, can you verify this?") == "Vladimir"
    assert watched_name("Ask Vlad about it") == "Vlad"
    assert watched_name("The vladivostok environment is ready") is None


def test_float_audio_is_resampled_for_openai():
    async def transcript(_payload):
        pass

    async def status(_state, _message):
        pass

    transcriber = OpenAIRealtimeTranscriber("test-key", "test-model", 48_000, transcript, status)
    source = np.linspace(-0.5, 0.5, 4_800, dtype="<f4").tobytes()
    output = transcriber._resample_pcm16(source)
    assert len(output) == 2_400 * 2


def test_realtime_transcription_uses_supported_delay_field():
    async def transcript(_payload):
        pass

    async def status(_state, _message):
        pass

    transcriber = OpenAIRealtimeTranscriber("test-key", "gpt-realtime-whisper", 24_000, transcript, status)
    transcription = transcriber._session_update()["session"]["audio"]["input"]["transcription"]
    assert transcription["delay"] == "low"
    assert "latency" not in transcription
    assert transcriber.connection_model == "gpt-realtime-2.1"
    quota_error = transcriber._provider_error(RuntimeError("You exceeded your current quota"))
    assert str(quota_error) == "OpenAI API quota is unavailable. Add API billing or credits, then try capture again."


def test_dashboard_bootstrap_never_exposes_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(
        OpenAISettingsStore,
        "load",
        lambda _self: OpenAISettings(api_key=None),
    )
    app = create_app(tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        context = app.state.context
        response = client.get(f"/auth/exchange?token={context.launch_token}")
        assert response.status_code == 303
        response = client.get("/api/v1/bootstrap")
        assert response.status_code == 200
        payload = response.json()
        assert payload["openai"]["configured"] is False
        assert "api_key" not in str(payload).lower()


def test_source_classification_is_domain_safe():
    assert classify_source("https://app.zoom.us/wc").category == CaptureCategory.MEETING
    assert classify_source("https://zoom.us.example.com").category == CaptureCategory.OTHER
    youtube = classify_source("https://www.youtube.com/watch?v=123")
    assert youtube.category == CaptureCategory.MEDIA
    assert youtube.service_label == "YouTube"
    assert classify_source("chrome://settings").supported is False
    assert classify_shared_label("Chrome Tab - Zoom Meeting").category == CaptureCategory.MEETING
    assert classify_shared_label("YouTube - Lecture").category == CaptureCategory.MEDIA
    assert classify_shared_label("Quarterly webcast").category == CaptureCategory.OTHER


def test_dashboard_launch_auth_survives_app_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAISettingsStore, "load", lambda _self: OpenAISettings(api_key="test-key"))
    first = create_app(tmp_path)
    second = create_app(tmp_path)
    assert first.state.context.launch_token == second.state.context.launch_token

    with TestClient(second) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json() == {"app": "DaListener", "status": "ready"}


def test_application_stop_requires_session_and_signals_server(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAISettingsStore, "load", lambda _self: OpenAISettings(api_key="test-key"))
    app = create_app(tmp_path)
    app.state.uvicorn_server = SimpleNamespace(should_exit=False)
    with TestClient(app, follow_redirects=False) as client:
        assert client.post("/api/v1/application/stop").status_code == 401
        client.get(f"/auth/exchange?token={app.state.context.launch_token}")
        assert client.post("/api/v1/application/stop").json() == {"ok": True}
        assert app.state.uvicorn_server.should_exit is True


def test_media_upload_is_authenticated_and_parameterizes_watched_names(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAISettingsStore, "load", lambda _self: OpenAISettings(api_key="test-key"))
    app = create_app(tmp_path)
    app.state.context.uploads.process = AsyncMock(return_value={"ok": True})
    with TestClient(app, follow_redirects=False) as client:
        files = {"media": ("meeting.mp4", b"media", "video/mp4")}
        form = {"watched_names": "Vlad, Alex", "provider": "local"}
        assert client.post("/api/v1/uploads/transcribe", files=files, data=form).status_code == 401
        client.get(f"/auth/exchange?token={app.state.context.launch_token}")
        response = client.post("/api/v1/uploads/transcribe", files=files, data=form)
        assert response.json() == {"ok": True}
        call = app.state.context.uploads.process.await_args
        assert call.args[2:] == (["Vlad", "Alex"], "local")


def test_full_package_discovers_bundled_lfm_and_cpu_runtime(tmp_path, monkeypatch):
    assets = tmp_path / "offline-assets" / "LocalFallback"
    (assets / "Moonshine").mkdir(parents=True)
    (assets / "Moonshine" / "model.bin").write_bytes(b"moonshine")
    (assets / "model.gguf").write_bytes(b"gguf")
    runtime = assets / "LlamaCpp" / "release" / "cpu" / "llama-server.exe"
    runtime.parent.mkdir(parents=True)
    runtime.write_bytes(b"runtime")
    monkeypatch.setenv("DALISTENER_OFFLINE_ASSETS", str(tmp_path / "offline-assets"))
    monkeypatch.setattr("dalistener.dashboard.local_models.CapabilityService.inspect", lambda _self: SimpleNamespace(gpu_refinement=False))
    service = LocalModelService(tmp_path / "data", lambda *_args: None)
    assert service.status.state == "ready"
    assert service.status.model_path == str(assets / "model.gguf")
    assert service.status.runtime_path == str(runtime)
    assert service.status.transcription_ready is True


def test_native_browser_audio_socket_requires_dashboard_session(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAISettingsStore, "load", lambda _self: OpenAISettings(api_key="test-key"))
    app = create_app(tmp_path)
    with TestClient(app) as client:
        try:
            with client.websocket_connect("/api/v1/browser/audio"):
                raise AssertionError("Unauthenticated browser audio socket was accepted")
        except WebSocketDisconnect as exc:
            assert exc.code == 4401


def test_native_browser_audio_socket_accepts_bootstrap_token(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAISettingsStore, "load", lambda _self: OpenAISettings(api_key="test-key"))
    app = create_app(tmp_path)
    with TestClient(app) as client:
        context = app.state.context
        with client.websocket_connect(f"/api/v1/browser/audio?token={context.session_token}") as websocket:
            websocket.send_text("{}")
            response = websocket.receive_json()
            assert response["type"] == "error"
            assert "validation error" in response["message"].lower()


def test_provider_mode_is_explicit_and_persistent(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAISettingsStore, "load", lambda _self: OpenAISettings(api_key="test-key"))
    app = create_app(tmp_path)
    with TestClient(app) as client:
        context = app.state.context
        client.get(f"/auth/exchange?token={context.launch_token}")
        changed = client.put("/api/v1/settings/providers", json={"mode": "local"})
        assert changed.status_code == 200
        assert changed.json()["policy"] == "local"
        assert client.get("/api/v1/bootstrap").json()["provider_mode"] == "local"

    restarted = create_app(tmp_path)
    assert restarted.state.context.provider_mode.load() == "local"


def test_duplicate_capture_titles_receive_short_random_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAISettingsStore, "load", lambda _self: OpenAISettings(api_key="test-key"))
    app = create_app(tmp_path)
    manager = app.state.context.meetings
    manager.meetings["existing"] = SimpleNamespace(summary=SimpleNamespace(title="Daily stand-up"))

    assert manager._unique_title("Quarterly review") == "Quarterly review"
    duplicate = manager._unique_title("  Daily   stand-up ")
    assert duplicate.startswith("Daily stand-up · ")
    assert len(duplicate.rsplit(" ", 1)[-1]) == 4
