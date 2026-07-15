from __future__ import annotations

import json
import re
import threading
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


PRICE_URL = "https://developers.openai.com/api/docs/models/gpt-realtime-whisper"
FALLBACK_RATE_USD = 0.017


@dataclass(slots=True)
class PricingSnapshot:
    model: str = "gpt-realtime-whisper"
    rate_per_minute_usd: float = FALLBACK_RATE_USD
    source_url: str = PRICE_URL
    checked_at: str = ""
    stale: bool = True

    def to_dict(self) -> dict:
        value = asdict(self)
        value["rate_per_hour_usd"] = round(self.rate_per_minute_usd * 60, 4)
        return value


class PricingCatalogService:
    """Refreshes the public model price without making capture depend on the network."""

    def __init__(self, cache_path: Path, refresh_after: timedelta = timedelta(days=1)):
        self.cache_path = cache_path
        self.refresh_after = refresh_after
        self._lock = threading.Lock()

    @staticmethod
    def parse_rate(page: str) -> float:
        plain = re.sub(r"<[^>]+>", " ", page)
        patterns = (
            r"Realtime\s+audio\s+duration.{0,300}?Per\s+minute.{0,100}?\$\s*([0-9]+(?:\.[0-9]+)?)",
            r"\$\s*([0-9]+(?:\.[0-9]+)?)\s*(?:per|/)\s*(?:audio\s*)?minute",
        )
        for pattern in patterns:
            match = re.search(pattern, plain, re.IGNORECASE | re.DOTALL)
            if match:
                value = float(match.group(1))
                if 0 < value < 10:
                    return value
        raise ValueError("Could not find the realtime audio per-minute price")

    def _load(self) -> PricingSnapshot | None:
        try:
            return PricingSnapshot(**json.loads(self.cache_path.read_text(encoding="utf-8")))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _save(self, snapshot: PricingSnapshot) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(json.dumps(asdict(snapshot), indent=2), encoding="utf-8")

    def snapshot(self, refresh: bool = True) -> PricingSnapshot:
        with self._lock:
            cached = self._load()
            now = datetime.now(timezone.utc)
            if cached and cached.checked_at:
                try:
                    checked = datetime.fromisoformat(cached.checked_at)
                    if not refresh or now - checked < self.refresh_after:
                        return cached
                except ValueError:
                    pass
            if refresh:
                try:
                    request = urllib.request.Request(PRICE_URL, headers={"User-Agent": "DaListener/0.3"})
                    with urllib.request.urlopen(request, timeout=6) as response:
                        rate = self.parse_rate(response.read().decode("utf-8", errors="replace"))
                    current = PricingSnapshot(rate_per_minute_usd=rate, checked_at=now.isoformat(), stale=False)
                    self._save(current)
                    return current
                except (OSError, ValueError):
                    pass
            if cached:
                cached.stale = True
                return cached
            return PricingSnapshot(checked_at=now.isoformat(), stale=True)
