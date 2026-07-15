from __future__ import annotations

import os
from dataclasses import dataclass

import keyring


@dataclass(slots=True)
class OpenAISettings:
    api_key: str | None
    admin_key: str | None = None
    transcription_model: str = "gpt-realtime-whisper"
    intelligence_model: str = "gpt-5.6-luna"
    realtime_connection_model: str = "gpt-realtime-2.1"


class OpenAISettingsStore:
    """Keeps the secret in the operating-system keychain, never browser storage."""

    SERVICE = "DaListener/OpenAI"
    USERNAME = "api-key"
    ADMIN_USERNAME = "admin-key"

    def load(self) -> OpenAISettings:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            try:
                key = keyring.get_password(self.SERVICE, self.USERNAME)
            except Exception:
                key = None
        admin_key = os.environ.get("OPENAI_ADMIN_KEY")
        if not admin_key:
            try:
                admin_key = keyring.get_password(self.SERVICE, self.ADMIN_USERNAME)
            except Exception:
                admin_key = None
        return OpenAISettings(
            api_key=key,
            admin_key=admin_key,
            transcription_model=os.environ.get("DALISTENER_TRANSCRIPTION_MODEL", "gpt-realtime-whisper"),
            intelligence_model=os.environ.get("DALISTENER_INTELLIGENCE_MODEL", "gpt-5.6-luna"),
            realtime_connection_model=os.environ.get("DALISTENER_REALTIME_MODEL", "gpt-realtime-2.1"),
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

    def save_admin_key(self, admin_key: str) -> None:
        key = admin_key.strip()
        if not key:
            raise ValueError("An OpenAI Admin key is required")
        keyring.set_password(self.SERVICE, self.ADMIN_USERNAME, key)

    def delete_admin_key(self) -> None:
        try:
            keyring.delete_password(self.SERVICE, self.ADMIN_USERNAME)
        except keyring.errors.PasswordDeleteError:
            pass
