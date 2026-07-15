from __future__ import annotations

import json
import secrets
import threading
from pathlib import Path


class DashboardLaunchStore:
    """Persists the browser launch secret so bookmarked URLs survive restarts."""

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def token(self) -> str:
        with self.lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                token = payload.get("launch_token")
                if isinstance(token, str) and len(token) >= 32:
                    return token
            except (OSError, json.JSONDecodeError, TypeError):
                pass
            token = secrets.token_urlsafe(32)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"version": 1, "launch_token": token}, indent=2), encoding="utf-8")
            temporary.replace(self.path)
            return token
