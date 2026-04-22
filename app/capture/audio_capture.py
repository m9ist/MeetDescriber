"""
Захват системного аудио.

Windows: PyAudioWPatch WASAPI loopback
Mac:     sounddevice + BlackHole

Работает в фоновом потоке. Буферизует аудио чанками по CHUNK_DURATION_SEC секунд.
Для каждого чанка:
  - Считает RMS (silence detection)
  - Сохраняет на диск если не тишина
  - Асинхронно оценивает качество транскрипции
  - Вызывает коллбэки о событиях
"""
from __future__ import annotations

import struct
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import config

OnChunkSaved = Callable[[Path, int], None]
OnQualityLow = Callable[[int, float], None]
OnAudioStarted = Callable[[], None]
OnAudioStopped = Callable[[], None]
OnError = Callable[[Exception], None]


class AudioCapture:
    """
    Захватывает системный звук и нарезает его на чанки.

    Использование:
        cap = AudioCapture(session_dir=Path("data/recordings/my_session"))
        cap.on_chunk_saved = lambda path, idx: print(f"Chunk {idx}: {path}")
        cap.on_quality_low = lambda idx, score: notify_user(idx, score)
        cap.start()
        ...
        cap.stop()
    """

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self.on_chunk_saved: Optional[OnChunkSaved] = None
        self.on_quality_low: Optional[OnQualityLow] = None
        self.on_audio_started: Optional[OnAudioStarted] = None
        self.on_audio_stopped: Optional[OnAudioStopped] = None
        self.on_error: Optional[OnError] = None
        self.on_audio_frame: Optional[Callable[[bytes], None]] = None

        self._recording = False
        self._audio_active = False
        self._chunk_index = 0
        self._thread: Optional[threading.Thread] = None
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="quality")

        self._rate: int = 0
        self._channels: int = 0
        self._sample_width: int = 2

    def start(self, device_index: Optional[int] = None) -> None:
        """Запускает захват в фоновом потоке."""
        if self._recording:
            return
        self._recording = True
        self._audio_active = False
        self._chunk_index = 0
        self._thread = threading.Thread(
            target=self._capture_loop,
            args=(device_index,),
            daemon=True,
            name="audio-capture",
        )
        self._thread.start()

    def stop(self) -> None:
        """Останавливает захват. Блокирует до завершения потока."""
        self._recording = False
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    @property
    def is_recording(self) -> bool:
        return self._recording

    def _capture_loop(self, device_index: Optional[int]) -> None:
        try:
            if config.IS_WINDOWS:
                self._capture_wasapi(device_index)
            elif config.IS_MAC:
                self._capture_blackhole(device_index)
            else:
                raise RuntimeError("Unsupported platform")
        except Exception as exc:
            if self.on_error:
                self.on_error(exc)

    def _capture_wasapi(self, device_index: Optional[int]) -> None:
        import logging
        import pyaudiowpatch as pyaudio

        log = logging.getLogger(__name__)
        pa = pyaudio.PyAudio()
        try:
            device = self._find_wasapi_loopback(pa, device_index)
            self._rate = int(device["defaultSampleRate"])
            self._channels = min(int(device["maxInputChannels"]), 2)
            frames_per_chunk = int(self._rate * config.CHUNK_DURATION_SEC)
            frames_per_read = 512

            loopback_stream = pa.open(
                format=pyaudio.paInt16,
                channels=self._channels,
                rate=self._rate,
                input=True,
                input_device_index=int(device["index"]),
                frames_per_buffer=frames_per_read,
            )

            mic_stream = None
            mic_channels = self._channels
            mic_rate = self._rate
            mic_resample_state = None
            mic_dev = self._find_default_mic(pa)
            if mic_dev is not None:
                mic_channels = min(int(mic_dev["maxInputChannels"]), 2)
                mic_rate = int(mic_dev.get("defaultSampleRate", self._rate))
                for try_rate in (self._rate, mic_rate):
                    try:
                        mic_stream = pa.open(
                            format=pyaudio.paInt16,
                            channels=mic_channels,
                            rate=try_rate,
                            input=True,
                            input_device_index=int(mic_dev["index"]),
                            frames_per_buffer=frames_per_read,
                        )
                        mic_rate = try_rate
                        log.info("mic stream opened: %s (%dch @ %dHz)", mic_dev["name"], mic_channels, mic_rate)
                        break
                    except Exception as e:
                        log.warning("mic open failed at %dHz: %s", try_rate, e)
                        mic_stream = None

            buffer: list[bytes] = []
            frames_buffered = 0

            while self._recording:
                lb_data = loopback_stream.read(frames_per_read, exception_on_overflow=False)
                if mic_stream:
                    try:
                        import audioop
                        mc_data = mic_stream.read(frames_per_read, exception_on_overflow=False)
                        if mic_rate != self._rate:
                            mc_data, mic_resample_state = audioop.ratecv(
                                mc_data, 2, mic_channels, mic_rate, self._rate, mic_resample_state
                            )
                        data = _mix_audio(lb_data, mc_data, self._channels, mic_channels)
                    except Exception:
                        data = lb_data
                else:
                    data = lb_data

                if self.on_audio_frame:
                    self.on_audio_frame(data)
                buffer.append(data)
                frames_buffered += frames_per_read

                if frames_buffered >= frames_per_chunk:
                    self._process_chunk(b"".join(buffer))
                    buffer = []
                    frames_buffered = 0

            if buffer:
                self._process_chunk(b"".join(buffer))

            loopback_stream.stop_stream()
            loopback_stream.close()
            if mic_stream:
                mic_stream.stop_stream()
                mic_stream.close()
        finally:
            pa.terminate()

    def _find_default_mic(self, pa) -> Optional[dict]:
        import pyaudiowpatch as pyaudio
        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            idx = wasapi_info.get("defaultInputDevice", -1)
            if idx >= 0:
                dev = pa.get_device_info_by_index(idx)
                if not dev.get("isLoopbackDevice") and dev.get("maxInputChannels", 0) > 0:
                    return dev
        except Exception:
            pass
        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if not dev.get("isLoopbackDevice") and dev.get("maxInputChannels", 0) > 0:
                return dev
        return None

    def _find_wasapi_loopback(self, pa, preferred_index: Optional[int]) -> dict:
        import pyaudiowpatch as pyaudio

        if preferred_index is not None:
            info = pa.get_device_info_by_index(preferred_index)
            if info.get("isLoopbackDevice"):
                return info
            # Не loopback — игнорируем и ищем автоматически

        try:
            wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_out = pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
            for i in range(pa.get_device_count()):
                dev = pa.get_device_info_by_index(i)
                if dev.get("isLoopbackDevice") and default_out["name"] in dev["name"]:
                    return dev
        except Exception:
            pass

        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if dev.get("isLoopbackDevice") and dev.get("maxInputChannels", 0) > 0:
                return dev

        raise RuntimeError("WASAPI loopback устройство не найдено")

    def _capture_blackhole(self, device_index: Optional[int]) -> None:
        import sounddevice as sd

        devices = sd.query_devices()
        if device_index is None:
            bh = next((i for i, d in enumerate(devices) if "BlackHole" in d["name"]), None)
            if bh is None:
                raise RuntimeError("BlackHole не найден. Установи: brew install blackhole-2ch")
            device_index = bh

        self._rate = 48000
        self._channels = 2
        frames_per_chunk = int(self._rate * config.CHUNK_DURATION_SEC)
        buffer: list[bytes] = []
        frames_buffered = 0

        def callback(indata, frames, time_info, status):
            nonlocal frames_buffered
            if not self._recording:
                raise sd.CallbackStop()
            raw = indata.copy().tobytes()
            if self.on_audio_frame:
                self.on_audio_frame(raw)
            buffer.append(raw)
            frames_buffered += frames
            if frames_buffered >= frames_per_chunk:
                self._process_chunk(b"".join(buffer))
                buffer.clear()
                frames_buffered = 0

        with sd.InputStream(
            device=device_index,
            channels=self._channels,
            samplerate=self._rate,
            dtype="int16",
            blocksize=1024,
            callback=callback,
        ):
            while self._recording:
                threading.Event().wait(0.1)

        if buffer:
            self._process_chunk(b"".join(buffer))

    def _process_chunk(self, raw: bytes) -> None:
        rms = _calc_rms(raw)
        is_silent = rms < config.SILENCE_THRESHOLD_RMS

        if not is_silent and not self._audio_active:
            self._audio_active = True
            if self.on_audio_started:
                self.on_audio_started()
        elif is_silent and self._audio_active:
            self._audio_active = False
            if self.on_audio_stopped:
                self.on_audio_stopped()

        if is_silent:
            return

        idx = self._chunk_index
        self._chunk_index += 1
        path = self.session_dir / f"chunk_{idx:04d}.wav"
        _save_wav(path, raw, self._rate, self._channels, self._sample_width)

        if self.on_chunk_saved:
            self.on_chunk_saved(path, idx)

        self._executor.submit(_evaluate_quality, path, idx, self.on_quality_low)


