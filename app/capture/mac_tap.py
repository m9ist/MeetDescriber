"""
Захват системного звука на macOS через Core Audio Tap (audiotee).

audiotee — Swift-CLI поверх Apple Core Audio Taps API (macOS 14.4+).
Бинарь бандлится в MeetDescriber.app/Contents/Resources/audiotee и
запускается subprocess'ом. Читаем raw PCM из stdout, structured JSON-логи
из stderr.

Преимущества vs BlackHole:
  - Не нужен виртуальный аудио-драйвер
  - Не нужно настраивать Multi-Output Device
  - Можно тапать только конкретные процессы (Chrome) — фоновая музыка не попадёт
  - Один permission-prompt от macOS («System Audio Recording»)

Архитектура:
  - audiotee пишет PCM в stdout (s16le, 48kHz, stereo по умолчанию)
  - JSON-логи в stderr (один объект на строку), включая metadata и stream_start
  - При SIGTERM корректно завершается (graceful shutdown с stream_stop)
"""
from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

import config

log = logging.getLogger(__name__)

# Кандидаты на размещение audiotee. Первый существующий выигрывает.
_AUDIOTEE_CANDIDATES = [
    # В .app бандле
    config.ROOT_DIR / "MeetDescriber.app/Contents/Resources/audiotee",
    # В исходниках для dev-запуска без .app
    config.ROOT_DIR / "vendor/audiotee/.build/release/audiotee",
]


def find_audiotee_binary() -> Optional[Path]:
    """Возвращает путь к audiotee бинарю или None если не найден."""
    # Если запущены ВНУТРИ .app бандла — sys.executable / argv[0] подскажут
    # его путь, лучше искать audiotee рядом
    for p in _AUDIOTEE_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def is_available() -> bool:
    """True если audiotee бинарь существует И macOS поддерживает Core Audio Taps."""
    if not config.IS_MAC:
        return False
    # macOS 14.4+ — Core Audio Taps API
    try:
        import platform
        ver = platform.mac_ver()[0]
        major, minor = [int(x) for x in ver.split(".")[:2]]
        if (major, minor) < (14, 4):
            return False
    except Exception:
        return False
    return find_audiotee_binary() is not None


class MacTapCapture:
    """
    Захват системного звука через audiotee subprocess.

    Использование:
        tap = MacTapCapture(sample_rate=48000, stereo=True)
        tap.on_frame = lambda raw: process_pcm(raw)
        tap.start(include_pids=[chrome_pid])  # или [] для всех процессов
        ...
        tap.stop()

    PCM поступает в on_frame блоками по ~chunk_duration_sec.
    Формат — s16le, mono или stereo по флагу `stereo`.
    """

    def __init__(
        self,
        sample_rate: int = 48000,
        stereo: bool = True,
        chunk_duration_sec: float = 0.2,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = 2 if stereo else 1
        self.chunk_duration_sec = chunk_duration_sec
        self.bytes_per_frame = 2 * self.channels  # s16le

        self.on_frame: Optional[Callable[[bytes], None]] = None
        self.on_error: Optional[Callable[[Exception], None]] = None

        self._proc: Optional[subprocess.Popen] = None
        self._stdout_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._running = False
        self._stream_started = threading.Event()

    def start(self, include_pids: Optional[list[int]] = None,
              exclude_pids: Optional[list[int]] = None) -> None:
        """Запускает audiotee subprocess.

        include_pids: тапать только эти процессы (None/[] = все).
        exclude_pids: исключить эти процессы (взаимоисключающее с include).
        """
        if self._proc is not None:
            return

        binary = find_audiotee_binary()
        if binary is None:
            raise RuntimeError(
                "audiotee бинарь не найден. Пересобери .app: bash create_mac_app.sh"
            )

        args = [
            str(binary),
            "--sample-rate", str(self.sample_rate),
            "--chunk-duration", str(self.chunk_duration_sec),
        ]
        if self.channels == 2:
            args.append("--stereo")
        if include_pids:
            args.append("--include-processes")
            args.extend(str(p) for p in include_pids)
        elif exclude_pids:
            args.append("--exclude-processes")
            args.extend(str(p) for p in exclude_pids)

        log.info("Запускаю audiotee: %s", " ".join(args))
        self._proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._running = True
        self._stream_started.clear()

        self._stdout_thread = threading.Thread(
            target=self._read_stdout, daemon=True, name="audiotee-stdout"
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, daemon=True, name="audiotee-stderr"
        )
        self._stdout_thread.start()
        self._stderr_thread.start()

        # Ждём stream_start — гарантия что тап реально стартовал
        if not self._stream_started.wait(timeout=10.0):
            log.warning("audiotee stream_start не получен за 10 сек")

    def stop(self) -> None:
        """Корректно завершает audiotee subprocess."""
        if self._proc is None:
            return
        self._running = False
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            log.warning("audiotee не завершился за 5 сек — kill")
            self._proc.kill()
            self._proc.wait(timeout=2.0)
        except Exception:
            pass
        finally:
            self._proc = None

        for t in (self._stdout_thread, self._stderr_thread):
            if t is not None:
                t.join(timeout=2.0)
        self._stdout_thread = None
        self._stderr_thread = None

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _read_stdout(self) -> None:
        """Читает raw PCM из stdout и форвардит в on_frame."""
        # Размер одного chunk_duration в байтах
        chunk_bytes = int(self.sample_rate * self.chunk_duration_sec) * self.bytes_per_frame
        buf = b""
        try:
            while self._running and self._proc is not None:
                data = self._proc.stdout.read1(64 * 1024) if hasattr(self._proc.stdout, 'read1') \
                    else os.read(self._proc.stdout.fileno(), 64 * 1024)
                if not data:
                    break
                buf += data
                # Отдаём фреймами кратными bytes_per_frame, чтобы не рвать сэмпл
                ready = (len(buf) // self.bytes_per_frame) * self.bytes_per_frame
                if ready > 0:
                    chunk, buf = buf[:ready], buf[ready:]
                    if self.on_frame:
                        try:
                            self.on_frame(chunk)
                        except Exception:
                            log.exception("on_frame callback failed")
        except Exception as exc:
            log.error("audiotee stdout reader error: %s", exc)
            if self.on_error:
                self.on_error(exc)

    def _read_stderr(self) -> None:
        """Читает structured JSON-логи из stderr."""
        try:
            for line_bytes in self._proc.stderr:
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("[audiotee] %s", line)
                    continue

                mtype = msg.get("message_type", "info")
                data = msg.get("data", {})
                text = data.get("message", "") if isinstance(data, dict) else str(data)

                if mtype == "stream_start":
                    self._stream_started.set()
                    log.info("[audiotee] stream_start")
                elif mtype == "stream_stop":
                    log.info("[audiotee] stream_stop")
                elif mtype == "metadata":
                    log.info("[audiotee] metadata: %s", json.dumps(data, ensure_ascii=False))
                elif mtype == "error":
                    log.error("[audiotee] %s", text or line)
                elif mtype == "info":
                    log.info("[audiotee] %s", text)
                elif mtype == "debug":
                    log.debug("[audiotee] %s", text)
                else:
                    log.debug("[audiotee:%s] %s", mtype, line)
        except Exception:
            log.exception("audiotee stderr reader error")
