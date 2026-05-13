"""
WhisperX runner: транскрипция + (опц.) align + (опц.) диаризация.

Использование:
    python runner_whisperx.py <wav_path> <out_dir> [--no-diarize] [--no-align]

Сохраняет:
    <out_dir>/whisperx_transcription.md  — markdown с таймстампами и спикерами
    <out_dir>/whisperx_timing.json       — wall time, VRAM peak, кол-во сегментов

Запускать ИЗ ИЗОЛИРОВАННОГО venv: research/transcribe_compare/.venv
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import os
import sys
import time
from pathlib import Path

# .env (для HUGGINGFACE_TOKEN)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("whisperx_runner")


def _fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _vram_used_gb() -> float:
    import torch
    if not torch.cuda.is_available():
        return 0.0
    return torch.cuda.max_memory_allocated() / 1e9


def _load_audio_pyav(path: Path):
    """Загружает любой аудио/видео файл как float32 mono 16kHz numpy array.
    whisperx.load_audio зовёт ffmpeg в subprocess — у нас его в PATH нет."""
    import av
    import numpy as np

    container = av.open(str(path))
    audio_stream = container.streams.audio[0]
    resampler = av.AudioResampler(format="flt", layout="mono", rate=16000)

    chunks = []
    for frame in container.decode(audio_stream):
        for r in resampler.resample(frame):
            chunks.append(r.to_ndarray().reshape(-1))
    for r in resampler.resample(None):
        chunks.append(r.to_ndarray().reshape(-1))
    container.close()
    return np.concatenate(chunks).astype(np.float32)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--no-diarize", action="store_true")
    ap.add_argument("--no-align", action="store_true")
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    import torch
    import whisperx

    device = "cuda" if torch.cuda.is_available() else "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    timing: dict = {"tool": "whisperx", "wav": str(args.wav)}

    # ── Загружаем модель ───────────────────────────────────────────────────
    t0 = time.monotonic()
    log.info("Loading whisper model large-v3 on %s...", device)
    model = whisperx.load_model("large-v3", device, compute_type=compute_type, language="ru")
    timing["model_load_sec"] = round(time.monotonic() - t0, 2)
    log.info("  loaded in %.1fs", timing["model_load_sec"])

    # ── Транскрибируем ─────────────────────────────────────────────────────
    log.info("Loading audio %s...", args.wav)
    audio = _load_audio_pyav(args.wav)

    log.info("Transcribing...")
    t0 = time.monotonic()
    result = model.transcribe(audio, batch_size=16, language="ru")
    timing["transcribe_sec"] = round(time.monotonic() - t0, 2)
    timing["transcribe_segments"] = len(result.get("segments", []))
    log.info("  transcribed in %.1fs (%d segments)",
             timing["transcribe_sec"], timing["transcribe_segments"])

    # Удалим whisper-модель чтобы освободить VRAM
    del model
    gc.collect()
    torch.cuda.empty_cache()

    # ── Forced alignment (для точных таймстампов слов) ─────────────────────
    if not args.no_align:
        log.info("Loading align model...")
        t0 = time.monotonic()
        try:
            align_model, metadata = whisperx.load_align_model(
                language_code="ru", device=device,
            )
            timing["align_load_sec"] = round(time.monotonic() - t0, 2)

            t0 = time.monotonic()
            log.info("Aligning...")
            result = whisperx.align(
                result["segments"], align_model, metadata, audio, device,
                return_char_alignments=False,
            )
            timing["align_sec"] = round(time.monotonic() - t0, 2)
            log.info("  aligned in %.1fs", timing["align_sec"])
            del align_model
            gc.collect()
            torch.cuda.empty_cache()
        except Exception as e:
            log.warning("Align failed (skipping): %s", e)
            timing["align_error"] = str(e)

    # ── Диаризация (опц.) ──────────────────────────────────────────────────
    if not args.no_diarize:
        hf_token = os.environ.get("HUGGINGFACE_TOKEN") or os.environ.get("HF_TOKEN")
        if not hf_token:
            log.warning("No HF token — skipping diarization")
        else:
            log.info("Loading diarization pipeline...")
            t0 = time.monotonic()
            try:
                # whisperx 3.8+ переехал на pyannote-audio 4.x; модуль может звать
                # либо whisperx.diarize.DiarizationPipeline, либо whisperx.DiarizationPipeline
                Diar = getattr(whisperx, "DiarizationPipeline", None)
                if Diar is None:
                    from whisperx.diarize import DiarizationPipeline as Diar
                diar_pipeline = Diar(use_auth_token=hf_token, device=device)
                timing["diar_load_sec"] = round(time.monotonic() - t0, 2)

                t0 = time.monotonic()
                log.info("Diarizing...")
                diarize_segments = diar_pipeline(audio)
                timing["diar_sec"] = round(time.monotonic() - t0, 2)
                log.info("  diarized in %.1fs", timing["diar_sec"])

                result = whisperx.assign_word_speakers(diarize_segments, result)
            except Exception as e:
                log.warning("Diarization failed (skipping): %s", e)
                timing["diar_error"] = str(e)

    # ── VRAM peak ──────────────────────────────────────────────────────────
    timing["vram_peak_gb"] = round(_vram_used_gb(), 2)

    # ── Запись markdown ────────────────────────────────────────────────────
    segments = result.get("segments", [])
    timing["final_segments"] = len(segments)

    lines = [
        f"# {args.wav.stem}",
        "",
        f"Tool: **WhisperX** {getattr(__import__('whisperx'), '__version__', '?')}",
        f"Длительность: {_fmt_ts(audio.shape[0] / 16000)}  |  Сегментов: {len(segments)}",
        "",
        "---",
        "",
    ]
    for seg in segments:
        ts = _fmt_ts(seg.get("start", 0))
        speaker = seg.get("speaker", "")
        text = seg.get("text", "").strip()
        if speaker:
            lines.append(f"**[{ts}]** **{speaker}:** {text}")
        else:
            lines.append(f"**[{ts}]** {text}")
        lines.append("")

    md_path = args.out_dir / "whisperx_transcription.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Markdown saved: %s", md_path)

    json_path = args.out_dir / "whisperx_timing.json"
    json_path.write_text(json.dumps(timing, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Timing saved: %s", json_path)
    log.info("Summary: %s", timing)


if __name__ == "__main__":
    main()
