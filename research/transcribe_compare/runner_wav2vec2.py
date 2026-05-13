"""
Wav2Vec2-XLS-R-300M (russian fine-tune) runner.

Model: jonatasgrosman/wav2vec2-large-xlsr-53-russian
- Только CTC-decoding (без attention/decoder)
- БЕЗ пунктуации, БЕЗ автоматических таймстампов сегментов
- Сегментируем по диаризации (или по фиксированным окнам)

Использование:
    python runner_wav2vec2.py <wav_path> <out_dir>

Запускать ИЗ venv: research/transcribe_compare/.venv
"""
from __future__ import annotations

import argparse
import gc
import json
import logging
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("wav2vec2_runner")

MODEL_ID = "jonatasgrosman/wav2vec2-large-xlsr-53-russian"


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
    ap.add_argument("--chunk-sec", type=float, default=20.0,
                    help="Длина окна для feed-forward (сек)")
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    import torch
    import numpy as np
    from transformers import AutoModelForCTC, Wav2Vec2Processor

    device = "cuda" if torch.cuda.is_available() else "cpu"
    timing: dict = {"tool": "wav2vec2-xlsr-russian", "wav": str(args.wav)}

    # ── Загрузка ──────────────────────────────────────────────────────────
    log.info("Loading model %s on %s...", MODEL_ID, device)
    t0 = time.monotonic()
    # Используем Wav2Vec2Processor вместо AutoProcessor чтобы не тащить kenlm-LM
    # decoder (на Windows установить kenlm — отдельная морока).
    processor = Wav2Vec2Processor.from_pretrained(MODEL_ID)
    model = AutoModelForCTC.from_pretrained(MODEL_ID).to(device)
    model.eval()
    timing["model_load_sec"] = round(time.monotonic() - t0, 2)
    log.info("  loaded in %.1fs", timing["model_load_sec"])

    # ── Аудио ─────────────────────────────────────────────────────────────
    log.info("Loading audio...")
    audio = _load_audio_pyav(args.wav)
    total_dur = len(audio) / 16000
    log.info("  %.1fs (%d samples)", total_dur, len(audio))

    # ── Инференс по окнам ─────────────────────────────────────────────────
    chunk_samples = int(args.chunk_sec * 16000)
    overlap_samples = int(2.0 * 16000)  # 2 сек overlap

    segments = []
    t0 = time.monotonic()
    log.info("Inferring with chunk=%.1fs overlap=2.0s...", args.chunk_sec)
    with torch.no_grad():
        pos = 0
        while pos < len(audio):
            end = min(pos + chunk_samples, len(audio))
            window = audio[pos:end]
            if len(window) < 16000 * 0.5:
                break

            inputs = processor(window, sampling_rate=16000, return_tensors="pt")
            inputs = {k: v.to(device) for k, v in inputs.items()}
            logits = model(**inputs).logits
            ids = torch.argmax(logits, dim=-1)
            text = processor.batch_decode(ids)[0].strip()

            if text:
                segments.append({
                    "start": pos / 16000,
                    "end": end / 16000,
                    "text": text,
                })
            pos = end - overlap_samples if end < len(audio) else end

            # Прогресс на stderr
            pct = pos / len(audio) * 100
            print(f"\r  {pct:5.1f}%  {_fmt_ts(pos/16000)} / {_fmt_ts(total_dur)}    ",
                  end="", flush=True, file=sys.stderr)

    print(file=sys.stderr)
    timing["transcribe_sec"] = round(time.monotonic() - t0, 2)
    timing["final_segments"] = len(segments)
    timing["vram_peak_gb"] = round(_vram_used_gb(), 2)
    log.info("Done in %.1fs, %d segments", timing["transcribe_sec"], len(segments))

    # ── Markdown ──────────────────────────────────────────────────────────
    lines = [
        f"# {args.wav.stem}",
        "",
        f"Tool: **Wav2Vec2-XLS-R-Russian** ({MODEL_ID})",
        f"Длительность: {_fmt_ts(total_dur)}  |  Сегментов: {len(segments)}",
        f"⚠ Без пунктуации, без диаризации. Сегментация: фикс. окна {args.chunk_sec}s + overlap 2s",
        "",
        "---",
        "",
    ]
    for seg in segments:
        ts = _fmt_ts(seg["start"])
        lines.append(f"**[{ts}]** {seg['text']}")
        lines.append("")

    md_path = args.out_dir / "wav2vec2_transcription.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Markdown: %s", md_path)

    json_path = args.out_dir / "wav2vec2_timing.json"
    json_path.write_text(json.dumps(timing, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Timing: %s", json_path)
    log.info("Summary: %s", timing)


if __name__ == "__main__":
    main()
