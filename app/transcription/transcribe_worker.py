"""
Worker-процесс транскрипции (subprocess).

Запускается дочерним процессом из faster_whisper_backend.py, чтобы
ctranslate2 (whisper) и PyTorch (pyannote) никогда не оказывались в
одном адресном пространстве — иначе cudnn64_9.dll конфликт → SIGABRT.

Вход:  argv[1] = абсолютный путь к аудиофайлу (WAV / MP4 и т.п.)
Выход: JSON {segments, language, duration} в stdout
       PROGRESS:<cur>/<total>  + обычные логи — в stderr
"""
import sys

# ────────────────────────────────────────────────────────────────────────
# КРИТИЧЕСКИ ВАЖНО: блокируем `import torch` ПЕРЕД любым импортом ctranslate2.
#
# ctranslate2 при загрузке (__init__ → converters → transformers.py) делает:
#     try:
#         import torch
#     except ImportError:
#         pass
# Если torch есть — он подтягивает свою cuDNN 9.1.0 в адресное пространство,
# а ctranslate2 бандлит cuDNN 9.10.2 (другой бинарь, тот же `cudnn64_9.dll`).
# Две версии одной DLL → stack corruption через несколько минут работы
# (0xC0000409, BEX64) → процесс убивается Windows без traceback.
#
# Подкладываем None в sys.modules — `import torch` падает ImportError и
# ctranslate2 благополучно работает без него (ему torch для инференса не нужен).
# ────────────────────────────────────────────────────────────────────────
sys.modules["torch"] = None  # type: ignore[assignment]

import json
import logging
from pathlib import Path

# Добавляем корень репозитория в sys.path чтобы импортировать config
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import config  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)


def main() -> None:
    if len(sys.argv) < 2:
        log.error("Usage: transcribe_worker.py <audio_path>")
        sys.exit(1)

    audio_path = Path(sys.argv[1])
    if not audio_path.exists():
        log.error("File not found: %s", audio_path)
        sys.exit(1)

    # ──────────────────────────────────────────────────────────────────────
    # КРИТИЧЕСКИ ВАЖНО: НЕ ИМПОРТИРОВАТЬ torch в этом процессе.
    # torch бандлит cuDNN 9.1.0, ctranslate2 бандлит cuDNN 9.10.2 — обе либы
    # лезут за одним `cudnn64_9.dll`, порядок загрузки лотерея, через несколько
    # минут работы случается stack corruption (0xC0000409, BEX64) и процесс
    # умирает без Python traceback.
    # CUDA-детекция — через ctranslate2; VRAM-инфо — через nvidia-smi.
    # ──────────────────────────────────────────────────────────────────────
    import ctranslate2
    from faster_whisper import WhisperModel

    # ВНИМАНИЕ: транскрипция на CPU ЯВНО ЗАПРЕЩЕНА.
    # Если CUDA недоступна — фейлимся, а не молча переключаемся на CPU
    # (CPU занимает 30-40 минут на час аудио → неприемлемо).
    if ctranslate2.get_cuda_device_count() < 1:
        log.error("CUDA is not available. CPU transcription is disabled by policy.")
        sys.exit(1)

    device  = "cuda"
    compute = "float16"

    # VRAM-инфо через nvidia-smi (без torch — чтобы не тянуть его cuDNN)
    try:
        import subprocess
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            text=True, timeout=5,
        )
        free_mib, total_mib = (int(x) for x in out.strip().split(", "))
        log.info("VRAM free %.1f GB / %.1f GB", free_mib / 1024, total_mib / 1024)
    except Exception as e:
        log.warning("nvidia-smi failed: %s", e)

    log.info("loading %s on %s...", config.WHISPER_MODEL, device)
    try:
        model = WhisperModel(config.WHISPER_MODEL, device=device, compute_type=compute)
        log.info("model loaded, transcribing %s", audio_path.name)

        raw_segments, info = model.transcribe(
            str(audio_path),
            language=config.WHISPER_LANGUAGE,
            word_timestamps=True,
            vad_filter=True,
        )

        total_dur = info.duration or 0.0
        segments = []
        for seg in raw_segments:
            words = []
            probs = []
            if seg.words:
                for w in seg.words:
                    words.append({
                        "start": w.start, "end": w.end,
                        "word": w.word, "probability": w.probability,
                    })
                    probs.append(w.probability)
            confidence = sum(probs) / len(probs) if probs else 1.0
            segments.append({
                "start": seg.start, "end": seg.end,
                "text": seg.text.strip(), "confidence": confidence,
                "words": words,
            })
            # Прогресс — парсится родителем из stderr
            print(f"PROGRESS:{seg.end:.3f}/{total_dur:.3f}", file=sys.stderr, flush=True)

        log.info("done: %d segments, lang=%s, duration=%.1f s",
                 len(segments), info.language, total_dur)

        print(json.dumps({"segments": segments, "language": info.language, "duration": total_dur}))

    except Exception as exc:
        log.error("transcription failed: %s: %s", type(exc).__name__, exc, exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
