"""
Точка входа приложения for_meets.

Жизненный цикл:
  1. Инициализация БД и конфига
  2. Запуск Native Messaging хоста (слушает Chrome)
  3. Запуск tray-иконки
  4. Главный поток — tkinter mainloop (все диалоги через него)

Встреча (Google Meet):
  Chrome → meet_started → уведомление → AudioCapture.start()
  Chrome → meet_ended   → AudioCapture.stop() → "Обработать сейчас?"

Ручной запуск:
  Tray → "Начать запись" → диалог → AudioCapture.start()
"""
import sys
import io
import threading
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import config
from app.storage.db import init_db, get_conn
from app.capture.audio_capture import AudioCapture, list_audio_sources
from app.extension.native_host import NativeHost, read_message, send_message
from app.ui import notifications, tray as tray_module, dialogs


class App:
    def __init__(self) -> None:
        init_db()

        self._capture: Optional[AudioCapture] = None
        self._current_session_id: Optional[int] = None
        self._current_title: str = ""
        self._skip_tab_ids: set[int] = set()  # встречи помеченные "не записывать"
        self._latest_tabs: list[dict] = []    # последний известный список вкладок Chrome

        # Tkinter root — главный поток
        self._root = tk.Tk()
        self._root.withdraw()
        self._root.title("for_meets")
        notifications.set_root(self._root)

        # Tray
        self._tray = tray_module.ForMeetsTray(
            on_start_manual=self._on_start_manual,
            on_stop=self._on_stop_manual,
            on_process_job=self._on_process_job,
            on_quit=self._on_quit,
        )

        # Native host thread
        self._host = NativeHost()
        self._host.on("meet_started", self._handle_meet_started)
        self._host.on("meet_ended", self._handle_meet_ended)
        self._host.on("tabs", self._handle_tabs)
        self._host.on("ping", lambda msg: {"type": "pong"})

    # ── Запуск ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        self._tray.start()
        self._refresh_tray_jobs()

        threading.Thread(
            target=self._host.run,
            daemon=True,
            name="native-host",
        ).start()

        self._root.mainloop()

    # ── Обработчики Chrome ────────────────────────────────────────────────────

    def _handle_meet_started(self, msg: dict) -> None:
        tab_id = msg.get("tab_id")
        title = msg.get("title", "Google Meet")
        self._latest_tabs = msg.get("tabs", [])

        if tab_id in self._skip_tab_ids:
            return

        if self._capture and self._capture.is_recording:
            return  # уже пишем

        def show():
            def skip():
                self._skip_tab_ids.add(tab_id)

            notifications.recording_started(title, on_skip=skip)

            # Начинаем запись (уведомление уже показано — пользователь может нажать "не записывать")
            self._start_session(title=title, source="meet")

        self._root.after(0, show)

    def _handle_meet_ended(self, msg: dict) -> None:
        tab_id = msg.get("tab_id")
        self._skip_tab_ids.discard(tab_id)
        self._root.after(0, self._stop_and_offer_processing)

    def _handle_tabs(self, msg: dict) -> None:
        self._latest_tabs = msg.get("tabs", [])

    # ── Ручной запуск ─────────────────────────────────────────────────────────

    def _on_start_manual(self) -> None:
        def show():
            sources = list_audio_sources()
            result = dialogs.ask_meeting_info(
                self._root,
                show_source_selector=True,
                audio_sources=sources,
                browser_tabs=self._latest_tabs,
            )
            if result:
                device_index = None
                if result["source"] and result["source"].get("type") == "audio":
                    device_index = result["source"].get("index")
                self._start_session(
                    title=result["title"],
                    agenda=result["agenda"],
                    source="manual",
                    device_index=device_index,
                )
        self._root.after(0, show)

    def _on_stop_manual(self) -> None:
        self._root.after(0, self._stop_and_offer_processing)

    # ── Сессия ────────────────────────────────────────────────────────────────

    def _start_session(
        self,
        title: str,
        agenda: str = "",
        source: str = "meet",
        device_index: Optional[int] = None,
    ) -> None:
        if self._capture and self._capture.is_recording:
            return

        now = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            cur = conn.execute(
                "INSERT INTO sessions (title, agenda, started_at, source) VALUES (?,?,?,?)",
                (title or "Встреча", agenda, now, source),
            )
            self._current_session_id = cur.lastrowid

        self._current_title = title or "Встреча"
        session_dir = config.RECORDINGS_DIR / f"session_{self._current_session_id}"

        self._capture = AudioCapture(session_dir=session_dir)
        self._capture.on_quality_low = lambda idx, score: notifications.quality_warning(idx, score)
        self._capture.on_error = lambda e: print(f"[capture error] {e}", file=sys.stderr)
        self._capture.start(device_index=device_index)

        self._tray.set_recording(True, self._current_title)

    def _stop_and_offer_processing(self) -> None:
        if not self._capture or not self._capture.is_recording:
            return

        self._capture.stop()
        self._capture = None

        session_id = self._current_session_id
        title = self._current_title
        self._current_session_id = None
        self._current_title = ""

        if session_id is None:
            return

        now = datetime.now(timezone.utc).isoformat()
        with get_conn() as conn:
            conn.execute("UPDATE sessions SET ended_at=? WHERE id=?", (now, session_id))

        self._tray.set_recording(False)
        self._refresh_tray_jobs()

        def process():
            self._create_job(session_id)
            self._refresh_tray_jobs()

        def later():
            pass  # задание уже создано при остановке — видно в трее

        # Создаём задание сразу (pending), предлагаем обработать
        self._create_job(session_id)
        self._refresh_tray_jobs()
        notifications.process_now(title, on_process=process, on_later=later)

    def _create_job(self, session_id: int) -> None:
        with get_conn() as conn:
            existing = conn.execute(
                "SELECT id FROM jobs WHERE session_id=?", (session_id,)
            ).fetchone()
            if not existing:
                conn.execute(
                    "INSERT INTO jobs (session_id, status) VALUES (?,?)",
                    (session_id, "pending"),
                )

    # ── Задания ───────────────────────────────────────────────────────────────

    def _on_process_job(self, job_id: int) -> None:
        # Этап 5 — здесь будет запуск транскрипции и LLM
        print(f"[app] process job {job_id} requested (Этап 5)")

    def _refresh_tray_jobs(self) -> None:
        with get_conn() as conn:
            pending_rows = conn.execute("""
                SELECT j.id, s.title, s.started_at
                FROM jobs j JOIN sessions s ON s.id = j.session_id
                WHERE j.status = 'pending'
                ORDER BY j.created_at DESC
            """).fetchall()

            done_rows = conn.execute("""
                SELECT j.id, s.title, s.started_at
                FROM jobs j JOIN sessions s ON s.id = j.session_id
                WHERE j.status = 'done'
                ORDER BY j.updated_at DESC
                LIMIT 20
            """).fetchall()

        to_dict = lambda r: {"id": r["id"], "title": r["title"], "started_at": r["started_at"]}
        self._tray.set_jobs(
            pending=[to_dict(r) for r in pending_rows],
            done=[to_dict(r) for r in done_rows],
        )

    # ── Выход ─────────────────────────────────────────────────────────────────

    def _on_quit(self) -> None:
        if self._capture and self._capture.is_recording:
            self._capture.stop()
        self._root.after(0, self._root.quit)


def main() -> None:
    App().run()


if __name__ == "__main__":
    main()
