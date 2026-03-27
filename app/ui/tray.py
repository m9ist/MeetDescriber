"""
Системный трей (Windows) / Menu bar (Mac).

Меню:
  - Статус (записывает / ожидает)
  - ──────────────────
  - Необработанные задания  ▶  [список сессий]
  - Обработанные задания    ▶  [список сессий]
  - ──────────────────
  - Начать запись вручную
  - Остановить запись  (если идёт)
  - ──────────────────
  - Выход
"""
import threading
from typing import Callable, Optional

import pystray
from PIL import Image, ImageDraw


def _make_icon(recording: bool = False) -> Image.Image:
    """Рисует простую иконку 64x64."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = "#e84040" if recording else "#4a90d9"
    draw.ellipse([8, 8, 56, 56], fill=color)
    if recording:
        # Квадрат = стоп
        draw.rectangle([22, 22, 42, 42], fill="white")
    else:
        # Треугольник = готов
        draw.polygon([(24, 18), (24, 46), (48, 32)], fill="white")
    return img


class ForMeetsTray:
    """
    Управляет иконкой в трее и реагирует на действия пользователя.

    Коллбэки:
        on_start_manual()      — пользователь нажал "Начать запись вручную"
        on_stop()              — пользователь нажал "Остановить запись"
        on_process_job(id)     — пользователь выбрал задание для обработки
        on_quit()              — пользователь выбрал "Выход"
    """

    def __init__(
        self,
        on_start_manual: Callable[[], None],
        on_stop: Callable[[], None],
        on_process_job: Callable[[int], None],
        on_quit: Callable[[], None],
    ) -> None:
        self._on_start_manual = on_start_manual
        self._on_stop = on_stop
        self._on_process_job = on_process_job
        self._on_quit = on_quit

        self._recording = False
        self._status_text = "Ожидание"
        self._pending_jobs: list[dict] = []   # [{id, title, started_at}]
        self._done_jobs: list[dict] = []

        self._icon: Optional[pystray.Icon] = None

    # ── Публичные методы (потокобезопасны) ───────────────────────────────────

    def set_recording(self, recording: bool, title: str = "") -> None:
        self._recording = recording
        self._status_text = f"Записывает: {title}" if recording else "Ожидание"
        self._refresh()

    def set_jobs(self, pending: list[dict], done: list[dict]) -> None:
        self._pending_jobs = pending
        self._done_jobs = done
        self._refresh()

    def start(self) -> None:
        """Запускает трей в отдельном потоке."""
        threading.Thread(target=self._run, daemon=True, name="tray").start()

    def stop(self) -> None:
        if self._icon:
            self._icon.stop()

    # ── Внутреннее ────────────────────────────────────────────────────────────

    def _run(self) -> None:
        self._icon = pystray.Icon(
            "for_meets",
            _make_icon(self._recording),
            "for_meets",
            menu=self._build_menu(),
        )
        self._icon.run()

    def _refresh(self) -> None:
        if self._icon:
            self._icon.icon = _make_icon(self._recording)
            self._icon.title = f"for_meets — {self._status_text}"
            self._icon.menu = self._build_menu()

    def _build_menu(self) -> pystray.Menu:
        items: list = []

        # Статус
        items.append(pystray.MenuItem(
            self._status_text,
            None,
            enabled=False,
        ))
        items.append(pystray.Menu.SEPARATOR)

        # Необработанные задания
        if self._pending_jobs:
            pending_items = [
                pystray.MenuItem(
                    self._job_label(j),
                    self._make_job_handler(j["id"]),
                )
                for j in self._pending_jobs
            ]
            items.append(pystray.MenuItem(
                f"Необработанные ({len(self._pending_jobs)})",
                pystray.Menu(*pending_items),
            ))
        else:
            items.append(pystray.MenuItem(
                "Необработанные задания",
                None,
                enabled=False,
            ))

        # Обработанные задания
        if self._done_jobs:
            done_items = [
                pystray.MenuItem(
                    self._job_label(j),
                    self._make_job_handler(j["id"]),
                )
                for j in self._done_jobs[-10:]  # последние 10
            ]
            items.append(pystray.MenuItem(
                f"Обработанные ({len(self._done_jobs)})",
                pystray.Menu(*done_items),
            ))

        items.append(pystray.Menu.SEPARATOR)

        # Запуск / остановка
        if self._recording:
            items.append(pystray.MenuItem("Остановить запись", self._handle_stop))
        else:
            items.append(pystray.MenuItem("Начать запись вручную", self._handle_start))

        items.append(pystray.Menu.SEPARATOR)
        items.append(pystray.MenuItem("Выход", self._handle_quit))

        return pystray.Menu(*items)

    @staticmethod
    def _job_label(job: dict) -> str:
        title = job.get("title") or "Без названия"
        dt = (job.get("started_at") or "")
        date = dt[:10]
        time = dt[11:16]
        label_dt = f"{date} {time}" if time else date
        suffix = " → анализ" if job.get("status") == "transcribed" else ""
        return f"{label_dt}  {title}{suffix}"

    def _make_job_handler(self, job_id: int) -> Callable:
        def handler(icon, item):
            self._on_process_job(job_id)
        return handler

    def _handle_start(self, icon, item) -> None:
        self._on_start_manual()

    def _handle_stop(self, icon, item) -> None:
        self._on_stop()

    def _handle_quit(self, icon, item) -> None:
        self._on_quit()
        icon.stop()
