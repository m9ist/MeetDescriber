"""
Тест захвата системного звука через Core Audio Tap (audiotee) на Mac.
Запуск:  .venv/bin/python test_mac_tap.py

Требования:
  1. macOS 14.4+
  2. Собран audiotee бинарь: bash create_mac_app.sh
  3. Разрешение в System Settings → Privacy & Security → System Audio Recording
     (выдаётся при первом запуске .app, но для CLI-теста нужно вручную или запуск из Terminal.app)
"""
import sys
import time
import threading
import wave
from pathlib import Path

# Корень репозитория в sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent))


def main():
    try:
        from app.capture.mac_tap import MacTapCapture, is_available, find_audiotee_binary
    except ImportError as e:
        print(f"✗ Не удалось импортировать mac_tap: {e}")
        sys.exit(1)

    if not is_available():
        print("✗ Core Audio Tap недоступен.")
        print("  Требования: macOS 14.4+ и собранный audiotee (bash create_mac_app.sh)")
        sys.exit(1)

    binary = find_audiotee_binary()
    print(f"✓ audiotee binary: {binary}")

    sample_rate = 48000
    duration = 5
    tap = MacTapCapture(sample_rate=sample_rate, stereo=True, chunk_duration_sec=0.2)

    collected = bytearray()
    tap.on_frame = lambda raw: collected.extend(raw)
    tap.on_error = lambda e: print(f"✗ Tap error: {e}")

    print(f"\nЗапускаю tap на {duration} сек...")
    print("→ Воспроизведи системный звук (afplay, музыка, видео)")
    tap.start(include_pids=None)
    time.sleep(duration)
    tap.stop()

    # Считаем RMS
    import struct
    count = len(collected) // 2
    if count == 0:
        print("✗ Захвачено 0 байт")
        sys.exit(1)

    samples = struct.unpack(f"<{count}h", bytes(collected))
    rms = (sum(v * v for v in samples) / count) ** 0.5
    peak = max(abs(v) for v in samples)
    print(f"\nЗахвачено {len(collected)} байт ({len(collected) / (sample_rate*4):.2f}с stereo s16)")
    print(f"RMS={rms:.1f}  peak={peak}")

    if rms < 10:
        print("\n✗ Сигнал нулевой — обычно permission не выдано.")
        print("   Проверь System Settings → Privacy & Security → System Audio Recording.")
        print("   Если запускаешь из IDE/sandbox-терминала — попробуй из Apple Terminal.app")
        print("   или из готового .app бандла (open MeetDescriber.app).")
        sys.exit(1)

    out = Path("/tmp") / "mac_tap_test.wav"
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(bytes(collected))

    print(f"✓ Сохранено: {out}")
    print("✓ Core Audio Tap работает")


if __name__ == "__main__":
    main()
