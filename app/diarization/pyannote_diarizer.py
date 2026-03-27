"""
Диаризация спикеров через pyannote.audio 4.x.

Особенность pyannote 4.x на Windows: torchcodec не работает без FFmpeg full-shared.
Поэтому аудио загружаем вручную через soundfile → torch.Tensor и передаём как dict.
"""
from __future__ import annotations

import wave
from dataclasses import dataclass
from pathlib import Path

import config


@dataclass
class DiarizationSegment:
    start: float     # секунды
    end: float
    speaker: str     # SPEAKER_00, SPEAKER_01, ...


_pipeline = None
_pipeline_lock = None


def _get_pipeline():
    global _pipeline, _pipeline_lock
    import threading
    if _pipeline_lock is None:
        _pipeline_lock = threading.Lock()
    with _pipeline_lock:
        if _pipeline is None:
            import torch
            from pyannote.audio import Pipeline
            _pipeline = Pipeline.from_pretrained(
                "pyannote/speaker-diarization-3.1",
                token=config.HUGGINGFACE_TOKEN,
            )
            # Принудительно CPU: после загрузки whisper-large-v3 (~3GB)
            # в VRAM места для pyannote не остаётся — hard crash в CUDA runtime.
            _pipeline.to(torch.device("cpu"))
    return _pipeline


def _load_wav_as_tensor(path: Path):
    """Загружает WAV как (1, samples) torch.Tensor без torchcodec."""
    import struct
    import torch

    with wave.open(str(path), "rb") as wf:
        n_channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        frame_rate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sample_width == 2:
        fmt = f"<{len(raw) // 2}h"
        samples = struct.unpack(fmt, raw)
        tensor = torch.tensor(samples, dtype=torch.float32) / 32768.0
    elif sample_width == 4:
        fmt = f"<{len(raw) // 4}i"
        samples = struct.unpack(fmt, raw)
        tensor = torch.tensor(samples, dtype=torch.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported sample width: {sample_width}")

    # Микшируем каналы в моно если нужно
    if n_channels > 1:
        tensor = tensor.reshape(-1, n_channels).mean(dim=1)

    return tensor.unsqueeze(0), frame_rate  # (1, samples), rate


class PyannoteDiarizer:

    def diarize(self, audio_path: Path) -> list[DiarizationSegment]:
        pipeline = _get_pipeline()
        waveform, rate = _load_wav_as_tensor(audio_path)

        audio = {"waveform": waveform.cpu(), "sample_rate": rate}
        diarization = pipeline(audio)

        segments: list[DiarizationSegment] = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append(DiarizationSegment(
                start=turn.start,
                end=turn.end,
                speaker=speaker,
            ))

        return sorted(segments, key=lambda s: s.start)
