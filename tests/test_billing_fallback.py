from pathlib import Path

from dalistener.dashboard.pricing import PricingCatalogService
from dalistener.dashboard.usage import UsageLedger


def test_pricing_parser_and_per_hour_math(tmp_path: Path):
    page = "<h2>Realtime audio duration</h2><p>Per minute</p><strong>$0.017</strong>"
    assert PricingCatalogService.parse_rate(page) == 0.017
    snapshot = PricingCatalogService(tmp_path / "pricing.json").snapshot(refresh=False)
    assert snapshot.stale is True
    assert snapshot.to_dict()["rate_per_hour_usd"] == 1.02


def test_usage_counts_cloud_audio_and_local_is_free(tmp_path: Path):
    ledger = UsageLedger(tmp_path / "usage.db")
    ledger.record("meeting-a", 1, "openai", "gpt-realtime-whisper", 60)
    ledger.record("meeting-a", 1, "local", "Moonshine", 600)
    ledger.record("meeting-b", 2, "openai", "gpt-realtime-whisper", 60)
    totals = ledger.totals(0.017, "meeting-a")
    assert totals["session_seconds"] == 60
    assert totals["session_cost_usd"] == 0.017
    assert totals["today_cost_usd"] == 0.034
