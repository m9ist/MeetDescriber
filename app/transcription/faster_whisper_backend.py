"""
Транскрипция через faster-whisper (Windows + CUDA / CPU).

Whisper запускается в отдельном subprocess чтобы изолировать ctranslate2
от других CUDA-библиотек. Это устраняет SIGABRT при уничтожении модели
в pipeline-треде (cudnn64_9.dll конфликт с PyTorch cuDNN).
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

from app.transcription.backend import (
    TranscriptionBackend,
    TranscriptionResult,
    TranscriptionSegment,
    TranscriptionWord,
)

log = logging.getLogger("app")

_WORKER    = Path(__file__).parent / "transcribe_worker.py"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def unload() -> None:
    """No-op: модель живёт в subprocess и выгружается вместе с ним."""


class FasterWhisperBackend(TranscriptionBackend):

    def transcribe(
        self,
        audio_path: Path,
        on_progress: Optional[Callable[[float, float], None]] = None,
    ) -> TranscriptionResult:
        # -u (unbuffered Python I/O) обязательно: иначе stderr child-процесса
        # буферизуется при пайпинге, PROGRESS-строки накапливаются и status-окно
        # не показывает прогресс в реальном времени.
        proc = subprocess.Popen(
            [sys.executable, "-u", str(_WORKER), str(audio_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,           # line-buffered с parent-side
            cwd=str(_REPO_ROOT),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

        # stderr: PROGRESS:cur/total → on_progress; остальное → app.log
        def _pipe_stderr() -> None:
            for line in proc.stderr:
                line = line.rstrip()
                if line.startswith("PROGRESS:"):
                    if on_progress:
                        try:
                            cur_s, total_s = line[9:].split("/")
                            on_progress(float(cur_s), float(total_s))
                        except ValueError:
                            pass
                else:
                    log.debug("[transcribe_worker] %s", line)

        t = threading.Thread(target=_pipe_stderr, daemon=True)
        t.start()

        # ВАЖНО: НЕ вызываем proc.communicate() — она внутри стартует ещё один
        # reader-тред для stderr и гонится с нашим _pipe_stderr (Windows pipe
        # читается дважды → строки PROGRESS теряются). Читаем stdout вручную.
        stdout = proc.stdout.read()
        proc.wait()
        t.join()

        if proc.returncode != 0:
            raise RuntimeError(
                f"transcribe_worker завершился с кодом {proc.returncode}"
            )

        data = json.loads(stdout)

        segments = [
            TranscriptionSegment(
                start=s["start"],
                end=s["end"],
                text=s["text"],
                confidence=s["confidence"],
                words=[TranscriptionWord(**w) for w in s.get("words", [])],
            )
            for s in data["segments"]
        ]
        return TranscriptionResult(
            segments=segments,
            language=data["language"],
            duration=data["duration"],
        )
