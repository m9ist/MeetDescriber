"""
Worker-процесс транскрипции через GigaAM (Sber, native RU).

Запускается дочерним процессом из gigaam_backend.py — изоляция гарантирует
что torch/cuDNN из gigaam не конфликтуют с ctranslate2 в parent.

Вход:  argv[1] = путь к аудиофайлу
       argv[2] = (опц.) модель: v2_rnnt | v2_ctc
Выход: JSON {segments, language, duration} в stdout
       PROGRESS:cur/total + логи — в stderr

Архитектура:
  1. Загружаем аудио как PCM int16 mono 16kHz (через PyAV, без ffmpeg)
  2. silero VAD (из faster_whisper) режет на чанки по 0.5-20 сек
  3. GigaAM транскрибирует каждый чанк
  4. Сегменты возвращаются с реальными таймстампами из VAD

Без пунктуации/диаризации (это не задача транскрипционного движка).
"""
import sys

# Блокируем `import torch` мы НЕ можем (gigaam использует torch напрямую).
# Зато ctranslate2 здесь не загружается, поэтому конфликта cuDNN нет.

import faulthandler
import json
import logging
import os
import time
import wave
from pathlib import Path

# Корень репозитория в sys.path для импорта config
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# .env (HF_TOKEN нужен для silero VAD загрузки)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    if not os.environ.get("HF_TOKEN") and os.environ.get("HUGGINGFACE_TOKEN"):
        os.environ["HF_TOKEN"] = os.environ["HUGGINGFACE_TOKEN"]
except ImportError:
    pass

import config  # noqa: E402

# GigaAM зовёт ffmpeg внутри load_audio. static-ffmpeg бандлит бинарь и
# добавляет его в PATH — делаем это до загрузки gigaam.
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)

# faulthandler — стек тредов при SIGABRT/SIGSEGV
_fh_path = Path(__file__).resolve().parents[2] / "gigaam_worker_fault.log"
_fh_file = open(_fh_path, "a", buffering=1, encoding="utf-8")
_fh_file.write(f"\n=== worker start pid={os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
faulthandler.enable(file=_fh_file)


def _load_audio_pyav_int16(path: Path):
    """Грузит аудио/видео в (1, samples) int16 mono 16kHz через PyAV.
    Возвращает (samples_int16_np, sample_rate)."""
    import av
    import numpy as np

    container = av.open(str(path))
    audio_stream = container.streams.audio[0]
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)

    chunks = []
    for frame in container.decode(audio_stream):
        for r in resampler.resample(frame):
            chunks.append(r.to_ndarray().reshape(-1).astype(np.int16))
    for r in resampler.resample(None):
        chunks.append(r.to_ndarray().reshape(-1).astype(np.int16))
    container.close()
    return np.concatenate(chunks), 16000


def main() -> None:
    if len(sys.argv) < 2:
        log.error("Usage: gigaam_worker.py <audio_path> [model]")
        sys.exit(1)

    audio_path = Path(sys.argv[1])
    if not audio_path.exists():
        log.error("File not found: %s", audio_path)
        sys.exit(1)

    model_name = sys.argv[2] if len(sys.argv) > 2 else config.GIGAAM_MODEL

    # ── Импорты (тяжёлые) ────────────────────────────────────────────────
    import torch
    import numpy as np
    import gigaam
    from faster_whisper.vad import get_speech_timestamps, VadOptions

    device = "cuda" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        log.error("CUDA is not available. GigaAM CPU is too slow for production.")
        sys.exit(1)

    # ── VRAM info через nvidia-smi ───────────────────────────────────────
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

    # ── Загрузка модели ──────────────────────────────────────────────────
    log.info("Loading GigaAM %s on %s...", model_name, device)
    t0 = time.monotonic()
    model = gigaam.load_model(model_name, device=device)
    log.info("Model loaded in %.1fs", time.monotonic() - t0)

    # ── Аудио + VAD ──────────────────────────────────────────────────────
    log.info("Loading audio %s...", audio_path.name)
    samples_i16, sample_rate = _load_audio_pyav_int16(audio_path)
    total_dur = len(samples_i16) / sample_rate
    samples_f32 = samples_i16.astype(np.float32) / 32768.0

    log.info("Running silero VAD (audio %.1fs)...", total_dur)
    t_vad = time.monotonic()
    vad_segments = get_speech_timestamps(
        samples_f32,
        VadOptions(min_silence_duration_ms=500, max_speech_duration_s=20.0),
    )
    log.info("VAD: %d speech regions in %.1fs",
             len(vad_segments), time.monotonic() - t_vad)

    # ── Транскрипция по чанкам ───────────────────────────────────────────
    import tempfile

    segments = []
    last_log_t = time.monotonic()
    t_trans_start = time.monotonic()
    for i, vs in enumerate(vad_segments):
        start_sec = vs["start"] / sample_rate
        end_sec = vs["end"] / sample_rate
        if end_sec - start_sec < 0.3:
            continue

        chunk_i16 = samples_i16[vs["start"]:vs["end"]]
        tmp_wav = Path(tempfile.mktemp(suffix=".wav"))
        with wave.open(str(tmp_wav), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(chunk_i16.tobytes())
        try:
            text = model.transcribe(str(tmp_wav)).strip()
        finally:
            try: tmp_wav.unlink()
            except OSError: pass

        if text:
            segments.append({
                "start": start_sec,
                "end": end_sec,
                "text": text,
                "confidence": 1.0,  # GigaAM не отдаёт confidence
                "words": [],
            })

        # PROGRESS — парсится родителем
        print(f"PROGRESS:{end_sec:.3f}/{total_dur:.3f}", file=sys.stderr, flush=True)

        # Раз в 10 сек — статус-лог
        now = time.monotonic()
        if now - last_log_t >= 10:
            last_log_t = now
            pct = (i + 1) / len(vad_segments) * 100
            log.info("chunk #%d/%d (%.0f%%)  audio=%.1f/%.1f s  collected=%d",
                     i + 1, len(vad_segments), pct, end_sec, total_dur, len(segments))

    log.info("Done in %.1fs (%d segments out of %d VAD chunks)",
             time.monotonic() - t_trans_start, len(segments), len(vad_segments))

    print(json.dumps({
        "segments": segments,
        "language": "ru",
        "duration": total_dur,
    }))


if __name__ == "__main__":
    main()