def _mix_audio(lb: bytes, mc: bytes, lb_ch: int, mc_ch: int) -> bytes:
    """Mix loopback + mic (int16 PCM). Handles mono/stereo mismatch."""
    n_frames = min(len(lb) // (2 * lb_ch), len(mc) // (2 * mc_ch))
    lb_samples = struct.unpack(f"<{n_frames * lb_ch}h", lb[: n_frames * lb_ch * 2])
    mc_samples = struct.unpack(f"<{n_frames * mc_ch}h", mc[: n_frames * mc_ch * 2])
    result = []
    for i in range(n_frames):
        for c in range(lb_ch):
            lb_val = lb_samples[i * lb_ch + c]
            mc_val = mc_samples[i * mc_ch + min(c, mc_ch - 1)]
            result.append(max(-32768, min(32767, lb_val + mc_val)))
    return struct.pack(f"<{len(result)}h", *result)


def _calc_rms(raw: bytes) -> float:
    count = len(raw) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", raw)
    return (sum(v * v for v in samples) / count) ** 0.5


def _save_wav(path: Path, raw: bytes, rate: int, channels: int, sampwidth: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(raw)


def _evaluate_quality(path: Path, idx: int, callback: Optional[OnQualityLow]) -> None:
    """Оценивает качество чанка через whisper-tiny. Не бросает исключений."""
    try:
        model = _get_quality_model()
        segments, _ = model.transcribe(
            str(path),
            language=config.WHISPER_LANGUAGE,
            word_timestamps=True,
        )
        scores: list[float] = []
        for seg in segments:
            if seg.words:
                scores.extend(w.probability for w in seg.words)

        score = sum(scores) / len(scores) if scores else 1.0

        if score < config.QUALITY_THRESHOLD and callback:
            callback(idx, score)
    except Exception:
        pass


_quality_model = None
_quality_model_lock = threading.Lock()


def _get_quality_model():
    global _quality_model
    with _quality_model_lock:
        if _quality_model is None:
            if config.IS_WINDOWS:
                from faster_whisper import WhisperModel
                # Принудительно CPU: tiny-модель быстрая, а GPU-контекст делить с основной
                # моделью из другого потока небезопасно — вызывает hard crash в CUDA runtime.
                _quality_model = WhisperModel("tiny", device="cpu", compute_type="int8")
            elif config.IS_MAC:
                import mlx_whisper as _mlx
                # Обёртка: приводим mlx_whisper к интерфейсу faster_whisper (transcribe → segments)
                _quality_model = _MlxQualityModel(_mlx)
    return _quality_model


class _MlxQualityModel:
    """Тонкая обёртка над mlx_whisper для оценки качества чанка на Mac."""

    def __init__(self, mlx_whisper_module) -> None:
        self._mlx = mlx_whisper_module

    def transcribe(self, path: str, language: str = "ru", word_timestamps: bool = False):
        result = self._mlx.transcribe(
            path,
            path_or_hf_repo="mlx-community/whisper-tiny-mlx",
            language=language,
            word_timestamps=word_timestamps,
        )
        segments = [_MlxSegment(s) for s in result.get("segments", [])]
        return segments, result


class _MlxSegment:
    """Приводит сегмент mlx_whisper к интерфейсу faster_whisper."""

    def __init__(self, seg: dict) -> None:
        self.words = [
            _MlxWord(w) for w in seg.get("words", [])
        ] if seg.get("words") else []


class _MlxWord:
    def __init__(self, w: dict) -> None:
        self.probability = w.get("probability", 1.0)


def list_audio_sources() -> list[dict]:
    """
    Возвращает список доступных аудиоисточников.
    Каждый элемент: {"index": int, "name": str, "is_loopback": bool}
    """
    sources = []

    if config.IS_WINDOWS:
        try:
            import pyaudiowpatch as pyaudio
            pa = pyaudio.PyAudio()
            for i in range(pa.get_device_count()):
                dev = pa.get_device_info_by_index(i)
                if dev.get("maxInputChannels", 0) > 0:
                    sources.append({
                        "index": i,
                        "name": dev["name"],
                        "is_loopback": bool(dev.get("isLoopbackDevice")),
                    })
            pa.terminate()
        except Exception:
            pass

    elif config.IS_MAC:
        try:
            import sounddevice as sd
            for i, dev in enumerate(sd.query_devices()):
                if dev["max_input_channels"] > 0:
                    sources.append({
                        "index": i,
                        "name": dev["name"],
                        "is_loopback": "BlackHole" in dev["name"],
                    })
        except Exception:
            pass

    return sources
