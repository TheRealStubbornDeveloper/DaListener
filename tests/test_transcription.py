from types import SimpleNamespace

import numpy as np

from dalistener.models import TranscriptionLanguage
from dalistener.transcription import WhisperFinalizer


class FakeWhisperModel:
    def __init__(self):
        self.transcribed_language = None

    def detect_language(self, **_kwargs):
        return "es", 0.4, [("tl", 0.72), ("en", 0.21), ("es", 0.07)]

    def transcribe(self, _audio, language, **_kwargs):
        self.transcribed_language = language
        return iter([SimpleNamespace(text=" Kumusta, mundo. ")]), SimpleNamespace(language=language)


def test_automatic_language_is_constrained_to_english_or_tagalog():
    finalizer = WhisperFinalizer.__new__(WhisperFinalizer)
    finalizer.model = FakeWhisperModel()

    result = finalizer.finalize(np.zeros(16_000, dtype=np.float32), TranscriptionLanguage.AUTO)

    assert finalizer.model.transcribed_language == "tl"
    assert result.text == "Kumusta, mundo."
    assert result.language == "tl"
    assert result.probability == 0.72


def test_explicit_english_skips_automatic_detection():
    finalizer = WhisperFinalizer.__new__(WhisperFinalizer)
    finalizer.model = FakeWhisperModel()

    result = finalizer.finalize(np.zeros(16_000, dtype=np.float32), TranscriptionLanguage.ENGLISH)

    assert finalizer.model.transcribed_language == "en"
    assert result.language == "en"
    assert result.probability == 1.0
