"""
Транскрипция через mlx-whisper (Mac Apple Silicon).
"""
from __future__ import annotations

import io
import logging
import threading
import time
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

import config
from app.transcription.backend import (
    TranscriptionBackend,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionWord,
)

log = logging.getLogger(__name__)


def unload() -> None:
    """No-op: mlx-whisper не кэширует модель между вызовами."""
    pass


class _LogWriter(io.TextIOBase):
    """File-like → logging. Каждая строка → log.info."""

    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            line = line.rstrip()
            if line:
                log.info("%s%s", self._prefix, line)
        return len(s)

    def flush(self) -> None:
        if self._buf.strip():
            log.info("%s%s", self._prefix, self._buf.strip())
        self._buf = ""


class MLXWhisperBackend(TranscriptionBackend):

    def transcribe(self, audio_path: Path, on_progress=None) -> TranscriptionResult:
        import mlx_whisper

        log.info("mlx-whisper: загружаем модель и аудио %s...", audio_path.name)
        t0 = time.monotonic()

        # Heartbeat — раз в 10с пишем в лог, чтобы пользователь видел что не зависло
        done = threading.Event()
        def _heartbeat():
            while not done.wait(10):
                log.info("mlx-whisper: транскрибация идёт... %.0f s", time.monotonic() - t0)
        hb = threading.Thread(target=_heartbeat, daemon=True, name="mlx-heartbeat")
        hb.start()

        # mlx_whisper с verbose=True печатает сегменты в stdout по мере транскрипции.
        # Перенаправляем stdout/stderr в logger чтобы это попадало в app.log.
        stdout_redirect = _LogWriter("[mlx-whisper] ")
        try:
            with redirect_stdout(stdout_redirect), redirect_stderr(stdout_redirect):
                result = mlx_whisper.transcribe(
                    str(audio_path),
                    path_or_hf_repo=f"mlx-community/whisper-{config.WHISPER_MODEL}-mlx",
                    language=config.WHISPER_LANGUAGE,
                    word_timestamps=True,
                    verbose=True,
                )
            stdout_redirect.flush()
        finally:
            done.set()
            hb.join(timeout=1.0)

        log.info("mlx-whisper: done in %.1f s", time.monotonic() - t0)

        segments: list[TranscriptionSegment] = []
        all_segs = result.get("segments", [])
        total_duration = all_segs[-1]["end"] if all_segs else 0.0
        for seg in all_segs:
            words = []
            probs = []
            for w in seg.get("words", []):
                prob = w.get("probability", 1.0)
                words.append(TranscriptionWord(
                    start=w["start"],
                    end=w["end"],
                    word=w["word"],
                    probability=prob,
                ))
                probs.append(prob)

            confidence = sum(probs) / len(probs) if probs else 1.0
            segments.append(TranscriptionSegment(
                start=seg["start"],
                end=seg["end"],
                text=seg["text"].strip(),
                confidence=confidence,
                words=words,
            ))
            if on_progress:
                on_progress(seg["end"], total_duration)

        duration = result["segments"][-1]["end"] if result.get("segments") else 0.0
        return TranscriptionResult(
            segments=segments,
            language=result.get("language", config.WHISPER_LANGUAGE),
            duration=duration,
        )
