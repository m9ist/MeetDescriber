"""
Абстракция TranscriptionBackend + фабричная функция.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import config


@dataclass
class TranscriptionWord:
    start: float
    end: float
    word: str
    probability: float


@dataclass
class TranscriptionSegment:
    start: float          # секунды от начала аудио
    end: float
    text: str
    confidence: float     # средняя вероятность слов в сегменте
    words: list[TranscriptionWord] = field(default_factory=list)


@dataclass
class TranscriptionResult:
    segments: list[TranscriptionSegment]
    language: str
    duration: float       # секунды


class TranscriptionBackend(ABC):
    @abstractmethod
    def transcribe(
        self,
        audio_path: Path,
        on_progress: "Optional[Callable[[float, float], None]]" = None,
    ) -> TranscriptionResult:
        """Транскрибирует аудиофайл. Возвращает сегменты с временными метками.

        on_progress(current_sec, total_sec) — вызывается после каждого сегмента.
        """
        ...


def get_backend() -> TranscriptionBackend:
    """Возвращает подходящий бэкенд для текущей платформы."""
    if config.IS_MAC:
        from app.transcription.mlx_whisper_backend import MLXWhisperBackend
        return MLXWhisperBackend()
    else:
        from app.transcription.faster_whisper_backend import FasterWhisperBackend
        return FasterWhisperBackend()
