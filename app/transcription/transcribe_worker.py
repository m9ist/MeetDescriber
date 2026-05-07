"""
Worker-процесс транскрипции (subprocess).

Запускается дочерним процессом из faster_whisper_backend.py, чтобы
ctranslate2 (whisper) и PyTorch (pyannote) никогда не оказывались в
одном адресном пространстве — иначе cudnn64_9.dll конфликт → SIGABRT.

Вход:  argv[1] = абсолютный путь к аудиофайлу (WAV / MP4 и т.п.)
Выход: JSON {segments, language, duration} в stdout
       PROGRESS:<cur>/<total>  + обычные логи — в stderr
"""
import json
import logging
import sys
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

    import torch
    from faster_whisper import WhisperModel

    device  = "cuda" if torch.cuda.is_available() else "cpu"
    compute = "float16" if device == "cuda" else "int8"

    if torch.cuda.is_available():
        free, total = torch.cuda.mem_get_info()
        log.info("VRAM free %.1f GB / %.1f GB", free / 1e9, total / 1e9)

    log.info("loading %s on %s...", config.WHISPER_MODEL, device)
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


if __name__ == "__main__":
    main()
