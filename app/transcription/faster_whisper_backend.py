"""
Транскрипция через faster-whisper (Windows + CUDA / CPU).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import config
from app.transcription.backend import (
    TranscriptionBackend,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionWord,
)

_model = None
_model_lock = None


def _get_model():
    global _model, _model_lock
    import threading
    if _model_lock is None:
        _model_lock = threading.Lock()
    with _model_lock:
        if _model is None:
            from faster_whisper import WhisperModel
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            compute = "float16" if device == "cuda" else "int8"
            _model = WhisperModel(
                config.WHISPER_MODEL,
                device=device,
                compute_type=compute,
            )
    return _model


class FasterWhisperBackend(TranscriptionBackend):

    def transcribe(
        self,
        audio_path: Path,
        on_progress: Optional[Callable[[float, float], None]] = None,
    ) -> TranscriptionResult:
        model = _get_model()

        raw_segments, info = model.transcribe(
            str(audio_path),
            language=config.WHISPER_LANGUAGE,
            word_timestamps=True,
            vad_filter=True,
        )

        total = info.duration or 0.0
        segments: list[TranscriptionSegment] = []
        for seg in raw_segments:
            words = []
            probs = []
            if seg.words:
                for w in seg.words:
                    words.append(TranscriptionWord(
                        start=w.start,
                        end=w.end,
                        word=w.word,
                        probability=w.probability,
                    ))
                    probs.append(w.probability)

            confidence = sum(probs) / len(probs) if probs else 1.0
            segments.append(TranscriptionSegment(
                start=seg.start,
                end=seg.end,
                text=seg.text.strip(),
                confidence=confidence,
                words=words,
            ))
            if on_progress and total > 0:
                on_progress(seg.end, total)

        return TranscriptionResult(
            segments=segments,
            language=info.language,
            duration=info.duration,
        )
