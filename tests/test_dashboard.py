import asyncio

import numpy as np
from fastapi.testclient import TestClient

from dalistener.dashboard.intelligence import watched_name
from dalistener.dashboard.openai_realtime import OpenAIRealtimeTranscriber
from dalistener.dashboard.server import create_app
from dalistener.dashboard.settings import OpenAISettings, OpenAISettingsStore
from dalistener.dashboard.sources import CaptureCategory, classify_source


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


def test_non_meeting_warning_can_be_suppressed_and_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAISettingsStore, "load", lambda _self: OpenAISettings(api_key="test-key"))
    app = create_app(tmp_path)
    with TestClient(app, follow_redirects=False) as client:
        context = app.state.context
        headers = {"X-DaListener-Extension-Token": context.extension_token}
        request = {"tab_id": 7, "title": "Video", "url": "https://www.youtube.com/watch?v=1"}

        first = client.post("/api/v1/extension/capture-preflight", headers=headers, json=request)
        assert first.status_code == 200
        assert first.json()["warning_required"] is True

        acknowledged = client.post(
            "/api/v1/extension/capture-warning/acknowledge",
            headers=headers,
            json={"domain": "youtube.com", "suppress_for_domain": True},
        )
        assert acknowledged.json()["suppressed_domains"] == ["youtube.com"]
        assert client.post("/api/v1/extension/capture-preflight", headers=headers, json=request).json()["warning_required"] is False

        client.get(f"/auth/exchange?token={context.launch_token}")
        reset = client.delete("/api/v1/settings/capture-warnings")
        assert reset.status_code == 200
        assert reset.json()["suppressed_domains"] == []


def test_extension_preflight_allows_chromium_extension_cors(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAISettingsStore, "load", lambda _self: OpenAISettings(api_key="test-key"))
    app = create_app(tmp_path)
    with TestClient(app) as client:
        response = client.options(
            "/api/v1/extension/capture-preflight",
            headers={
                "Origin": f"chrome-extension://{'a' * 32}",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,x-dalistener-extension-token",
            },
        )
        assert response.status_code == 200
        assert response.headers["access-control-allow-origin"] == f"chrome-extension://{'a' * 32}"


def test_extension_pairing_survives_app_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(OpenAISettingsStore, "load", lambda _self: OpenAISettings(api_key="test-key"))
    first = create_app(tmp_path)
    second = create_app(tmp_path)
    assert first.state.context.extension_token == second.state.context.extension_token

    with TestClient(second) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200
        assert health.json() == {"app": "DaListener", "status": "ready"}
