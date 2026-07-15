import asyncio

import numpy as np
from fastapi.testclient import TestClient

from dalistener.dashboard.intelligence import watched_name
from dalistener.dashboard.openai_realtime import OpenAIRealtimeTranscriber
from dalistener.dashboard.server import create_app
from dalistener.dashboard.settings import OpenAISettings, OpenAISettingsStore


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
