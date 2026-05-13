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


def get_backend(engine: Optional[str] = None) -> TranscriptionBackend:
    """Возвращает подходящий бэкенд.

    Mac → mlx-whisper (single option).
    Windows/Linux → переключается через config.TRANSCRIPTION_ENGINE ("whisper" | "gigaam"),
    либо явный аргумент engine.

    См. research/transcribe_compare/README.md по сравнению движков.
    """
    if config.IS_MAC:
        from app.transcription.mlx_whisper_backend import MLXWhisperBackend
        return MLXWhisperBackend()

    chosen = engine or getattr(config, "TRANSCRIPTION_ENGINE", "whisper")
    if chosen == "gigaam":
        from app.transcription.gigaam_backend import GigaAMBackend
        return GigaAMBackend()
    if chosen == "whisper":
        from app.transcription.faster_whisper_backend import FasterWhisperBackend
        return FasterWhisperBackend()
    raise ValueError(f"Unknown TRANSCRIPTION_ENGINE: {chosen!r} (expected 'whisper'|'gigaam')")


def unload_model() -> None:
    """Выгружает модель транскрипции из памяти (no-op для subprocess-бэкендов)."""
    if config.IS_MAC:
        from app.transcription import mlx_whisper_backend
        mlx_whisper_backend.unload()
        return
    # Все Windows-бэкенды subprocess-based → unload не нужен, но вызовы оставлены
    # для обратной совместимости со старым кодом.
    from app.transcription import faster_whisper_backend, gigaam_backend
    faster_whisper_backend.unload()
    gigaam_backend.unload()
