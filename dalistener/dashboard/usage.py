from __future__ import annotations

import json
import sqlite3
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


class UsageLedger:
    def __init__(self, path: Path):
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS usage_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL,
                tab_id INTEGER,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                audio_seconds REAL NOT NULL,
                recorded_at TEXT NOT NULL
            )
        """)
        self.connection.commit()

    def record(self, meeting_id: str, tab_id: int | None, provider: str, model: str, seconds: float) -> None:
        if seconds <= 0:
            return
        with self.lock:
            self.connection.execute(
                "INSERT INTO usage_ledger(meeting_id, tab_id, provider, model, audio_seconds, recorded_at) VALUES(?,?,?,?,?,?)",
                (meeting_id, tab_id, provider, model, float(seconds), datetime.now(timezone.utc).isoformat()),
            )
            self.connection.commit()

    def totals(self, rate_per_minute: float, meeting_id: str | None = None) -> dict:
        now = datetime.now(timezone.utc)
        day = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
        with self.lock:
            def total(where: str = "", args: tuple = ()) -> float:
                row = self.connection.execute(
                    "SELECT COALESCE(SUM(audio_seconds),0) seconds FROM usage_ledger WHERE provider='openai' " + where,
                    args,
                ).fetchone()
                return round(float(row["seconds"]), 3)
            session_seconds = total("AND meeting_id=?", (meeting_id,)) if meeting_id else 0.0
            today_seconds = total("AND recorded_at>=?", (day,))
            month_seconds = total("AND recorded_at>=?", (month,))
        cost = lambda seconds: round(seconds / 60 * rate_per_minute, 6)
        return {
            "meeting_id": meeting_id,
            "session_seconds": session_seconds,
            "today_seconds": today_seconds,
            "month_seconds": month_seconds,
            "session_cost_usd": cost(session_seconds),
            "today_cost_usd": cost(today_seconds),
            "month_cost_usd": cost(month_seconds),
        }


class OpenAIOrganizationUsage:
    BASE = "https://api.openai.com/v1/organization"

    @staticmethod
    def _get(path: str, key: str, params: dict) -> dict:
        url = f"{OpenAIOrganizationUsage.BASE}/{path}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))

    def month(self, admin_key: str | None) -> dict:
        if not admin_key:
            return {"configured": False, "status": "not-configured", "message": "Connect an OpenAI Admin key for account-wide totals."}
        start = int(datetime.now(timezone.utc).replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp())
        try:
            usage = self._get("usage/audio_transcriptions", admin_key, {"start_time": start, "bucket_width": "1d", "limit": 31})
            costs = self._get("costs", admin_key, {"start_time": start, "bucket_width": "1d", "limit": 31})
            seconds = sum(float(result.get("seconds", 0)) for bucket in usage.get("data", []) for result in bucket.get("results", []))
            amount = sum(float(result.get("amount", {}).get("value", 0)) for bucket in costs.get("data", []) for result in bucket.get("results", []))
            return {"configured": True, "status": "ready", "audio_seconds": seconds, "cost_usd": round(amount, 4)}
        except Exception as exc:
            return {"configured": True, "status": "error", "message": str(exc)}
