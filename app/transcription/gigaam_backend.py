"""
Транскрипция через GigaAM (Sber, native RU).

GigaAM запускается в отдельном subprocess — изоляция от ctranslate2/cuDNN
в основном процессе.
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

_WORKER    = Path(__file__).parent / "gigaam_worker.py"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def unload() -> None:
    """No-op: модель живёт в subprocess и выгружается вместе с ним."""


class GigaAMBackend(TranscriptionBackend):

    def transcribe(
        self,
        audio_path: Path,
        on_progress: Optional[Callable[[float, float], None]] = None,
    ) -> TranscriptionResult:
        proc = subprocess.Popen(
            [sys.executable, "-u", str(_WORKER), str(audio_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            cwd=str(_REPO_ROOT),
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )

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
                    log.debug("[gigaam_worker] %s", line)

        t = threading.Thread(target=_pipe_stderr, daemon=True)
        t.start()

        stdout = proc.stdout.read()
        proc.wait()
        t.join()

        if proc.returncode != 0:
            raise RuntimeError(
                f"gigaam_worker завершился с кодом {proc.returncode}"
            )

        data = json.loads(stdout)

        segments = [
            TranscriptionSegment(
                start=s["start"],
                end=s["end"],
                text=s["text"],
                confidence=s.get("confidence", 1.0),
                words=[TranscriptionWord(**w) for w in s.get("words", [])],
            )
            for s in data["segments"]
        ]
        return TranscriptionResult(
            segments=segments,
            language=data["language"],
            duration=data["duration"],
        )
