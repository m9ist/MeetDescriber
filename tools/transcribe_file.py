"""
Транскрипция одного аудио/видео файла без записи в БД и без диаризации.

Использует тот же ctranslate2-subprocess пайплайн что и основное приложение
(faster-whisper, whisper-large-v3, CUDA). PyAV декодирует MP4/MKV/WebM/MP3
прозрачно — ffmpeg в PATH не нужен.

Использование:
    python tools/transcribe_file.py <путь_к_файлу>
    python tools/transcribe_file.py <путь_к_файлу> -o <путь_к_выводу.md>

Результат — markdown с таймстампами рядом с исходником (или по -o).
Подробности: docs/TRANSCRIBE_FILE.md
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("input", type=Path, help="путь к аудио/видео файлу")
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="путь к markdown-выводу (по умолчанию <input>_transcription.md)")
    args = ap.parse_args()

    if not args.input.exists():
        log.error("Файл не найден: %s", args.input)
        sys.exit(1)

    output_path = args.output or args.input.with_name(args.input.stem + "_transcription.md")

    log.info("Вход:  %s", args.input)
    log.info("Выход: %s", output_path)

    # Импорт после парсинга аргументов, чтобы --help работал быстро
    from app.transcription.backend import get_backend
    backend = get_backend()

    log.info("Запускаем транскрипцию (worker subprocess, CUDA + whisper-large-v3)...")

    def on_progress(current: float, total: float) -> None:
        pct = current / total * 100 if total > 0 else 0.0
        # \r чтобы строка перерисовывалась
        print(f"\r  {pct:5.1f}%  {_fmt_ts(current)} / {_fmt_ts(total)}  ",
              end="", flush=True, file=sys.stderr)

    result = backend.transcribe(args.input, on_progress=on_progress)
    print(file=sys.stderr)  # перевод строки после прогресс-бара

    log.info(
        "Готово: %d сегментов, длительность %s, язык=%s",
        len(result.segments), _fmt_ts(result.duration), result.language,
    )

    # ── Запись markdown ────────────────────────────────────────────────────
    lines = [
        f"# {args.input.stem}",
        "",
        f"Длительность: {_fmt_ts(result.duration)}  |  Язык: {result.language}  |  "
        f"Сегментов: {len(result.segments)}",
        "",
        "---",
        "",
    ]
    for seg in result.segments:
        lines.append(f"**[{_fmt_ts(seg.start)}]** {seg.text}")
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Сохранено: %s", output_path)


if __name__ == "__main__":
    main()
