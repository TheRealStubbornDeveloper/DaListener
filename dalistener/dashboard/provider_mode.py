from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Literal


ProviderMode = Literal["auto", "cloud", "local"]


class ProviderModeStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()

    def load(self) -> ProviderMode:
        with self.lock:
            try:
                mode = json.loads(self.path.read_text(encoding="utf-8")).get("mode")
                return mode if mode in {"auto", "cloud", "local"} else "auto"
            except (OSError, AttributeError, json.JSONDecodeError):
                return "auto"

    def save(self, mode: str) -> ProviderMode:
        if mode not in {"auto", "cloud", "local"}:
            raise ValueError("Provider mode must be auto, cloud, or local")
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(json.dumps({"version": 1, "mode": mode}, indent=2), encoding="utf-8")
            temporary.replace(self.path)
        return mode  # type: ignore[return-value]
