from __future__ import annotations

import json
import threading
from pathlib import Path


class CapturePreferenceStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()

    def suppressed_domains(self) -> list[str]:
        with self.lock:
            if not self.path.exists():
                return []
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                values = payload.get("suppressed_non_meeting_domains", [])
                return sorted({str(value).lower() for value in values if value})
            except (OSError, json.JSONDecodeError, TypeError):
                return []

    def is_suppressed(self, domain: str) -> bool:
        return domain.lower() in self.suppressed_domains()

    def suppress(self, domain: str) -> list[str]:
        normalized = domain.strip().lower()
        if not normalized:
            raise ValueError("A website domain is required")
        domains = set(self.suppressed_domains())
        domains.add(normalized)
        return self._save(domains)

    def remove(self, domain: str) -> list[str]:
        domains = set(self.suppressed_domains())
        domains.discard(domain.strip().lower())
        return self._save(domains)

    def reset(self) -> list[str]:
        return self._save(set())

    def _save(self, domains: set[str]) -> list[str]:
        with self.lock:
            ordered = sorted(domains)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps({"version": 1, "suppressed_non_meeting_domains": ordered}, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
            return ordered
