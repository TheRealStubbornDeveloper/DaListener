from __future__ import annotations

import os
from dataclasses import dataclass

import keyring


@dataclass(slots=True)
class OpenAISettings:
    api_key: str | None
    transcription_model: str = "gpt-realtime-whisper"
    intelligence_model: str = "gpt-5.6-luna"


class OpenAISettingsStore:
    """Keeps the secret in the operating-system keychain, never browser storage."""

    SERVICE = "DaListener/OpenAI"
    USERNAME = "api-key"

    def load(self) -> OpenAISettings:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            try:
                key = keyring.get_password(self.SERVICE, self.USERNAME)
            except Exception:
                key = None
        return OpenAISettings(
            api_key=key,
            transcription_model=os.environ.get("DALISTENER_TRANSCRIPTION_MODEL", "gpt-realtime-whisper"),
            intelligence_model=os.environ.get("DALISTENER_INTELLIGENCE_MODEL", "gpt-5.6-luna"),
        )

    def save_api_key(self, api_key: str) -> None:
        key = api_key.strip()
        if not key:
            raise ValueError("An OpenAI API key is required")
        keyring.set_password(self.SERVICE, self.USERNAME, key)

    def delete_api_key(self) -> None:
        try:
            keyring.delete_password(self.SERVICE, self.USERNAME)
        except keyring.errors.PasswordDeleteError:
            pass
