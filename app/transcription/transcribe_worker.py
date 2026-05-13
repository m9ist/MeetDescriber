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

# Блокируем `import torch` ПЕРЕД любым импортом ctranslate2 (см. подробный
# комментарий ниже про cuDNN-конфликт).
sys.modules["torch"] = None  # type: ignore[assignment]

import faulthandler
import json
import logging
import os
import time
import traceback
from pathlib import Path

# ────────────────────────────────────────────────────────────────────────
# Диагностика крашей: faulthandler пишет стектрейсы всех Python-тредов
# при получении SIGABRT/SIGSEGV/SIGFPE/SIGBUS/SIGILL. Это не помогает,
# если процесс убивается __fastfail() мимо Python — но если хоть какой
# Python-обработчик ещё жив, мы получим картину.
# ────────────────────────────────────────────────────────────────────────
_fh_path = Path(__file__).resolve().parents[2] / "transcribe_worker_fault.log"
_fh_file = open(_fh_path, "a", buffering=1, encoding="utf-8")
_fh_file.write(f"\n=== worker start pid={os.getpid()} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
faulthandler.enable(file=_fh_file)
# Каждые 30 секунд пишем дамп тредов — если worker зависнет, увидим где
faulthandler.dump_traceback_later(30, repeat=True, file=_fh_file)

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

    # Подтверждение: torch ДЕЙСТВИТЕЛЬНО не загружен
    log.info("torch blocked: sys.modules['torch']=%r", sys.modules.get("torch"))
    log.info("loaded modules count: %d", len(sys.modules))

    # Будем сохранять прогресс в отдельный JSON — даже при краше будут partial-сегменты
    partial_path = audio_path.parent / f"{audio_path.stem}_transcription_partial.json"

    def _vram_free_gb() -> float:
        try:
            import subprocess
            out = subprocess.check_output(
                ["nvidia-smi", "--query-gpu=memory.free",
                 "--format=csv,noheader,nounits"],
                text=True, timeout=3,
            )
            return int(out.strip()) / 1024
        except Exception:
            return -1.0

    log.info("loading %s on %s...", config.WHISPER_MODEL, device)
    try:
        model = WhisperModel(config.WHISPER_MODEL, device=device, compute_type=compute)
        log.info("model loaded; VRAM free %.1f GB", _vram_free_gb())
        log.info("starting transcription of %s", audio_path.name)

        # ВАЖНО: temperature=(0.0,) полностью отключает fallback-цикл.
        # Без этого whisper при сложных чанках (русский+шум+несколько голосов)
        # перебирает температуры 0.0..1.0, висит в generate_with_fallback на одном
        # сегменте 8+ минут и в итоге abort'ит ctranslate2 (см. faulthandler-дамп).
        #
        # Но при temperature=(0.0,) нечем гасить зацикливания (whisper на тихом
        # аудио штампует одно слово подряд). Поэтому:
        # - condition_on_previous_text=False — модель не цепляется за свой же
        #   предыдущий вывод "ага → ага → ага..."
        # - repetition_penalty=1.2 — softмерная penalty на повтор токенов
        # - no_repeat_ngram_size=3 — жёсткий запрет повторять 3-граммы в одном
        #   проходе декодирования
        #
        # word_timestamps=False — слишком дорого + не используется downstream.
        # vad_filter=True оставляем — отрезает тишину и снижает кол-во чанков.
        raw_segments, info = model.transcribe(
            str(audio_path),
            language=config.WHISPER_LANGUAGE,
            word_timestamps=False,
            vad_filter=True,
            temperature=(0.0,),
            compression_ratio_threshold=None,
            log_prob_threshold=None,
            condition_on_previous_text=False,
            repetition_penalty=1.2,
            no_repeat_ngram_size=3,
        )

        total_dur = info.duration or 0.0
        log.info("transcribe() returned generator; audio duration=%.1f s, lang=%s",
                 total_dur, info.language)

        import math
        segments = []
        last_log_t = time.monotonic()
        for seg_idx, seg in enumerate(raw_segments):
            # Segment-уровень confidence: exp(avg_logprob).
            # avg_logprob — natural log средней вероятности токенов, обычно
            # около -0.1..-0.5 для нормальной речи.
            confidence = math.exp(seg.avg_logprob) if seg.avg_logprob is not None else 1.0
            segments.append({
                "start": seg.start, "end": seg.end,
                "text": seg.text.strip(), "confidence": confidence,
                "words": [],
            })
            # Прогресс — парсится родителем из stderr
            print(f"PROGRESS:{seg.end:.3f}/{total_dur:.3f}", file=sys.stderr, flush=True)

            # Раз в 10 секунд: лог + сохранение partial + VRAM
            now = time.monotonic()
            if now - last_log_t >= 10:
                last_log_t = now
                log.info(
                    "seg #%d  audio=%.1f/%.1f s  segs_collected=%d  VRAM_free=%.1f GB",
                    seg_idx, seg.end, total_dur, len(segments), _vram_free_gb(),
                )
                try:
                    partial_path.write_text(json.dumps({
                        "segments": segments, "language": info.language,
                        "duration": total_dur, "partial": True,
                    }), encoding="utf-8")
                except Exception as e:
                    log.warning("partial save failed: %s", e)

        log.info("done: %d segments, lang=%s, duration=%.1f s",
                 len(segments), info.language, total_dur)

        # Финальный сохранён — удалим partial
        try:
            partial_path.unlink(missing_ok=True)
        except Exception:
            pass

        print(json.dumps({"segments": segments, "language": info.language, "duration": total_dur}))

    except BaseException as exc:
        log.error("transcription failed: %s: %s", type(exc).__name__, exc)
        log.error("traceback:\n%s", traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
