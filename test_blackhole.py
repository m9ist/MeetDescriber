"""
Тест захвата аудио через BlackHole на Mac.
Запуск: .venv/bin/python test_blackhole.py

Перед запуском:
  1. brew install blackhole-2ch  (и перезагрузка)
  2. Audio MIDI Setup → Multi-Output Device (BlackHole 2ch + динамики)
  3. System Settings → Sound → Output → выбрать Multi-Output Device
  4. Воспроизведи любой звук
"""
import sys
import struct
import wave
import tempfile
from pathlib import Path

def main():
    try:
        import sounddevice as sd
    except ImportError:
        print("✗  sounddevice не установлен: pip install sounddevice")
        sys.exit(1)

    print("Доступные аудиоустройства:")
    devices = sd.query_devices()
    for i, d in enumerate(devices):
        marker = " ← BlackHole" if "BlackHole" in d["name"] else ""
        if d["max_input_channels"] > 0:
            print(f"  [{i}] {d['name']} (in: {d['max_input_channels']}){marker}")

    bh_idx = next((i for i, d in enumerate(devices) if "BlackHole" in d["name"]), None)
    if bh_idx is None:
        print("\n✗  BlackHole не найден.")
        print("   Установи: brew install blackhole-2ch  (требует перезагрузки)")
        sys.exit(1)

    print(f"\n✓  Найден BlackHole: [{bh_idx}] {devices[bh_idx]['name']}")
    print("\nЗаписываем 5 секунд...")
    print("   → Воспроизведи любой звук сейчас\n")

    rate = 48000
    duration = 5
    try:
        recording = sd.rec(
            int(duration * rate),
            samplerate=rate,
            channels=2,
            dtype="int16",
            device=bh_idx,
        )
        sd.wait()
    except Exception as e:
        print(f"✗  Ошибка записи: {e}")
        sys.exit(1)

    flat = recording.flatten()
    rms = (sum(int(v) ** 2 for v in flat) / len(flat)) ** 0.5
    print(f"   RMS = {rms:.1f}  (порог тишины: {100})")

    if rms < 100:
        print("✗  Сигнал слишком тихий — звук не прошёл через BlackHole.")
        print("   Проверь настройку Multi-Output Device в Audio MIDI Setup.")
        sys.exit(1)

    # Сохраняем WAV
    out = Path(tempfile.mktemp(suffix=".wav"))
    with wave.open(str(out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(recording.tobytes())

    print(f"✓  Звук захвачен успешно (RMS={rms:.1f})")
    print(f"   Сохранён: {out}")
    print("\n✓  H2 подтверждена — BlackHole работает")

if __name__ == "__main__":
    main()
