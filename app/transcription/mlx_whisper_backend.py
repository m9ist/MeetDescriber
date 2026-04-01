"""
Транскрипция через mlx-whisper (Mac Apple Silicon).
"""
from __future__ import annotations

from pathlib import Path

import config
from app.transcription.backend import (
    TranscriptionBackend,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionWord,
)


class MLXWhisperBackend(TranscriptionBackend):

    def transcribe(self, audio_path: Path, on_progress=None) -> TranscriptionResult:
        import mlx_whisper

        result = mlx_whisper.transcribe(
            str(audio_path),
            path_or_hf_repo=f"mlx-community/whisper-{config.WHISPER_MODEL}-mlx",
            language=config.WHISPER_LANGUAGE,
            word_timestamps=True,
        )

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
