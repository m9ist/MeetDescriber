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
from pathlib import Path
from typing import Callable, Optional

import pystray
from PIL import Image, ImageDraw


def _make_icon(recording: bool = False) -> Image.Image:
    """Рисует простую иконку 64x64."""
    import math
    S = 64
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    if recording:
        # Красный круг + белый квадрат (стоп)
        draw.ellipse([2, 2, 61, 61], fill="#e84040")
        draw.rectangle([22, 22, 42, 42], fill="white")
    else:
        # Тёмно-синий круг + синусоида + красная точка записи (ожидание)
        draw.ellipse([2, 2, 61, 61], fill="#1a1a2e")
        pts = []
        for px in range(8, 57):
            t = (px - 8) / (56 - 8) * 2 * math.pi * 2.5
            py = int(32 + math.sin(t) * 14 * (1 - abs(px - 32) / 32))
            pts.append((px, py))
        for i in range(len(pts) - 1):
            draw.line([pts[i], pts[i + 1]], fill="#26c6da", width=2)
        draw.ellipse([27, 27, 37, 37], fill="#ef5350", outline="#ffffff", width=1)

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
        on_edit_job: Callable[[int], None] = None,
        on_delete_job: Callable[[int], None] = None,
        on_delete_all_pending: Callable[[], None] = None,
    ) -> None:
        self._on_start_manual = on_start_manual
        self._on_stop = on_stop
        self._on_process_job = on_process_job
        self._on_quit = on_quit
        self._on_edit_job = on_edit_job
        self._on_delete_job = on_delete_job
        self._on_delete_all_pending = on_delete_all_pending

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
                    self._make_pending_submenu(j),
                )
                for j in self._pending_jobs
            ]
            pending_items.append(pystray.Menu.SEPARATOR)
            pending_items.append(pystray.MenuItem(
                "Удалить все необработанные",
                self._handle_delete_all_pending,
            ))
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
                    self._make_done_submenu(j),
                )
                for j in self._done_jobs[-10:]
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

    def _make_done_submenu(self, job: dict) -> pystray.Menu:
        import os

        def open_folder(icon, item):
            # Ищем первый существующий файл, открываем его папку
            for key in ("transcription_path", "analysis_path", "followup_path"):
                p = job.get(key)
                if p and Path(p).exists():
                    os.startfile(str(Path(p).parent))
                    return

        def make_open_file(path_str):
            def handler(icon, item):
                if path_str and Path(path_str).exists():
                    os.startfile(path_str)
            return handler

        def edit_info(icon, item):
            if self._on_edit_job:
                self._on_edit_job(job["id"])

        tr = job.get("transcription_path")
        an = job.get("analysis_path")
        fu = job.get("followup_path")

        return pystray.Menu(
            pystray.MenuItem("Открыть расположение", open_folder),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Транскрипция",
                make_open_file(tr),
                enabled=bool(tr and Path(tr).exists()),
            ),
            pystray.MenuItem(
                "Анализ",
                make_open_file(an),
                enabled=bool(an and Path(an).exists()),
            ),
            pystray.MenuItem(
                "Follow-up",
                make_open_file(fu),
                enabled=bool(fu and Path(fu).exists()),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Информация о совещании", edit_info),
        )

    def _make_pending_submenu(self, job: dict) -> pystray.Menu:
        job_id = job["id"]

        def process(icon, item):
            self._on_process_job(job_id)

        def delete(icon, item):
            if self._on_delete_job:
                self._on_delete_job(job_id)

        return pystray.Menu(
            pystray.MenuItem("Обработать", process),
            pystray.MenuItem("Удалить", delete),
        )

    def _make_job_handler(self, job_id: int) -> Callable:
        def handler(icon, item):
            self._on_process_job(job_id)
        return handler

    def _handle_delete_all_pending(self, icon, item) -> None:
        if self._on_delete_all_pending:
            self._on_delete_all_pending()

    def _handle_start(self, icon, item) -> None:
        self._on_start_manual()

    def _handle_stop(self, icon, item) -> None:
        self._on_stop()

    def _handle_quit(self, icon, item) -> None:
        self._on_quit()
        icon.stop()
