"""
Native Messaging хост для Chrome-расширения.

Протокол Native Messaging:
- Каждое сообщение: 4 байта длины (little-endian) + JSON
- stdin → читаем сообщения от Chrome
- stdout → отправляем ответы

Запускается Chrome автоматически при подключении расширения.
"""

import json
import struct
import sys
import threading
from typing import Callable


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


def run_host(
    on_meet_started: Callable | None = None,
    on_meet_ended: Callable | None = None,
) -> None:
    """
    Точка входа для запуска хоста.
    Вызывается Chrome как отдельный процесс.
    """
    host = NativeHost()

    host.on("ping", lambda msg: {"type": "pong"})

    host.on("get_tabs", lambda msg: None)  # Chrome сам шлёт tabs в ответ

    if on_meet_started:
        def _meet_started(msg):
            on_meet_started(
                tab_id=msg.get("tab_id"),
                title=msg.get("title", ""),
                tabs=msg.get("tabs", []),
            )
        host.on("meet_started", _meet_started)

    if on_meet_ended:
        def _meet_ended(msg):
            on_meet_ended(tab_id=msg.get("tab_id"))
        host.on("meet_ended", _meet_ended)

    host.run()


if __name__ == "__main__":
    import logging
    from pathlib import Path
    log_path = Path(__file__).parent / "native_host.log"
    logging.basicConfig(
        filename=str(log_path),
        level=logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logging.info("native_host started")
    try:
        run_host()
        logging.info("native_host exited normally")
    except Exception as e:
        logging.exception(f"native_host crashed: {e}")
