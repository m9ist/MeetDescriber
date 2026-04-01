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
import faulthandler
import logging
import threading
import tkinter as tk
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import config

_LOG_PATH = config.ROOT_DIR / "app.log"

# faulthandler пишет напрямую через OS write() — работает даже при SIGABRT.
# Направляем в app.log чтобы трейсбек был там, а не только в терминале.
_fault_log = open(_LOG_PATH, "a", buffering=1)
faulthandler.enable(file=_fault_log)


class _FlushFileHandler(logging.FileHandler):
    """FileHandler с немедленным flush — критично когда процесс падает с SIGABRT."""
    def emit(self, record):
        super().emit(record)
        self.flush()


logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    handlers=[
        _FlushFileHandler(_LOG_PATH, encoding="utf-8"),
        logging.StreamHandler(sys.stderr),
    ],
)
log = logging.getLogger("app")
log.info("=== app start ===")  # первая запись сразу при старте

from app.storage.db import init_db, get_conn, update_session, update_job_paths
from app.storage.file_manager import rename_session_docs
from app.capture.audio_capture import AudioCapture, list_audio_sources
from app.extension.native_host import NativeHost, read_message, send_message
from app.ui import notifications, tray as tray_module, dialogs
from app.ui.spectrum import SpectrumWidget
from app.ui.status_window import ProcessingStatusWindow


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
        self._root.report_callback_exception = self._on_tk_error
        notifications.set_root(self._root)

        self._spectrum = SpectrumWidget(self._root)

        # Tray
        self._tray = tray_module.ForMeetsTray(
            on_start_manual=self._on_start_manual,
            on_stop=self._on_stop_manual,
            on_process_job=self._on_process_job,
            on_quit=self._on_quit,
            on_edit_job=self._on_edit_job,
            on_delete_job=self._on_delete_job,
            on_delete_all_pending=self._on_delete_all_pending,
        )

        # Native host thread
        self._host = NativeHost()
        self._host.on("meet_started", self._handle_meet_started)
        self._host.on("meet_ended", self._handle_meet_ended)
        self._host.on("tabs", self._handle_tabs)
        self._host.on("ping", lambda msg: {"type": "pong"})

    # ── Запуск ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        threading.Thread(
            target=self._host.run,
            daemon=True,
            name="native-host",
        ).start()

        self._refresh_tray_jobs()

        if config.IS_MAC:
            # macOS: ни root.mainloop() ни pystray.run() не вызываем напрямую.
            # root.mainloop() отпускает GIL внутри Tk C-кода; когда AppKit
            # диспатчит NSMenu коллбэк через PyObjC — GIL уже released → SIGABRT.
            # Решение: ручной цикл на main thread:
            #   - NSApp.nextEvent(...) ждёт событие (до 10мс), не отпуская GIL надолго
            #   - sendEvent_() вызывает PyObjC-коллбэк с корректным GIL
            #   - root.update() обрабатывает очередь tkinter без блокировки
            import AppKit
            import Foundation
            self._tray.start_for_mac()
            ns_app = AppKit.NSApplication.sharedApplication()
            ns_app.finishLaunching()
            while True:
                event = ns_app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                    AppKit.NSUIntegerMax,
                    Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.01),
                    AppKit.NSDefaultRunLoopMode,
                    True,
                )
                if event:
                    ns_app.sendEvent_(event)
                try:
                    self._root.update()
                except tk.TclError:
                    break  # root был уничтожен → выход
        else:
            # Windows: tkinter занимает main thread, pystray — фоновый поток.
            self._tray.start()
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
        self._capture.on_error = lambda e: log.error("capture error: %s", e, exc_info=e)
        self._capture.on_audio_frame = self._spectrum.push_frame
        self._capture.start(device_index=device_index)

        # Обновляем формат в спектре после старта (rate/channels известны из потока)
        def _update_spectrum_fmt():
            if self._capture:
                self._spectrum.set_format(self._capture._rate, self._capture._channels)
        self._root.after(300, _update_spectrum_fmt)

        self._spectrum.show()
        self._tray.set_recording(True, self._current_title)

    def _stop_and_offer_processing(self) -> None:
        if not self._capture or not self._capture.is_recording:
            return

        self._capture.stop()
        self._capture = None
        self._spectrum.hide()

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

        # Создаём задание сразу (pending), берём его id
        self._create_job(session_id)
        self._refresh_tray_jobs()
        with get_conn() as conn:
            row = conn.execute(
                "SELECT id FROM jobs WHERE session_id=?", (session_id,)
            ).fetchone()
        job_id = row["id"] if row else None

        def process():
            if job_id is not None:
                self._on_process_job(job_id)

        def later():
            pass  # задание видно в трее, запустить можно оттуда

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

    def _make_ask_claude(self) -> "Callable":
        """Возвращает callback для диалога ручного запуска Claude."""
        import queue as _queue
        from app.ui.dialogs import ClaudeManualDialog

        def ask_claude(stage: str, prompt_path, cli: str,
                       chat_prompt: str = "", output_path=None):
            result_q = _queue.Queue()
            self._root.after(
                0,
                lambda: ClaudeManualDialog(
                    self._root, stage, prompt_path, cli, result_q,
                    chat_prompt=chat_prompt, output_path=output_path,
                ),
            )
            return result_q.get(timeout=1800)  # 30 мин

        return ask_claude

    def _on_process_job(self, job_id: int) -> None:
        """Запускает пайплайн транскрипции в фоновом потоке."""
        import threading
        from app.processing.pipeline import run_transcription

        with get_conn() as conn:
            row = conn.execute(
                "SELECT s.title FROM jobs j JOIN sessions s ON s.id=j.session_id WHERE j.id=?",
                (job_id,),
            ).fetchone()
        title = row["title"] if row else "Встреча"

        modal = ProcessingStatusWindow(self._root, title)
        modal.show()
        ask_claude = self._make_ask_claude()

        def run():
            try:
                path = run_transcription(job_id, on_progress=modal.update, ask_claude=ask_claude)
                log.info("job %d done → %s", job_id, path)
            except Exception as e:
                log.error("job %d error: %s", job_id, e, exc_info=True)
                modal.update("error", str(e)[:60])
            finally:
                modal.close()
                self._root.after(0, self._refresh_tray_jobs)

        threading.Thread(target=run, daemon=True, name=f"pipeline-{job_id}").start()

    def _on_edit_job(self, job_id: int) -> None:
        """Открывает диалог редактирования названия/агенды совещания."""
        def show():
            with get_conn() as conn:
                row = conn.execute(
                    """SELECT s.id, s.title, s.agenda, s.started_at
                       FROM jobs j JOIN sessions s ON s.id = j.session_id
                       WHERE j.id=?""",
                    (job_id,),
                ).fetchone()
            if not row:
                return
            session_id = row["id"]
            old_title = row["title"] or ""
            old_agenda = row["agenda"] or ""
            started_at = row["started_at"]

            from app.ui.dialogs import ask_edit_meeting_info
            result = ask_edit_meeting_info(
                self._root,
                title=old_title,
                agenda=old_agenda,
            )
            if result is None:
                return

            new_title = result["title"]
            new_agenda = result["agenda"]

            # Переименовываем файлы если изменилось название
            if new_title != old_title and new_title:
                new_paths = rename_session_docs(session_id, old_title, new_title, started_at)
                update_job_paths(session_id, new_paths)

            update_session(session_id, new_title or old_title, new_agenda)
            self._refresh_tray_jobs()

        self._root.after(0, show)

    def _delete_job_files(self, conn, job_id: int) -> None:
        """Удаляет файлы задания с диска и записи из БД (job + session)."""
        import shutil
        from app.storage.file_manager import get_doc_paths

        row = conn.execute(
            """SELECT j.session_id, j.transcription_path, j.analysis_path, j.followup_path,
                      s.title, s.started_at
               FROM jobs j JOIN sessions s ON s.id = j.session_id
               WHERE j.id=?""",
            (job_id,),
        ).fetchone()
        if not row:
            return

        # Удаляем документы из jobs-таблицы
        for col in ("transcription_path", "analysis_path", "followup_path"):
            p = row[col]
            if p:
                try:
                    Path(p).unlink(missing_ok=True)
                except OSError:
                    pass

        # Удаляем промпт-файлы (их нет в jobs, выводим из get_doc_paths)
        for key in ("analysis_prompt", "followup_prompt"):
            p = get_doc_paths(row["title"], row["started_at"]).get(key)
            if p:
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass

        # Удаляем папку с аудиочанками
        session_dir = config.RECORDINGS_DIR / f"session_{row['session_id']}"
        if session_dir.exists():
            try:
                shutil.rmtree(session_dir)
            except OSError:
                pass

        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))
        conn.execute("DELETE FROM sessions WHERE id=?", (row["session_id"],))

    def _on_delete_job(self, job_id: int) -> None:
        """Удаляет необработанное задание и все его файлы."""
        with get_conn() as conn:
            self._delete_job_files(conn, job_id)
        self._refresh_tray_jobs()

    def _on_delete_all_pending(self) -> None:
        """Удаляет все необработанные задания и их файлы."""
        with get_conn() as conn:
            job_ids = [
                r["id"] for r in conn.execute(
                    "SELECT id FROM jobs WHERE status IN ('pending', 'transcribed')"
                ).fetchall()
            ]
            for job_id in job_ids:
                self._delete_job_files(conn, job_id)
        self._refresh_tray_jobs()

    def _refresh_tray_jobs(self) -> None:
        with get_conn() as conn:
            pending_rows = conn.execute("""
                SELECT j.id, j.status, s.title, s.started_at
                FROM jobs j JOIN sessions s ON s.id = j.session_id
                WHERE j.status IN ('pending', 'transcribed')
                ORDER BY j.created_at DESC
            """).fetchall()

            done_rows = conn.execute("""
                SELECT j.id, j.status, j.session_id, j.transcription_path, j.analysis_path, j.followup_path,
                       s.title, s.started_at
                FROM jobs j JOIN sessions s ON s.id = j.session_id
                WHERE j.status = 'done'
                ORDER BY j.updated_at DESC
                LIMIT 20
            """).fetchall()

        pending_to_dict = lambda r: {
            "id": r["id"],
            "status": r["status"],
            "title": r["title"],
            "started_at": r["started_at"],
        }
        done_to_dict = lambda r: {
            "id": r["id"],
            "status": r["status"],
            "title": r["title"],
            "started_at": r["started_at"],
            "session_id": r["session_id"],
            "transcription_path": r["transcription_path"],
            "analysis_path": r["analysis_path"],
            "followup_path": r["followup_path"],
        }
        self._tray.set_jobs(
            pending=[pending_to_dict(r) for r in pending_rows],
            done=[done_to_dict(r) for r in done_rows],
        )

    # ── Выход ─────────────────────────────────────────────────────────────────

    def _on_tk_error(self, exc_type, exc_val, exc_tb) -> None:
        log.critical("Tkinter callback error:\n%s",
                     "".join(traceback.format_exception(exc_type, exc_val, exc_tb)))

    def _on_quit(self) -> None:
        if self._capture and self._capture.is_recording:
            self._capture.stop()
        self._root.after(0, self._root.quit)


def main() -> None:
    try:
        App().run()
    except Exception:
        log.critical("Unhandled exception:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
