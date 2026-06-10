"""
Native Messaging хост для Chrome-расширения.

Протокол Native Messaging:
- Каждое сообщение: 4 байта длины (little-endian) + JSON
- stdin → читаем сообщения от Chrome
- stdout → отправляем ответы

Архитектура автостарта записи:
Chrome запускает этот скрипт (PyInstaller-сборку dist/for_meets_host/
for_meets_host.exe) как ОТДЕЛЬНЫЙ процесс при коннекте расширения.
Tray-приложение работает само по себе — у них нет общего stdin/stdout.
Поэтому хост пересылает события (meet_started / meet_ended / tabs)
в приложение через локальный TCP-сокет 127.0.0.1:48765 (JSON-строки).
Приложение поднимает сервер на этом порту (app/main.py).

Если приложение не запущено — события просто дропаются (хост жив,
Chrome не переподключается, при следующем событии попробуем снова).

ВАЖНО: файл должен оставаться stdlib-only — он собирается PyInstaller'ом
в лёгкий exe без зависимостей проекта.
"""

import json
import socket
import struct
import sys
from typing import Callable

# Порт локального моста хост → tray-приложение.
# Менять синхронно с app/main.py (он импортирует константу отсюда).
BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 48765


def read_message() -> dict | None:
    """Читает одно сообщение из stdin."""
    raw_len = sys.stdin.buffer.read(4)
    if len(raw_len) < 4:
        return None
    msg_len = struct.unpack("<I", raw_len)[0]
    raw_msg = sys.stdin.buffer.read(msg_len)
    if len(raw_msg) < msg_len:
        return None
    return json.loads(raw_msg.decode("utf-8"))


def send_message(msg: dict) -> None:
    """Отправляет одно сообщение в stdout."""
    encoded = json.dumps(msg).encode("utf-8")
    sys.stdout.buffer.write(struct.pack("<I", len(encoded)))
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()


class AppBridge:
    """Пересылает сообщения в работающее tray-приложение по TCP (JSON-строки)."""

    def __init__(self) -> None:
        self._sock: socket.socket | None = None

    def forward(self, msg: dict) -> bool:
        data = (json.dumps(msg) + "\n").encode("utf-8")
        # Две попытки: вторая — с переподключением, если сокет протух
        # (приложение перезапускалось между событиями).
        for _ in range(2):
            try:
                if self._sock is None:
                    self._sock = socket.create_connection(
                        (BRIDGE_HOST, BRIDGE_PORT), timeout=1.0,
                    )
                self._sock.sendall(data)
                return True
            except OSError:
                if self._sock is not None:
                    try:
                        self._sock.close()
                    except OSError:
                        pass
                    self._sock = None
        return False

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None


class NativeHost:
    """
    Запускается как отдельный процесс Chrome Native Messaging.
    Принимает сообщения от расширения и вызывает обработчики.
    """

    def __init__(self):
        self._handlers: dict[str, Callable] = {}
        self._running = False

    def on(self, msg_type: str, handler: Callable) -> None:
        """Регистрирует обработчик для типа сообщения."""
        self._handlers[msg_type] = handler

    def send(self, msg: dict) -> None:
        send_message(msg)

    def run(self) -> None:
        """Основной цикл чтения сообщений."""
        import logging
        self._running = True
        while self._running:
            msg = read_message()
            if msg is None:
                break
            msg_type = msg.get("type")
            logging.info(f"received: {msg_type}")
            handler = self._handlers.get(msg_type)
            if handler:
                try:
                    result = handler(msg)
                    if result is not None:
                        self.send(result)
                except Exception as e:
                    self.send({"type": "error", "message": str(e)})

    def stop(self) -> None:
        self._running = False


# Типы сообщений, которые пересылаются в tray-приложение
_FORWARDED_TYPES = ("meet_started", "meet_ended", "tabs")


def run_host() -> None:
    """
    Точка входа standalone-хоста (запускается Chrome).
    Пересылает события расширения в tray-приложение через AppBridge.
    """
    import logging

    bridge = AppBridge()
    host = NativeHost()

    host.on("ping", lambda msg: {"type": "pong"})

    def _make_forwarder(msg_type: str) -> Callable:
        def _handler(msg: dict):
            ok = bridge.forward(msg)
            logging.info("forward %s -> app: %s", msg_type, "ok" if ok else "app not running")
            return None
        return _handler

    for t in _FORWARDED_TYPES:
        host.on(t, _make_forwarder(t))

    try:
        host.run()
    finally:
        bridge.close()


if __name__ == "__main__":
    import logging
    from pathlib import Path
    log_path = Path(__file__).parent / "native_host.log"
    try:
        logging.basicConfig(
            filename=str(log_path),
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(message)s",
        )
    except OSError:
        pass  # exe может лежать в read-only месте — работаем без лога
    logging.info("native_host started")
    try:
        run_host()
        logging.info("native_host exited normally")
    except Exception as e:
        logging.exception(f"native_host crashed: {e}")
