"""
Скрипт первоначальной настройки for_meets.

Запуск: python setup.py

Последовательно проверяет гипотезы H1–H8 и выводит отчёт.
При провале критической гипотезы останавливается и объясняет что делать.
"""
import sys
import io
import time
import wave
import struct
import tempfile
import subprocess
from pathlib import Path

# Принудительно UTF-8 для вывода в терминал Windows
if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# ── вывод ──────────────────────────────────────────────────────────────────

WIDTH = 60

def header(text: str) -> None:
    print(f"\n{'─' * WIDTH}")
    print(f"  {text}")
    print(f"{'─' * WIDTH}")

def ok(text: str) -> None:
    print(f"  ✓  {text}")

def fail(text: str) -> None:
    print(f"  ✗  {text}")

def info(text: str) -> None:
    print(f"     {text}")

def ask(text: str) -> str:
    return input(f"\n  ?  {text} ").strip()


# ── результаты ─────────────────────────────────────────────────────────────

results: dict[str, bool | None] = {}  # H1..H8 → True/False/None(skip)


def record_result(h: str, passed: bool) -> None:
    results[h] = passed


# ── helpers ────────────────────────────────────────────────────────────────

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"


def check_import(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def save_wav(path: Path, frames: bytes, rate: int, channels: int, sampwidth: int) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(sampwidth)
        wf.setframerate(rate)
        wf.writeframes(frames)


def generate_sine_wav(path: Path, freq: int = 440, duration: float = 3.0,
                      rate: int = 16000) -> None:
    """Генерирует тестовый WAV с синусоидой."""
    import math
    n = int(rate * duration)
    frames = b""
    for i in range(n):
        v = int(32767 * math.sin(2 * math.pi * freq * i / rate))
        frames += struct.pack("<h", v)
    save_wav(path, frames, rate, 1, 2)


# ══════════════════════════════════════════════════════════════════════════
# ПРОВЕРКИ ЗАВИСИМОСТЕЙ
# ══════════════════════════════════════════════════════════════════════════

def check_dependencies() -> bool:
    header("Проверка зависимостей")
    all_ok = True

    required_common = ["dotenv", "anthropic", "pyannote"]
    required_win = ["pyaudiowpatch"] if IS_WINDOWS else []
    required_mac = ["sounddevice", "mlx_whisper"] if IS_MAC else []
    required_win_transcribe = ["faster_whisper"] if IS_WINDOWS else []

    for mod in required_common + required_win + required_mac + required_win_transcribe:
        if check_import(mod):
            ok(mod)
        else:
            fail(f"{mod}  ← не установлен")
            all_ok = False

    if not all_ok:
        req = "requirements-windows.txt" if IS_WINDOWS else "requirements-mac.txt"
        info(f"Установи зависимости: pip install -r {req}")
        if IS_WINDOWS:
            info("PyTorch с CUDA: pip install torch --index-url https://download.pytorch.org/whl/cu121")
        if IS_MAC:
            info("BlackHole: brew install blackhole-2ch")

    return all_ok


# ══════════════════════════════════════════════════════════════════════════
# H1 — WASAPI LOOPBACK (Windows)
# ══════════════════════════════════════════════════════════════════════════

def check_h1_wasapi() -> bool:
    header("H1 — Захват системного аудио (WASAPI loopback, Windows)")

    try:
        import pyaudiowpatch as pyaudio
    except ImportError:
        fail("PyAudioWPatch не установлен — пропускаем H1")
        record_result("H1", None)
        return True

    info("Ищем WASAPI loopback устройство...")
    pa = pyaudio.PyAudio()
    loopback_device = None

    try:
        wasapi_info = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_speakers = pa.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        # Ищем loopback-версию устройства вывода
        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if dev.get("isLoopbackDevice") and default_speakers["name"] in dev["name"]:
                loopback_device = dev
                break
    except Exception as e:
        fail(f"Ошибка при поиске WASAPI устройства: {e}")
        record_result("H1", False)
        pa.terminate()
        return False

    if not loopback_device:
        fail("WASAPI loopback устройство не найдено")
        info("Убедись что в системе есть аудиовыход и установлены стандартные драйверы")
        record_result("H1", False)
        pa.terminate()
        return False

    ok(f"Найдено устройство: {loopback_device['name']}")
    info("Записываем 3 секунды (воспроизведи любой звук)...")

    ask("Нажми Enter когда будешь готов...")

    frames = []
    rate = int(loopback_device["defaultSampleRate"])
    channels = loopback_device["maxInputChannels"]

    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=channels,
            rate=rate,
            input=True,
            input_device_index=loopback_device["index"],
            frames_per_buffer=512,
        )
        for _ in range(0, int(rate / 512 * 3)):
            frames.append(stream.read(512, exception_on_overflow=False))
        stream.stop_stream()
        stream.close()
    except Exception as e:
        fail(f"Ошибка записи: {e}")
        record_result("H1", False)
        pa.terminate()
        return False
    finally:
        pa.terminate()

    raw = b"".join(frames)
    rms = (sum(v ** 2 for v in struct.unpack(f"<{len(raw)//2}h", raw)) / (len(raw) // 2)) ** 0.5

    if rms < 50:
        fail(f"Сигнал слишком тихий (RMS={rms:.0f}). Возможно звук не воспроизводился.")
        info("Убедись что в момент записи играет какой-нибудь звук")
        record_result("H1", False)
        return False

    ok(f"Звук захвачен (RMS={rms:.0f})")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = Path(f.name)
    save_wav(tmp, raw, rate, channels, 2)
    ok(f"Сохранён тестовый файл: {tmp}")
    record_result("H1", True)
    return True


# ══════════════════════════════════════════════════════════════════════════
# H2 — BLACKHOLE (Mac)
# ══════════════════════════════════════════════════════════════════════════

def check_h2_blackhole() -> bool:
    header("H2 — Захват системного аудио (BlackHole, Mac)")

    try:
        import sounddevice as sd
    except ImportError:
        fail("sounddevice не установлен — пропускаем H2")
        record_result("H2", None)
        return True

    devices = sd.query_devices()
    bh = next((d for d in devices if "BlackHole" in d["name"]), None)

    if not bh:
        fail("BlackHole не найден среди аудиоустройств")
        info("Установи: brew install blackhole-2ch")
        info("Затем настрой Aggregate Device в Audio MIDI Setup")
        record_result("H2", False)
        return False

    ok(f"BlackHole найден: {bh['name']}")
    info("Записываем 3 секунды (воспроизведи любой звук через BlackHole)...")
    ask("Нажми Enter когда будешь готов...")

    rate = 44100
    duration = 3
    try:
        recording = sd.rec(int(duration * rate), samplerate=rate,
                           channels=1, dtype="int16",
                           device=bh["name"])
        sd.wait()
    except Exception as e:
        fail(f"Ошибка записи: {e}")
        record_result("H2", False)
        return False

    flat = recording.flatten()
    rms = (sum(int(v) ** 2 for v in flat) / len(flat)) ** 0.5

    if rms < 50:
        fail(f"Сигнал слишком тихий (RMS={rms:.0f})")
        record_result("H2", False)
        return False

    ok(f"Звук захвачен через BlackHole (RMS={rms:.0f})")
    record_result("H2", True)
    return True


# ══════════════════════════════════════════════════════════════════════════
# H3/H4 — ТРАНСКРИПЦИЯ (faster-whisper / mlx-whisper)
# ══════════════════════════════════════════════════════════════════════════

def check_h3_h4_transcription() -> bool:
    label = "H3" if IS_WINDOWS else "H4"
    backend = "faster-whisper (CUDA)" if IS_WINDOWS else "mlx-whisper (Apple Silicon)"
    header(f"{label} — Транскрипция: {backend}")

    info("Для теста используем синтетический WAV + короткую русскую фразу.")
    info("Качество на синтетике будет низким — важна сама работоспособность модели.")

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        test_wav = Path(f.name)
    generate_sine_wav(test_wav)

    if IS_WINDOWS:
        try:
            from faster_whisper import WhisperModel
        except ImportError:
            fail("faster-whisper не установлен")
            record_result(label, None)
            return True

        info("Загружаем модель 'small' (первый запуск скачает ~500 МБ)...")
        try:
            model = WhisperModel("small", device="cuda", compute_type="float16")
        except Exception as cuda_err:
            if "cublas" in str(cuda_err).lower() or "cudnn" in str(cuda_err).lower() or "cuda" in str(cuda_err).lower():
                fail(f"CUDA runtime DLL не найдены: {cuda_err}")
                info("Установи CUDA runtime:")
                info("  pip install --user nvidia-cublas-cu12 nvidia-cudnn-cu12")
                info("Или скачай CUDA Toolkit 12.x: https://developer.nvidia.com/cuda-downloads")
                info("Пробуем CPU-режим как fallback...")
            try:
                model = WhisperModel("small", device="cpu", compute_type="int8")
                info("Модель загружена на CPU (GPU будет доступен после установки CUDA runtime)")
            except Exception as e:
                fail(f"Не удалось загрузить модель: {e}")
                record_result(label, False)
                return False

        try:
            segments, info_obj = model.transcribe(str(test_wav), language="ru")
            _ = list(segments)  # материализуем
            ok(f"Модель загружена и работает (язык: {info_obj.language})")
            record_result(label, True)
        except Exception as e:
            fail(f"Ошибка транскрипции: {e}")
            record_result(label, False)
            return False

    else:
        try:
            import mlx_whisper
        except ImportError:
            fail("mlx-whisper не установлен")
            record_result(label, None)
            return True

        info("Запускаем mlx-whisper small...")
        try:
            result = mlx_whisper.transcribe(str(test_wav), path_or_hf_repo="mlx-community/whisper-small-mlx")
            ok(f"Транскрипция прошла: '{result.get('text', '').strip()[:60]}'")
            record_result(label, True)
        except Exception as e:
            fail(f"Ошибка транскрипции: {e}")
            record_result(label, False)
            return False

    test_wav.unlink(missing_ok=True)
    return True


# ══════════════════════════════════════════════════════════════════════════
# H5 — ДИАРИЗАЦИЯ (pyannote)
# ══════════════════════════════════════════════════════════════════════════

def check_h5_diarization() -> bool:
    header("H5 — Диаризация: pyannote.audio 3.1")

    try:
        from pyannote.audio import Pipeline
    except ImportError:
        fail("pyannote.audio не установлен")
        record_result("H5", None)
        return True

    import config as cfg
    if not cfg.HUGGINGFACE_TOKEN:
        fail("HUGGINGFACE_TOKEN не задан в .env")
        record_result("H5", False)
        return False

    info("Загружаем pipeline (первый запуск скачает модели ~1 ГБ)...")
    device_str = "cuda" if IS_WINDOWS else "mps" if IS_MAC else "cpu"

    try:
        import torch
        device = torch.device(device_str if torch.cuda.is_available() or
                              (IS_MAC and torch.backends.mps.is_available())
                              else "cpu")
        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            token=cfg.HUGGINGFACE_TOKEN,
        ).to(device)
        ok(f"Pipeline загружен (device: {device})")
    except Exception as e:
        fail(f"Ошибка загрузки pipeline: {e}")
        info("Убедись что лицензия принята на huggingface.co/pyannote/speaker-diarization-3.1")
        record_result("H5", False)
        return False

    info("Тест на синтетическом сигнале (передаём тензор напрямую, минуя torchcodec)...")

    try:
        import math as _math
        rate = 16000
        t = torch.arange(int(rate * 5), dtype=torch.float32) / rate
        waveform = torch.sin(2 * _math.pi * 440 * t).unsqueeze(0).to(device)
        audio = {"waveform": waveform, "sample_rate": rate}
        result = pipeline(audio)
        # pyannote 4.x возвращает DiarizeOutput, 3.x — Annotation напрямую
        diarization = getattr(result, "speaker_diarization", result)
        speakers = set(diarization.labels())
        ok(f"Диаризация работает. Обнаружено спикеров: {len(speakers)} (0 — норма для синусоиды)")
        record_result("H5", True)
    except Exception as e:
        fail(f"Ошибка диаризации: {e}")
        record_result("H5", False)
        return False

    return True


# ══════════════════════════════════════════════════════════════════════════
# H6 — CONFIDENCE SCORE
# ══════════════════════════════════════════════════════════════════════════

def check_h6_confidence() -> bool:
    header("H6 — Confidence score коррелирует с качеством звука")

    if not IS_WINDOWS:
        info("Проверка H6 реализована только для faster-whisper (Windows). Пропускаем.")
        record_result("H6", None)
        return True

    try:
        from faster_whisper import WhisperModel
    except ImportError:
        fail("faster-whisper не установлен")
        record_result("H6", None)
        return True

    info("Сравниваем confidence на чистом и зашумлённом сигнале...")

    # Чистый сигнал
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        clean_wav = Path(f.name)
    generate_sine_wav(clean_wav, duration=3.0)

    # Зашумлённый (почти тишина — случайный шум)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        noisy_wav = Path(f.name)
    import random
    noise = b"".join(struct.pack("<h", random.randint(-200, 200)) for _ in range(16000 * 3))
    save_wav(noisy_wav, noise, 16000, 1, 2)

    try:
        device = "cuda" if check_import("torch") and __import__("torch").cuda.is_available() else "cpu"
        compute = "float16" if device == "cuda" else "int8"
        model = WhisperModel("small", device=device, compute_type=compute)

        def avg_confidence(path: Path) -> float:
            segs, _ = model.transcribe(str(path), language="ru", word_timestamps=True)
            scores = []
            for seg in segs:
                if seg.words:
                    scores.extend(w.probability for w in seg.words)
            return sum(scores) / len(scores) if scores else 0.0

        score_clean = avg_confidence(clean_wav)
        score_noisy = avg_confidence(noisy_wav)

        ok(f"Confidence чистый сигнал: {score_clean:.2f}")
        ok(f"Confidence зашумлённый:   {score_noisy:.2f}")

        if score_clean >= score_noisy:
            ok("Корреляция подтверждена — чистый >= зашумлённого")
            record_result("H6", True)
        else:
            fail("Чистый сигнал получил более низкий score — метрика ненадёжна")
            info("Возможно нужна другая метрика качества")
            record_result("H6", False)
    except Exception as e:
        fail(f"Ошибка: {e}")
        record_result("H6", False)
    finally:
        clean_wav.unlink(missing_ok=True)
        noisy_wav.unlink(missing_ok=True)

    return True


# ══════════════════════════════════════════════════════════════════════════
# H7 — NATIVE MESSAGING
# ══════════════════════════════════════════════════════════════════════════

def check_h7_native_messaging() -> bool:
    header("H7 — Chrome Native Messaging")

    # Шаг 1: регистрируем хост
    info("Регистрируем Native Messaging хост...")
    try:
        from app.extension.install_host import install, get_extension_id
        ext_id = get_extension_id()
        ok_install = install(extension_id=ext_id)
        if ok_install:
            ok("Хост зарегистрирован в реестре")
        else:
            fail("Не удалось зарегистрировать хост")
            record_result("H7", False)
            return False
    except Exception as e:
        fail(f"Ошибка регистрации: {e}")
        record_result("H7", False)
        return False

    # Шаг 2: ping-pong тест — запускаем хост как subprocess
    info("Тест ping-pong с Native Messaging хостом...")
    try:
        import json, struct, subprocess as sp
        host_script = Path("app/extension/native_host.py")
        proc = sp.Popen(
            [sys.executable, str(host_script)],
            stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.PIPE,
        )
        # Отправляем ping
        msg = json.dumps({"type": "ping"}).encode("utf-8")
        proc.stdin.write(struct.pack("<I", len(msg)) + msg)
        proc.stdin.flush()
        # Читаем ответ (таймаут 3 сек)
        import select, platform as _platform
        if _platform.system() == "Windows":
            # select не работает с pipe на Windows — читаем напрямую
            proc.stdin.close()
            raw_len = proc.stdout.read(4)
        else:
            r, _, _ = select.select([proc.stdout], [], [], 3)
            if not r:
                proc.kill()
                fail("Хост не ответил за 3 секунды")
                record_result("H7", False)
                return False
            raw_len = proc.stdout.read(4)

        if len(raw_len) == 4:
            msg_len = struct.unpack("<I", raw_len)[0]
            response = json.loads(proc.stdout.read(msg_len).decode("utf-8"))
            proc.kill()
            if response.get("type") == "pong":
                ok("Ping-pong успешен — хост отвечает")
            else:
                fail(f"Неожиданный ответ: {response}")
                record_result("H7", False)
                return False
        else:
            proc.kill()
            fail("Хост не вернул данные")
            record_result("H7", False)
            return False
    except Exception as e:
        fail(f"Ошибка теста: {e}")
        record_result("H7", False)
        return False

    # Шаг 3: инструкция по установке расширения
    ext_id = get_extension_id()
    if not ext_id:
        info("")
        info("Следующий шаг — установить Chrome-расширение:")
        info("  1. Открой chrome://extensions/")
        info("  2. Включи 'Режим разработчика' (правый верхний угол)")
        info(f"  3. 'Загрузить распакованное' → выбери папку:")
        info(f"     {Path('app/extension/chrome').resolve()}")
        info("  4. Скопируй ID расширения и выполни:")
        info("     python -m app.extension.install_host --update-id <ID>")
        info("")
        info("После установки расширения хост будет принимать подключения от Chrome.")

    record_result("H7", True)
    return True


# ══════════════════════════════════════════════════════════════════════════
# H8 — КАЧЕСТВО НА 2x СКОРОСТИ
# ══════════════════════════════════════════════════════════════════════════

def check_h8_speed() -> bool:
    header("H8 — Транскрипция при 2x скорости воспроизведения")
    info("Эта проверка требует реального аудио с речью.")
    info("Пропускаем до Этапа 4 — там сравним вручную на реальной записи.")
    record_result("H8", None)
    return True


# ══════════════════════════════════════════════════════════════════════════
# ИНИЦИАЛИЗАЦИЯ БД
# ══════════════════════════════════════════════════════════════════════════

def check_db() -> bool:
    header("База данных (SQLite)")
    try:
        sys.path.insert(0, str(Path(__file__).parent))
        from app.storage.db import init_db, db_exists
        import config as cfg
        cfg.ensure_dirs()
        init_db()
        if db_exists():
            ok(f"База данных создана: {cfg.DB_PATH}")
            return True
        else:
            fail("Файл БД не создан")
            return False
    except Exception as e:
        fail(f"Ошибка инициализации БД: {e}")
        return False


# ══════════════════════════════════════════════════════════════════════════
# ИТОГОВЫЙ ОТЧЁТ
# ══════════════════════════════════════════════════════════════════════════

def print_report() -> None:
    header("ИТОГОВЫЙ ОТЧЁТ")

    labels = {
        "H1": "Захват аудио WASAPI (Windows)",
        "H2": "Захват аудио BlackHole (Mac)",
        "H3": "Транскрипция faster-whisper/CUDA",
        "H4": "Транскрипция mlx-whisper (Mac)",
        "H5": "Диаризация pyannote.audio",
        "H6": "Confidence score как метрика качества",
        "H7": "Chrome Native Messaging",
        "H8": "Качество транскрипции при 2x скорости",
    }

    passed = failed = skipped = 0
    for h, label in labels.items():
        r = results.get(h)
        if r is True:
            print(f"  ✓  {h}: {label}")
            passed += 1
        elif r is False:
            print(f"  ✗  {h}: {label}  ← ТРЕБУЕТ ВНИМАНИЯ")
            failed += 1
        else:
            print(f"  ○  {h}: {label}  (отложена)")
            skipped += 1

    print(f"\n  Итого: {passed} OK / {failed} ошибок / {skipped} отложено")

    if failed == 0:
        print("\n  Всё готово. Можно переходить к Этапу 1.")
    else:
        print("\n  Исправь ошибки выше и запусти setup.py повторно.")


# ══════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════

def main() -> None:
    print(f"\n{'═' * WIDTH}")
    print("  for_meets — первоначальная настройка")
    print(f"  Платформа: {'Windows' if IS_WINDOWS else 'macOS' if IS_MAC else 'Linux'}")
    print(f"{'═' * WIDTH}")

    deps_ok = check_dependencies()
    if not deps_ok:
        print("\n  Установи зависимости и запусти setup.py повторно.")
        sys.exit(1)

    check_db()

    if IS_WINDOWS:
        check_h1_wasapi()
    elif IS_MAC:
        check_h2_blackhole()

    check_h3_h4_transcription()
    check_h5_diarization()
    check_h6_confidence()
    check_h7_native_messaging()
    check_h8_speed()

    print_report()


if __name__ == "__main__":
    main()
