"""
Worker-процесс диаризации (subprocess).

Запускается дочерним процессом из pyannote_diarizer.py, чтобы изолировать
pyannote/PyTorch от ctranslate2 (faster-whisper). Оба пакета бандлят свой
cudnn64_9.dll; при совместной загрузке в одном процессе — stack corruption
(0xc0000409, BEX64) без Python-исключения.

Вход:  argv[1] = абсолютный путь к WAV-файлу
Выход: JSON-список [{speaker, start, end}, ...] в stdout
Логи:  в stderr (форвардятся в app.log родителем)
"""
import json
import logging
import struct
import sys
import warnings
import wave
from pathlib import Path

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(message)s",
    stream=sys.stderr,
)
log = logging.getLogger(__name__)


def _load_wav_as_tensor(path: Path):
    import torch

    with wave.open(str(path), "rb") as wf:
        n_channels  = wf.getnchannels()
        sample_width = wf.getsampwidth()
        frame_rate  = wf.getframerate()
        n_frames    = wf.getnframes()
        raw         = wf.readframes(n_frames)

    if sample_width == 2:
        samples = struct.unpack(f"<{len(raw) // 2}h", raw)
        tensor  = torch.tensor(samples, dtype=torch.float32) / 32768.0
    elif sample_width == 4:
        samples = struct.unpack(f"<{len(raw) // 4}i", raw)
        tensor  = torch.tensor(samples, dtype=torch.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported sample_width={sample_width}")

    if n_channels > 1:
        tensor = tensor.reshape(-1, n_channels).mean(dim=1)

    return tensor.unsqueeze(0), frame_rate   # (1, samples), rate


EMBEDDING_BATCH_SIZE = 4
VRAM_FRACTION = 0.4


def main() -> None:
    if len(sys.argv) < 2:
        log.error("Usage: diarize_worker.py <wav_path>")
        sys.exit(1)

    wav_path = Path(sys.argv[1])
    if not wav_path.exists():
        log.error("WAV not found: %s", wav_path)
        sys.exit(1)

    import torch

    if torch.cuda.is_available():
        free, total = torch.cuda.mem_get_info()
        log.info("VRAM free %.1f GB / %.1f GB", free / 1e9, total / 1e9)

    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="torchcodec is not installed")
        warnings.filterwarnings("ignore", category=UserWarning, module="torio")
        from pyannote.audio import Pipeline

    log.info("loading pyannote pipeline...")
    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-community-1")

    device = "cuda" if torch.cuda.is_available() else "cpu"
    pipeline.to(torch.device(device))
    # При батче эмбеддингов 8 и 32 (дефолт) cuDNN 9.10 / torch 2.8 выбирает алгоритм
    # свёртки с рабочей памятью ~8.7 ГБ. Запаса на 12 ГБ RTX 3060 это не оставляет:
    # когда VRAM занята чем-то ещё, драйвер сбрасывает её в системную RAM, GPU висит
    # на 100% при ~60 Вт, и диаризация 20–40 мин аудио тянется полчаса (29.10).
    # Батч 4 даёт пик 0.43 ГБ при той же скорости. Лимит доли VRAM — страховка от
    # смены эвристики cuDNN: план, которому не хватает памяти, cuDNN отбрасывает и
    # берёт следующий (замерено: батч 32 под лимитом — пик 1.6 ГБ, та же скорость).
    pipeline.embedding_batch_size = EMBEDDING_BATCH_SIZE
    if device == "cuda":
        torch.cuda.set_per_process_memory_fraction(VRAM_FRACTION)
    log.info("pyannote loaded on %s", device)

    waveform, rate = _load_wav_as_tensor(wav_path)
    duration = waveform.shape[-1] / rate
    log.info("diarizing %.1f s ...", duration)

    audio  = {"waveform": waveform.cpu(), "sample_rate": rate}
    result = pipeline(audio)

    annotation = getattr(result, "speaker_diarization", result)
    segments = [
        {"speaker": speaker, "start": turn.start, "end": turn.end}
        for turn, _, speaker in annotation.itertracks(yield_label=True)
    ]
    segments.sort(key=lambda s: s["start"])
    log.info("done: %d segments", len(segments))
    if torch.cuda.is_available():
        log.info("VRAM peak allocated %.2f GB, reserved %.2f GB",
                 torch.cuda.max_memory_allocated() / 1e9, torch.cuda.max_memory_reserved() / 1e9)

    print(json.dumps(segments))


if __name__ == "__main__":
    main()
