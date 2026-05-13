"""
Транскрипция одного аудио/видео файла + диаризация (опц.) без записи в БД.

Использует тот же subprocess-пайплайн что и основное приложение:
  - faster-whisper (whisper-large-v3, CUDA) для транскрипции
  - pyannote.audio для диаризации спикеров

PyAV декодирует MP4/MKV/WebM/MP3 прозрачно — ffmpeg в PATH не нужен.

Использование:
    python tools/transcribe_file.py <путь>                # + диаризация
    python tools/transcribe_file.py <путь> --no-diarize   # только текст
    python tools/transcribe_file.py <путь> -o out.md      # свой путь вывода

Подробности: docs/TRANSCRIBE_FILE.md
"""
from __future__ import annotations

import argparse
import logging
import sys
import tempfile
import time
import wave
from pathlib import Path

# Скрипт лежит в tools/, импорты из app/ в корне репозитория.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Load .env (для HUGGINGFACE_TOKEN при диаризации)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

# На Windows консоль часто cp1251 — принудительно utf-8 для вывода
_stream = (
    open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    if hasattr(sys.stdout, "fileno") else sys.stdout
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    handlers=[logging.StreamHandler(_stream)],
)
log = logging.getLogger("transcribe_file")


def _fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _extract_wav_16k_mono(input_path: Path, output_wav: Path) -> None:
    """Извлекает аудио в WAV 16kHz mono через PyAV (для diarize_worker).

    ВАЖНО: используем frame.to_ndarray() вместо frame.planes[0] —
    у planes есть паддинг под SIMD, и его длина в байтах больше реального
    количества семплов в кадре. С `bytes(planes[0])` WAV получится на ~22%
    длиннее реального аудио → у whisper смещаются таймстампы и он находит
    только пару сегментов в начале.
    """
    import av

    container = av.open(str(input_path))
    audio_stream = container.streams.audio[0]

    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)

    with wave.open(str(output_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        for frame in container.decode(audio_stream):
            for resampled in resampler.resample(frame):
                # to_ndarray даёт shape (1, samples) для mono s16 — берём как bytes
                wf.writeframes(resampled.to_ndarray().tobytes())
        # flush resampler (он буферизует последние семплы)
        for resampled in resampler.resample(None):
            wf.writeframes(resampled.to_ndarray().tobytes())

    container.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("input", type=Path, help="путь к аудио/видео файлу")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="markdown-вывод (по умолчанию <input>_transcription.md)")
    ap.add_argument("--no-diarize", action="store_true",
                    help="пропустить диаризацию — будет один блок текста без спикеров")
    args = ap.parse_args()

    if not args.input.exists():
        log.error("Файл не найден: %s", args.input)
        sys.exit(1)

    output_path = args.output or args.input.with_name(args.input.stem + "_transcription.md")

    log.info("Вход:  %s", args.input)
    log.info("Выход: %s", output_path)
    log.info("Диаризация: %s", "выключена" if args.no_diarize else "включена")

    # Импорты после парсинга, чтобы --help работал быстро
    from app.transcription.backend import get_backend
    backend = get_backend()

    with tempfile.TemporaryDirectory(prefix="meetdesc_") as tmp_dir:
        tmp_dir = Path(tmp_dir)
        wav_path = tmp_dir / "audio_16k_mono.wav"

        log.info("Извлекаем аудио (16kHz mono WAV)...")
        t0 = time.monotonic()
        _extract_wav_16k_mono(args.input, wav_path)
        log.info("Извлечено за %.1f s, размер %.1f MB",
                 time.monotonic() - t0, wav_path.stat().st_size / 1e6)

        # ── Диаризация (опционально) ─────────────────────────────────────
        diarization = []
        if not args.no_diarize:
            from app.diarization.pyannote_diarizer import PyannoteDiarizer
            log.info("Запускаем диаризацию (pyannote, CUDA)...")
            t0 = time.monotonic()
            diarization = PyannoteDiarizer().diarize(wav_path)
            log.info("Диаризация: %.1f s, %d сегментов",
                     time.monotonic() - t0, len(diarization))

        # ── Транскрипция ─────────────────────────────────────────────────
        log.info("Запускаем транскрипцию (worker subprocess, CUDA + whisper-large-v3)...")
        t0 = time.monotonic()

        def on_progress(current: float, total: float) -> None:
            pct = current / total * 100 if total > 0 else 0.0
            print(f"\r  {pct:5.1f}%  {_fmt_ts(current)} / {_fmt_ts(total)}  ",
                  end="", flush=True, file=sys.stderr)

        result = backend.transcribe(wav_path, on_progress=on_progress)
        print(file=sys.stderr)  # перевод строки после прогресс-бара
        log.info("Транскрипция: %.1f s, %d сегментов, язык=%s",
                 time.monotonic() - t0, len(result.segments), result.language)

    # ── Выравнивание сегментов со спикерами ──────────────────────────────
    if diarization:
        from app.processing.pipeline import _assign_speakers, _build_speaker_map
        aligned = _assign_speakers(result, diarization)
        # _detect_names в основном приложении выдаёт мусорные имена
        # ("Ну", "Да", "Здесь" и т.п.) — там их потом редактируют вручную в UI.
        # В standalone-инструменте UI нет, оставляем чистые «Спикер N».
        speaker_map = _build_speaker_map(aligned, detected_names={}, saved_names={})
        log.info("Спикеры: %s", list(speaker_map.values()))
    else:
        aligned = [
            {"start": s.start, "end": s.end, "text": s.text,
             "confidence": s.confidence, "speaker": ""}
            for s in result.segments
        ]
        speaker_map = {}

    # ── Запись markdown ──────────────────────────────────────────────────
    lines = [
        f"# {args.input.stem}",
        "",
        f"Длительность: {_fmt_ts(result.duration)}  |  Язык: {result.language}  |  "
        f"Сегментов: {len(aligned)}",
    ]
    if speaker_map:
        lines.append(f"Спикеры: {', '.join(speaker_map.values())}")
    lines += ["", "---", ""]

    for seg in aligned:
        ts = _fmt_ts(seg["start"])
        speaker = speaker_map.get(seg["speaker"], "") if seg.get("speaker") else ""
        if speaker:
            lines.append(f"**[{ts}]** **{speaker}:** {seg['text']}")
        else:
            lines.append(f"**[{ts}]** {seg['text']}")
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Сохранено: %s", output_path)


if __name__ == "__main__":
    main()
