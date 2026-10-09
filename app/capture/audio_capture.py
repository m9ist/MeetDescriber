"""
Захват системного аудио + микрофона.

Windows: PyAudioWPatch WASAPI loopback + микрофон (микс)
Mac:     Core Audio Tap (через audiotee) + микрофон (микс)
         Раньше использовался BlackHole — больше не нужен (macOS 14.4+).

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

# Порог дрейфа mic↔loopback (сек), после которого пользователю показываем алерт.
# В норме diff ~ 0.2-0.3 сек; при mic-stuck — 50-120 сек.
DRIFT_WARN_THRESHOLD_SEC = 5.0
DRIFT_WARN_COOLDOWN_SEC = 60.0   # throttling, чтобы не спамить

# Сколько ждать выхода lb-reader'а из loopback_stream.read() перед закрытием стрима.
# PortAudio блокирует read() пока в системе тишина, поэтому ждать приходится не
# «мгновение», а до нескольких секунд.
LB_READER_JOIN_TIMEOUT_SEC = 5.0


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
        # on_drift_warning(drift_seconds): срабатывает когда mic отстаёт от
        # loopback больше чем DRIFT_WARN_THRESHOLD_SEC. Симптом hyper-блокирующего
        # mic_stream.read() — другое приложение держит mic exclusive, либо
        # USB-драйвер сваливается в throttling.
        self.on_drift_warning: Optional[Callable[[float], None]] = None
        self._last_drift_warn_t: float = 0.0  # для throttling уведомлений

        self._recording = False
        self._audio_active = False
        self._chunk_index = 0
        self._thread: Optional[threading.Thread] = None
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="quality")

        self._rate: int = 0
        self._channels: int = 0
        self._sample_width: int = 2
        self._loopback_stream = None
        self._mic_stream = None

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
        # Только микрофон: его read() держит основной цикл захвата, и его надо
        # разблокировать. Loopback-стрим отсюда НЕ трогаем — его читает поток
        # lb-reader, и stop_stream() под работающим read() даёт access violation
        # (дамп 11.06 в app.log). Loopback закрывает сам поток захвата, дождавшись
        # выхода reader'а из read().
        if self._mic_stream is not None:
            try:
                self._mic_stream.stop_stream()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None
        # Пул оценки качества создаётся на каждую сессию и живёт не-демон
        # потоками до конца процесса, если его не закрыть.
        self._executor.shutdown(wait=False)

    @property
    def is_recording(self) -> bool:
        return self._recording

    def _capture_loop(self, device_index: Optional[int]) -> None:
        try:
            if config.IS_WINDOWS:
                self._capture_wasapi(device_index)
            elif config.IS_MAC:
                self._capture_mac(device_index)
            else:
                raise RuntimeError("Unsupported platform")
        except Exception as exc:
            if self.on_error:
                self.on_error(exc)

    def _capture_wasapi(self, device_index: Optional[int]) -> None:
        import audioop
        import logging
        import time
        import pyaudiowpatch as pyaudio

        log = logging.getLogger(__name__)
        log.setLevel(logging.DEBUG)
        pa = pyaudio.PyAudio()
        lb_thread: Optional[threading.Thread] = None
        loopback_stream = None
        mic_stream = None
        try:
            device = self._find_wasapi_loopback(pa, device_index)
            self._rate = int(device["defaultSampleRate"])
            self._channels = min(int(device["maxInputChannels"]), 2)
            frames_per_chunk = int(self._rate * config.CHUNK_DURATION_SEC)
            frames_per_read = 512
            # Таймаут ожидания loopback-данных: 2 периода чтения.
            # При тишине (нет системного звука) loopback блокируется;
            # таймаут позволяет основному циклу всё равно записывать mic.
            lb_timeout = (frames_per_read / self._rate) * 2  # ~21ms при 48kHz (для loopback-only режима)

            loopback_stream = pa.open(
                format=pyaudio.paInt16,
                channels=self._channels,
                rate=self._rate,
                input=True,
                input_device_index=int(device["index"]),
                frames_per_buffer=frames_per_read,
            )
            self._loopback_stream = loopback_stream

            # Loopback и mic пишутся в отдельные сырые буферы независимо.
            # Микс происходит только при сохранении чанка — никакой синхронизации фреймов.
            lb_raw = bytearray()
            lb_lock = threading.Lock()

            def _lb_reader():
                import time
                frame_period = frames_per_read / self._rate  # ожидаемое время одного read (~10.67ms)
                while self._recording:
                    try:
                        t0 = time.monotonic()
                        data = loopback_stream.read(frames_per_read, exception_on_overflow=False)
                        elapsed = time.monotonic() - t0
                        with lb_lock:
                            # Если read заблокировался дольше ожидаемого — системный звук
                            # не поступал. Вставляем тишину чтобы сохранить выравнивание
                            # lb_raw по стенному времени с mc_raw.
                            silence_sec = elapsed - frame_period
                            if silence_sec > frame_period:
                                silence_frames = int(silence_sec * self._rate)
                                lb_raw.extend(bytes(silence_frames * self._channels * 2))
                            lb_raw.extend(data)
                    except OSError:
                        break

            lb_thread = threading.Thread(target=_lb_reader, daemon=True, name="lb-reader")
            lb_thread.start()

            # Microphone
            mic_stream = None
            mic_channels = self._channels
            mic_rate = self._rate
            mic_resample_state = None

            mic_dev = self._find_default_mic(pa)
            if mic_dev is not None:
                mic_channels = min(int(mic_dev["maxInputChannels"]), 2)
                mic_rate_native = int(mic_dev.get("defaultSampleRate", self._rate))
                for try_rate in (self._rate, mic_rate_native):
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
                        self._mic_stream = mic_stream
                        log.info("mic stream opened: %s (%dch @ %dHz)", mic_dev["name"], mic_channels, mic_rate)
                        break
                    except Exception as e:
                        log.warning("mic open failed at %dHz: %s", try_rate, e)
                        mic_stream = None

            mc_raw = bytearray()   # сырые байты mic за текущий чанк
            frames_buffered = 0    # по числу mic-фреймов (определяет длительность)

            def _flush_chunk():
                """Смешивает накопленные lb_raw + mc_raw и сохраняет чанк."""
                with lb_lock:
                    lb_bytes = bytes(lb_raw)
                    del lb_raw[:]
                mc_bytes = bytes(mc_raw)
                mc_raw.clear()
                if mic_stream is not None:
                    lb_frames_raw = len(lb_bytes) // (2 * self._channels)
                    mc_frames_raw = len(mc_bytes) // (2 * mic_channels)
                    diff = lb_frames_raw - mc_frames_raw
                    diff_sec = diff / self._rate
                    log.debug(
                        "flush: lb=%d frames, mc=%d frames, diff=%+d frames (%.3fs)",
                        lb_frames_raw, mc_frames_raw, diff, diff_sec,
                    )
                    # Алерт: mic отстаёт от loopback >5 сек → значит mic блокировался
                    # дольше нормы (другая программа держит mic exclusive / USB lag).
                    # Throttling: не чаще раза в 60 сек на сессию.
                    if abs(diff_sec) > DRIFT_WARN_THRESHOLD_SEC and self.on_drift_warning:
                        _now = time.monotonic()
                        if _now - self._last_drift_warn_t > DRIFT_WARN_COOLDOWN_SEC:
                            self._last_drift_warn_t = _now
                            log.warning(
                                "mic drift detected: %+.1fs (lb=%d, mc=%d frames) — "
                                "вероятно другое приложение держит mic exclusive",
                                diff_sec, lb_frames_raw, mc_frames_raw,
                            )
                            try:
                                self.on_drift_warning(diff_sec)
                            except Exception:
                                log.exception("on_drift_warning callback failed")
                    # Обрезаем lb до длины mc, чтобы любой дрейф не добавлял тишину в конец
                    mc_frames_count = len(mc_bytes) // (2 * mic_channels)
                    lb_bytes = lb_bytes[: mc_frames_count * self._channels * 2]
                    mixed = _mix_audio(lb_bytes, mc_bytes, self._channels, mic_channels)
                else:
                    mixed = lb_bytes
                if mixed:
                    self._process_chunk(mixed)

            if mic_stream is not None:
                # Сбрасываем lb_raw: lb_reader стартует раньше mic-loop и успевает
                # накопить несколько фреймов. Без сброса lb_raw будет длиннее mc_raw
                # → сдвиг при миксе.
                with lb_lock:
                    pre_clear = len(lb_raw)
                    del lb_raw[:]
                log.debug("lb_raw cleared before mic loop: discarded %d bytes (%.3fs)",
                          pre_clear, pre_clear / (2 * self._channels * self._rate))

                # Mic-driven: mic определяет темп и длительность чанка.
                # Loopback пишется асинхронно в lb_raw, микс — при сохранении.
                while self._recording:
                    try:
                        mc_data = mic_stream.read(frames_per_read, exception_on_overflow=False)
                    except OSError:
                        break
                    if mic_rate != self._rate:
                        mc_data, mic_resample_state = audioop.ratecv(
                            mc_data, 2, mic_channels, mic_rate, self._rate, mic_resample_state
                        )
                    mc_raw.extend(mc_data)
                    frames_buffered += frames_per_read

                    if self.on_audio_frame:
                        lb_peek_size = frames_per_read * self._channels * 2
                        with lb_lock:
                            lb_peek = bytes(lb_raw[-lb_peek_size:]) if len(lb_raw) >= lb_peek_size else None
                        if lb_peek:
                            self.on_audio_frame(_mix_audio(lb_peek, mc_data, self._channels, mic_channels))
                        else:
                            self.on_audio_frame(mc_data)

                    if frames_buffered >= frames_per_chunk:
                        _flush_chunk()
                        frames_buffered = 0
            else:
                # Нет mic: loopback-driven с ожиданием данных.
                while self._recording:
                    with lb_lock:
                        available = len(lb_raw)
                    if available < frames_per_read * self._channels * 2:
                        time.sleep(lb_timeout)
                        continue
                    with lb_lock:
                        chunk_bytes = bytes(lb_raw[:frames_per_read * self._channels * 2])
                        del lb_raw[:frames_per_read * self._channels * 2]
                    if self.on_audio_frame:
                        self.on_audio_frame(chunk_bytes)
                    mc_raw.extend(chunk_bytes)
                    frames_buffered += frames_per_read

                    if frames_buffered >= frames_per_chunk:
                        _flush_chunk()
                        frames_buffered = 0

            if frames_buffered > 0:
                _flush_chunk()

        finally:
            # Разрушение — единственным путём, в том числе при выходе по исключению
            # (раньше исключение в цикле оставляло стримы открытыми).
            # Флаг снимаем первым: иначе lb-reader крутится вечно, а is_recording
            # остаётся True и следующая запись не стартует.
            self._recording = False
            self._loopback_stream = None
            self._mic_stream = None

            # lb-reader может стоять внутри loopback_stream.read(): PortAudio
            # блокирует чтение, пока в системе тишина. Закрыть стрим или
            # терминировать PortAudio под работающим read() — это access violation,
            # которая убивает процесс без шанса на traceback (дамп 11.06 в app.log).
            # Поэтому сначала ждём выхода reader'а, а если он застрял — намеренно
            # оставляем loopback открытым: утечка стрима на одну сессию дешевле
            # убитого приложения.
            if lb_thread is not None:
                lb_thread.join(timeout=LB_READER_JOIN_TIMEOUT_SEC)
            lb_stuck = lb_thread is not None and lb_thread.is_alive()
            if lb_stuck:
                log.error("lb-reader не вышел из read() за %.0f с — не закрываем "
                          "loopback-стрим и не терминируем PortAudio",
                          LB_READER_JOIN_TIMEOUT_SEC)

            for s in (mic_stream, None if lb_stuck else loopback_stream):
                if s is not None:
                    try:
                        s.stop_stream()
                        s.close()
                    except Exception:
                        pass
            if not lb_stuck:
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

    def _capture_mac(self, device_index: Optional[int]) -> None:
        """Захват на macOS: Core Audio Tap (audiotee) для системного звука +
        sounddevice для микрофона. Микс при сохранении чанка.

        Раньше использовался BlackHole — требовал brew install + ручную настройку
        Multi-Output Device. Tap делает то же самое нативно через Apple API.
        """
        import logging
        import sounddevice as sd
        from app.capture.mac_tap import MacTapCapture, is_available

        log = logging.getLogger(__name__)

        if not is_available():
            raise RuntimeError(
                "Core Audio Tap недоступен. Требования: macOS 14.4+ и собранный "
                "audiotee бинарь (bash create_mac_app.sh)."
            )

        devices = sd.query_devices()

        self._rate = 48000
        self._channels = 2
        frames_per_chunk = int(self._rate * config.CHUNK_DURATION_SEC)
        tap_sample_size = 2 * self._channels  # bytes на фрейм

        # Микрофон: дефолтный системный input
        mic_idx, mic_channels = self._find_mac_mic(devices)
        if mic_idx is not None:
            log.info("mic: [%d] %s (%dch)", mic_idx, devices[mic_idx]["name"], mic_channels)
        else:
            log.warning("mic не найден — записываем только системный звук")
        log.info("system audio: Core Audio Tap @ %dHz, %dch", self._rate, self._channels)

        mc_sample_size = 2 * mic_channels
        tap_buf = bytearray()
        mc_buf = bytearray()
        buf_lock = threading.Lock()

        def tap_on_frame(raw: bytes) -> None:
            with buf_lock:
                tap_buf.extend(raw)
            # Без микрофона спектр кормится только tap'ом
            if mic_idx is None and self.on_audio_frame:
                self.on_audio_frame(raw)

        def tap_on_error(exc: Exception) -> None:
            log.error("audiotee error: %s", exc)
            if self.on_error:
                self.on_error(exc)

        def mc_callback(indata, frames, time_info, status):
            if not self._recording:
                raise sd.CallbackStop()
            raw = indata.tobytes()
            tap_peek_size = frames * tap_sample_size
            with buf_lock:
                mc_buf.extend(raw)
                tap_peek = bytes(tap_buf[-tap_peek_size:]) if len(tap_buf) >= tap_peek_size else None
            if self.on_audio_frame:
                # Спектр показывает микс tap + mic в формате tap (stereo); пока tap пуст — только mic
                self.on_audio_frame(_mix_audio(tap_peek or b"", raw, self._channels, mic_channels))

        def flush_chunk():
            """Снимает накопленное и микширует mic + tap."""
            with buf_lock:
                if mic_idx is not None:
                    mc_data = bytes(mc_buf)
                    mc_buf.clear()
                    mc_frames = len(mc_data) // mc_sample_size
                    # Tap обрезается до длительности микрофона — иначе дрейф
                    tap_take = mc_frames * tap_sample_size
                    tap_data = bytes(tap_buf[:tap_take])
                    del tap_buf[:tap_take]
                else:
                    tap_data = bytes(tap_buf)
                    tap_buf.clear()
                    mc_data = b""
            if mc_data:
                mixed = _mix_audio(tap_data, mc_data, self._channels, mic_channels)
            else:
                mixed = tap_data
            if mixed:
                tap_rms = _calc_rms(tap_data) if tap_data else 0
                mc_rms = _calc_rms(mc_data) if mc_data else 0
                log.info("flush: tap=%d frames (RMS=%.1f), mc=%d frames (RMS=%.1f)",
                         len(tap_data) // tap_sample_size, tap_rms,
                         len(mc_data) // mc_sample_size if mc_data else 0, mc_rms)
                # Warning если tap молчит — обычно permission не выдано
                if tap_rms < 10 and mc_rms > 100:
                    log.warning(
                        "Системный звук молчит (RMS=%.1f) при наличии mic. "
                        "Проверь System Settings → Privacy & Security → "
                        "System Audio Recording — должно быть разрешение MeetDescriber.",
                        tap_rms,
                    )
                self._process_chunk(mixed)

        # Core Audio Tap для системного звука
        tap = MacTapCapture(
            sample_rate=self._rate,
            stereo=(self._channels == 2),
            chunk_duration_sec=0.2,
        )
        tap.on_frame = tap_on_frame
        tap.on_error = tap_on_error

        mic_stream = None
        if mic_idx is not None:
            try:
                mic_stream = sd.InputStream(
                    device=mic_idx, channels=mic_channels, samplerate=self._rate,
                    dtype="int16", blocksize=1024, callback=mc_callback,
                )
            except Exception as e:
                log.warning("mic stream failed at %dHz: %s — only system audio", self._rate, e)
                mic_stream = None
                mic_idx = None

        try:
            tap.start(include_pids=None)  # Тапаем все процессы (можно указать PID Chrome)
            if mic_stream:
                mic_stream.start()

            # Тайминг ведёт mic (если есть), иначе tap
            while self._recording:
                threading.Event().wait(0.1)
                with buf_lock:
                    if mic_idx is not None:
                        frames_have = len(mc_buf) // mc_sample_size
                    else:
                        frames_have = len(tap_buf) // tap_sample_size
                if frames_have >= frames_per_chunk:
                    flush_chunk()

            # Финальный сброс при остановке (может быть < chunk_duration)
            flush_chunk()
        finally:
            tap.stop()
            if mic_stream is not None:
                try:
                    mic_stream.stop()
                    mic_stream.close()
                except Exception:
                    pass

    def _find_mac_mic(self, devices) -> tuple[Optional[int], int]:
        """Возвращает (index, channels) дефолтного микрофона на Mac.

        Исключает BlackHole (legacy) и любые tap-устройства (например,
        от audiotee aggregate device).
        """
        import sounddevice as sd

        def _is_real_input(d: dict) -> bool:
            name = d.get("name", "")
            if d.get("max_input_channels", 0) <= 0:
                return False
            if "BlackHole" in name:
                return False
            if "Tap" in name or "audiotee" in name.lower():
                return False
            return True

        # Default input — sd.default.device может быть int, list, или _InputOutputPair
        try:
            default_in = int(sd.default.device[0])
        except (TypeError, IndexError):
            try:
                default_in = int(sd.default.device)
            except (TypeError, ValueError):
                default_in = -1
        if default_in >= 0 and _is_real_input(devices[default_in]):
            return default_in, min(int(devices[default_in]["max_input_channels"]), 2)
        # Любой подходящий input
        for i, d in enumerate(devices):
            if _is_real_input(d):
                return i, min(int(d["max_input_channels"]), 2)
        return None, 1

    def _process_chunk(self, raw: bytes) -> None:
        import logging
        log = logging.getLogger(__name__)

        rms = _calc_rms(raw)
        duration_sec = len(raw) / (2 * self._channels * self._rate) if self._rate else 0
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
            log.info("chunk discarded: silent (RMS=%.1f < %d, %.2fs)",
                     rms, config.SILENCE_THRESHOLD_RMS, duration_sec)
            return

        idx = self._chunk_index
        self._chunk_index += 1
        path = self.session_dir / f"chunk_{idx:04d}.wav"
        _save_wav(path, raw, self._rate, self._channels, self._sample_width)
        log.info("chunk saved: %s (RMS=%.1f, %.2fs)", path.name, rms, duration_sec)

        if self.on_chunk_saved:
            self.on_chunk_saved(path, idx)

        self._executor.submit(_evaluate_quality, path, idx, self.on_quality_low)


def _mix_audio(lb: bytes, mc: bytes, lb_ch: int, mc_ch: int) -> bytes:
    """Mix loopback + mic (int16 PCM). Handles mono/stereo and length mismatch."""
    lb_frames = len(lb) // (2 * lb_ch)
    mc_frames = len(mc) // (2 * mc_ch)
    n_frames = max(lb_frames, mc_frames)
    if n_frames == 0:
        return b""
    # Дополняем тишиной более короткий поток
    lb_pad = lb + bytes((n_frames - lb_frames) * lb_ch * 2)
    mc_pad = mc + bytes((n_frames - mc_frames) * mc_ch * 2)
    lb_samples = struct.unpack(f"<{n_frames * lb_ch}h", lb_pad)
    mc_samples = struct.unpack(f"<{n_frames * mc_ch}h", mc_pad)
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
        # MLX/Metal не потокобезопасен: два transcribe параллельно из пула quality
        # (два чанка подряд при остановке записи) → SIGSEGV в decoding._main_loop.
        self._lock = threading.Lock()

    def transcribe(self, path: str, language: str = "ru", word_timestamps: bool = False):
        with self._lock:
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
        # На Mac системный звук захватывается через Core Audio Tap (mac_tap.py),
        # отдельные input-устройства больше не нужны. Список здесь — это только
        # реальные микрофоны для возможности явного выбора. BlackHole/audiotee
        # tap-устройства мы пропускаем как не-mic.
        try:
            import sounddevice as sd
            for i, dev in enumerate(sd.query_devices()):
                name = dev["name"]
                if dev["max_input_channels"] > 0 and "BlackHole" not in name and "Tap" not in name:
                    sources.append({
                        "index": i,
                        "name": name,
                        "is_loopback": False,
                    })
        except Exception:
            pass

    return sources
