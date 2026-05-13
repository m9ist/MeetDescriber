"""
GigaAM (Sber) runner.

Model: v2_rnnt — наибольшая точность из доступных публично.
Поддерживает длинные записи через `transcribe_longform()`.

Использование:
    python runner_gigaam.py <wav_path> <out_dir> [--model v2_rnnt|v2_ctc]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# .env (для HF_TOKEN — нужен GigaAM для VAD)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    # GigaAM ожидает HF_TOKEN, у нас в .env HUGGINGFACE_TOKEN
    if not os.environ.get("HF_TOKEN") and os.environ.get("HUGGINGFACE_TOKEN"):
        os.environ["HF_TOKEN"] = os.environ["HUGGINGFACE_TOKEN"]
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger("gigaam_runner")


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


def _mp4_to_wav_if_needed(input_path: Path) -> Path:
    """GigaAM требует WAV. Если на входе не .wav — конвертим во временный."""
    if input_path.suffix.lower() == ".wav":
        return input_path
    import tempfile, wave
    import av
    container = av.open(str(input_path))
    a = container.streams.audio[0]
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    tmp = Path(tempfile.mktemp(suffix=".wav"))
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        for frame in container.decode(a):
            for r in resampler.resample(frame):
                wf.writeframes(r.to_ndarray().tobytes())
        for r in resampler.resample(None):
            wf.writeframes(r.to_ndarray().tobytes())
    container.close()
    return tmp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("wav", type=Path)
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--model", default="v2_rnnt", help="v2_rnnt | v2_ctc")
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)

    import torch
    import gigaam
    import gigaam.preprocess as _gp

    # GigaAM зовёт ffmpeg внутри load_audio. В нашей среде его нет —
    # подменяем загрузчик на PyAV-вариант.
    _orig_load_audio = _gp.load_audio
    def _pyav_load_audio(audio_path, sample_rate=16000, return_format="float"):
        import av, numpy as np
        container = av.open(str(audio_path))
        a = container.streams.audio[0]
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
        chunks = []
        for frame in container.decode(a):
            for r in resampler.resample(frame):
                chunks.append(r.to_ndarray().reshape(-1).astype(np.int16))
        for r in resampler.resample(None):
            chunks.append(r.to_ndarray().reshape(-1).astype(np.int16))
        container.close()
        raw = np.concatenate(chunks)
        t = torch.from_numpy(raw)
        if return_format == "float":
            return t.float() / 32768.0
        return t
    _gp.load_audio = _pyav_load_audio
    # Также внутри gigaam.model импортируется load_audio из preprocess —
    # надо подменить и там
    import gigaam.model as _gm
    _gm.load_audio = _pyav_load_audio

    # GigaAM использует устаревший API pyannote: Pipeline.from_pretrained(...,
    # use_auth_token=...). В pyannote 4.x этот kwarg переименован в `token`.
    # Подменяем get_pipeline на корректный вариант.
    import gigaam.vad_utils as _gv
    from pyannote.audio import Pipeline as _PaPipeline
    def _patched_get_pipeline(device):
        if _gv._PIPELINE is not None:
            return _gv._PIPELINE.to(device)
        hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")
        if not hf_token:
            raise ValueError("HF_TOKEN environment variable is not set")
        _gv._PIPELINE = _PaPipeline.from_pretrained(
            "pyannote/voice-activity-detection", token=hf_token,
        )
        return _gv._PIPELINE.to(device)
    _gv.get_pipeline = _patched_get_pipeline

    device = "cuda" if torch.cuda.is_available() else "cpu"
    timing: dict = {"tool": f"gigaam-{args.model}", "wav": str(args.wav)}

    # ── Загрузка модели ───────────────────────────────────────────────────
    log.info("Loading GigaAM %s on %s...", args.model, device)
    t0 = time.monotonic()
    model = gigaam.load_model(args.model, device=device)
    timing["model_load_sec"] = round(time.monotonic() - t0, 2)
    log.info("  loaded in %.1fs", timing["model_load_sec"])

    # ── Convert to WAV if needed ──────────────────────────────────────────
    wav_path = _mp4_to_wav_if_needed(args.wav)
    if wav_path != args.wav:
        log.info("Converted to WAV: %s", wav_path)

    # ── Длительность аудио ────────────────────────────────────────────────
    import wave
    with wave.open(str(wav_path), "rb") as wf:
        total_dur = wf.getnframes() / wf.getframerate()

    # ── Транскрипция ──────────────────────────────────────────────────────
    # GigaAM-овский transcribe_longform требует pyannote/voice-activity-detection,
    # а это gated-репо требующее отдельной апробации. Идём через silero-vad
    # (входит в faster-whisper), бьём аудио на чанки сами, кормим gigaam.transcribe().
    from faster_whisper.vad import get_speech_timestamps, VadOptions
    import wave as _wave
    with _wave.open(str(wav_path), "rb") as wf:
        raw = wf.readframes(wf.getnframes())
        _ch = wf.getnchannels()
    import numpy as np
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if _ch == 2:
        samples = samples.reshape(-1, 2).mean(axis=1)

    log.info("Running silero VAD...")
    t_vad = time.monotonic()
    # min_silence 500ms (vs default 2000ms) → разрезает на более мелкие чанки;
    # max_speech 20s → не даём собирать слишком длинные регионы (gigaam.transcribe()
    # рассчитан на короткие клипы).
    vad_segments = get_speech_timestamps(
        samples,
        VadOptions(min_silence_duration_ms=500, max_speech_duration_s=20.0),
    )
    timing["vad_sec"] = round(time.monotonic() - t_vad, 2)
    log.info("  VAD found %d speech regions in %.1fs",
             len(vad_segments), timing["vad_sec"])

    log.info("Transcribing each segment with GigaAM...")
    t0 = time.monotonic()
    segments = []
    import tempfile
    for vs in vad_segments:
        s_sec = vs["start"] / 16000
        e_sec = vs["end"] / 16000
        if e_sec - s_sec < 0.3:
            continue
        chunk = samples[vs["start"]:vs["end"]]
        # gigaam.transcribe требует путь к файлу
        tmp_wav = Path(tempfile.mktemp(suffix=".wav"))
        with _wave.open(str(tmp_wav), "wb") as wf:
            wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
            wf.writeframes((chunk * 32768).astype(np.int16).tobytes())
        try:
            text = model.transcribe(str(tmp_wav)).strip()
        finally:
            try: tmp_wav.unlink()
            except OSError: pass
        if text:
            segments.append({"boundaries": (s_sec, e_sec), "transcription": text})

    timing["transcribe_sec"] = round(time.monotonic() - t0, 2)
    timing["final_segments"] = len(segments)
    timing["vram_peak_gb"] = round(_vram_used_gb(), 2)
    log.info("  done in %.1fs (%d segments)", timing["transcribe_sec"], len(segments))

    # ── Markdown ──────────────────────────────────────────────────────────
    lines = [
        f"# {args.wav.stem}",
        "",
        f"Tool: **GigaAM** ({args.model})",
        f"Длительность: {_fmt_ts(total_dur)}  |  Сегментов: {len(segments)}",
        "",
        "---",
        "",
    ]
    for seg in segments:
        # seg может быть dict {"transcription": str, "boundaries": (start, end)}
        start, end = seg.get("boundaries", (0.0, 0.0))
        text = seg.get("transcription", "").strip()
        lines.append(f"**[{_fmt_ts(start)}]** {text}")
        lines.append("")

    md_path = args.out_dir / f"gigaam_{args.model}_transcription.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("Markdown: %s", md_path)

    json_path = args.out_dir / f"gigaam_{args.model}_timing.json"
    json_path.write_text(json.dumps(timing, indent=2, ensure_ascii=False), encoding="utf-8")
    log.info("Timing: %s", json_path)
    log.info("Summary: %s", timing)

    # cleanup временного WAV
    if wav_path != args.wav:
        try: wav_path.unlink()
        except OSError: pass


if __name__ == "__main__":
    main()
