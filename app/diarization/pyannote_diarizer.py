"""
Диаризация спикеров через pyannote.audio 4.x.

pyannote (PyTorch) и faster-whisper (ctranslate2) оба бандлят cudnn64_9.dll.
При совместной загрузке в одном адресном пространстве — stack corruption
(0xc0000409, BEX64). Решение: diarize_worker.py запускается отдельным
subprocess-ом; pyannote живёт только в нём и умирает вместе с процессом.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("app")

_WORKER = Path(__file__).parent / "diarize_worker.py"
_REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass
class DiarizationSegment:
    start: float     # секунды
    end: float
    speaker: str     # SPEAKER_00, SPEAKER_01, ...


def unload() -> None:
    """No-op: модели живут в subprocess, выгружаются вместе с ним."""


class PyannoteDiarizer:

    def diarize(self, audio_path: Path) -> list[DiarizationSegment]:
        proc = subprocess.Popen(
            [sys.executable, str(_WORKER), str(audio_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(_REPO_ROOT),
            env=os.environ.copy(),
        )

        # Форвардим stderr worker'а в app.log в реальном времени
        def _pipe_stderr() -> None:
            for line in proc.stderr:
                log.debug("[diarize_worker] %s", line.rstrip())

        t = threading.Thread(target=_pipe_stderr, daemon=True)
        t.start()

        stdout, _ = proc.communicate()
        t.join()

        if proc.returncode != 0:
            raise RuntimeError(
                f"diarize_worker завершился с кодом {proc.returncode}"
            )

        data = json.loads(stdout)
        return [DiarizationSegment(**d) for d in data]
