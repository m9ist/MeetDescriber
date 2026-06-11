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
import json
import logging
import queue
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
log.info("=== app start ===")

# Подавляем DEBUG-спам от numba/httpcore — захламляет лог
for _noisy in ("numba", "httpcore", "httpx", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)  # первая запись сразу при старте

from app.storage.db import init_db, get_conn, update_session, update_job_paths
from app.storage.file_manager import rename_session_docs
from app.capture.audio_capture import AudioCapture
from app.extension.native_host import BRIDGE_HOST, BRIDGE_PORT
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
        self._current_tab_id: Optional[int] = None  # Meet-вкладка активной записи (для meet_title)
        # На Mac: PyObjC callbacks не могут безопасно вызывать tkinter напрямую.
        # Все tkinter-операции передаём через эту очередь и выполняем в main loop.
        self._mac_queue: queue.SimpleQueue = queue.SimpleQueue()
        self._running: bool = True  # флаг выхода для ручного event loop на Mac

        # Tkinter root — главный поток
        self._root = tk.Tk()
        self._root.withdraw()
        self._root.title("for_meets")
        self._root.report_callback_exception = self._on_tk_error
        notifications.set_root(self._root)
        notifications.set_schedule(self._schedule)
        dialogs.set_schedule(self._schedule)

        self._spectrum = SpectrumWidget(self._root, schedule_fn=self._schedule)

        # Tray
        self._tray = tray_module.ForMeetsTray(
            on_start_manual=self._on_start_manual,
            on_stop=self._on_stop_manual,
            on_process_job=self._on_process_job,
            on_quit=self._on_quit,
            on_edit_job=self._on_edit_job,
            on_delete_job=self._on_delete_job,
            on_delete_all_pending=self._on_delete_all_pending,
            on_dismiss_job=self._on_dismiss_job,
            on_open_meetings_window=self._on_open_meetings_window,
        )

        # События от Chrome-расширения приходят через TCP-мост: Chrome запускает
        # standalone-хост (app/extension/native_host.py → for_meets_host.exe),
        # который пересылает meet_started/meet_ended/tabs на 127.0.0.1:BRIDGE_PORT.
        self._bridge_handlers: dict = {
            "meet_started": self._handle_meet_started,
            "meet_ended": self._handle_meet_ended,
            "meet_title": self._handle_meet_title,
            "tabs": self._handle_tabs,
        }

    # ── Запуск ────────────────────────────────────────────────────────────────

    def run(self) -> None:
        threading.Thread(
            target=self._run_bridge_server,
            daemon=True,
            name="ext-bridge",
        ).start()

        self._refresh_tray_jobs()

        if config.IS_MAC:
            # macOS: ручной event loop — единственный безопасный способ совместить
            # tkinter (Aqua/Tk) и pystray (PyObjC/NSStatusItem).
            # root.mainloop() отпускает GIL в C-коде Tk; PyObjC callback из NSMenu
            # в этот момент получает GIL released → SIGABRT.
            # Решение: дренируем NSApp и tkinter по очереди, GIL всегда у нас.
            import AppKit
            import Foundation
            self._tray.start_for_mac()
            ns_app = AppKit.NSApplication.sharedApplication()
            ns_app.finishLaunching()
            while self._running:
                # Обрабатываем NSApp события (меню, иконка)
                event = ns_app.nextEventMatchingMask_untilDate_inMode_dequeue_(
                    AppKit.NSUIntegerMax,
                    Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.01),
                    AppKit.NSDefaultRunLoopMode,
                    True,
                )
                if event:
                    ns_app.sendEvent_(event)
                # Дренируем очередь от PyObjC callbacks → выполняем в main thread
                while not self._mac_queue.empty():
                    try:
                        fn = self._mac_queue.get_nowait()
                        fn()
                    except Exception:
                        log.error("mac_queue callback error", exc_info=True)
                # Обрабатываем tkinter события
                try:
                    self._root.update()
                except tk.TclError:
                    break  # root был уничтожен → выход
            # Корректное завершение: убираем иконку, затем жёсткий выход.
            # ns_app.terminate_() и sys.exit() вызывают зачистку daemon-потоков
            # что приводит к crash-репорту macOS. os._exit() завершает немедленно.
            try:
                self._tray.stop()
            except Exception:
                pass
            import os as _os
            _os._exit(0)
        else:
            # Windows: tkinter занимает main thread, pystray — фоновый поток.
            self._tray.start()
            self._root.mainloop()

    # ── Мост с Chrome-расширением ─────────────────────────────────────────────

    def _run_bridge_server(self) -> None:
        """TCP-сервер для событий от native-host'а (запущенного Chrome'ом).

        Протокол: JSON-объект на строку. Хост переподключается сам при обрыве.
        """
        import socket

        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((BRIDGE_HOST, BRIDGE_PORT))
        except OSError:
            # Порт занят — вероятно второй экземпляр приложения. Автостарт
            # будет работать только в первом; логируем и не падаем.
            log.error("bridge: port %d busy — autostart from Chrome disabled "
                      "in this instance", BRIDGE_PORT, exc_info=True)
            return
        srv.listen(2)
        log.info("bridge: listening on %s:%d", BRIDGE_HOST, BRIDGE_PORT)
        while True:
            try:
                conn, addr = srv.accept()
            except OSError:
                log.error("bridge: accept failed", exc_info=True)
                return
            log.info("bridge: host connected from %s", addr)
            threading.Thread(
                target=self._serve_bridge_conn,
                args=(conn,),
                daemon=True,
                name="ext-bridge-conn",
            ).start()

    def _serve_bridge_conn(self, conn) -> None:
        buf = b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if not line.strip():
                        continue
                    try:
                        msg = json.loads(line.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError):
                        log.warning("bridge: bad message %r", line[:200])
                        continue
                    handler = self._bridge_handlers.get(msg.get("type"))
                    if handler:
                        try:
                            handler(msg)
                        except Exception:
                            log.error("bridge: handler %s failed", msg.get("type"),
                                      exc_info=True)
        log.info("bridge: host disconnected")

    # ── Обработчики Chrome ────────────────────────────────────────────────────

    @staticmethod
    def _clean_meet_title(title: str) -> str:
        """Убирает служебный префикс из заголовка Meet-вкладки.

        "Meet – Weekly Sync" → "Weekly Sync", "Meet – abc-defg-hij" → "abc-defg-hij".
        """
        t = (title or "").strip()
        for prefix in ("Meet – ", "Meet — ", "Meet - "):
            if t.startswith(prefix):
                t = t[len(prefix):].strip()
                break
        return t

    def _handle_meet_started(self, msg: dict) -> None:
        tab_id = msg.get("tab_id")
        title = self._clean_meet_title(msg.get("title", "")) or "Google Meet"
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
            # Запоминаем вкладку: на момент старта заголовок обычно ещё «Meet» —
            # настоящее название приедет следом сообщением meet_title.
            self._current_tab_id = tab_id

        self._schedule(show)

    def _handle_meet_ended(self, msg: dict) -> None:
        tab_id = msg.get("tab_id")
        self._skip_tab_ids.discard(tab_id)
        self._current_tab_id = None
        self._schedule(self._stop_and_offer_processing)

    def _handle_meet_title(self, msg: dict) -> None:
        """Meet выставляет настоящее название вкладки уже после старта записи —
        обновляем название активной сессии вдогонку."""
        tab_id = msg.get("tab_id")
        title = self._clean_meet_title(msg.get("title", ""))
        if not title or title in ("Meet", "Google Meet"):
            return
        if tab_id != self._current_tab_id or self._current_session_id is None:
            return
        if not (self._capture and self._capture.is_recording):
            return
        if title == self._current_title:
            return

        session_id = self._current_session_id
        log.info("meet title update: %r -> %r (session %d)",
                 self._current_title, title, session_id)
        self._current_title = title
        with get_conn() as conn:
            conn.execute(
                "UPDATE sessions SET title=? WHERE id=?",
                (title, session_id),
            )
        self._schedule(lambda: self._tray.set_recording(True, title))

    def _handle_tabs(self, msg: dict) -> None:
        self._latest_tabs = msg.get("tabs", [])

    # ── Ручной запуск ─────────────────────────────────────────────────────────

    def _on_start_manual(self) -> None:
        def show():
            result = dialogs.ask_meeting_info(self._root)
            if result:
                self._start_session(
                    title=result["title"],
                    agenda=result["agenda"],
                    source="manual",
                    device_index=None,
                )
        self._schedule(show)

    def _on_stop_manual(self) -> None:
        log.info("_on_stop_manual called")
        self._schedule(self._stop_and_offer_processing)

    def _schedule(self, fn, delay_ms: int = 0) -> None:
        """Thread-safe планировщик для tkinter-операций с опциональной задержкой.

        На Mac: всё идёт через _mac_queue, который дренируется main loop'ом.
        root.after() небезопасен — Tk регистрирует _runBackgroundLoop, который
        может выстрелить во время NSMenuTrackingSession и сломать GIL state.
        Для delay_ms>0 используем threading.Timer → put в очередь.
        На Windows: root.after(delay_ms, fn) — стандартный путь.
        """
        if config.IS_MAC:
            if delay_ms > 0:
                t = threading.Timer(delay_ms / 1000.0, lambda: self._mac_queue.put(fn))
                t.daemon = True
                t.start()
            else:
                self._mac_queue.put(fn)
        else:
            self._root.after(delay_ms, fn)

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
        self._capture.on_error = lambda e: log.error("capture error: %s", e, exc_info=e)
        self._capture.on_audio_frame = self._spectrum.push_frame
        self._capture.on_drift_warning = lambda d: notifications.mic_drift_warning(d)
        self._capture.start(device_index=device_index)

        # Обновляем формат в спектре после старта (rate/channels известны из потока)
        def _update_spectrum_fmt():
            if self._capture:
                self._spectrum.set_format(self._capture._rate, self._capture._channels)
        self._schedule(_update_spectrum_fmt, 300)

        self._spectrum.show()
        self._tray.set_recording(True, self._current_title)

    def _stop_and_offer_processing(self) -> None:
        log.info("_stop_and_offer_processing called")
        if not self._capture or not self._capture.is_recording:
            log.info("no capture active, returning")
            return

        log.info("stopping capture...")
        self._capture.stop()
        self._capture = None
        self._current_tab_id = None
        log.info("capture stopped")
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
            self._schedule(
                lambda: ClaudeManualDialog(
                    self._root, stage, prompt_path, cli, result_q,
                    chat_prompt=chat_prompt, output_path=output_path,
                )
            )
            return result_q.get(timeout=1800)  # 30 мин

        return ask_claude

    def _on_process_job(self, job_id: int) -> None:
        """Запускает пайплайн транскрипции в фоновом потоке."""
        import threading
        from app.processing.pipeline import run_transcription, PipelineCancelledError

        with get_conn() as conn:
            row = conn.execute(
                "SELECT s.title FROM jobs j JOIN sessions s ON s.id=j.session_id WHERE j.id=?",
                (job_id,),
            ).fetchone()
        title = row["title"] if row else "Встреча"

        cancel_event = threading.Event()
        modal = ProcessingStatusWindow(self._root, title, schedule_fn=self._schedule,
                                       cancel_event=cancel_event)
        modal.show()
        ask_claude = self._make_ask_claude()

        def run():
            try:
                path = run_transcription(job_id, on_progress=modal.update,
                                         ask_claude=ask_claude, cancel_event=cancel_event)
                log.info("job %d done → %s", job_id, path)
            except PipelineCancelledError:
                log.info("job %d cancelled by user", job_id)
            except Exception as e:
                log.error("job %d error: %s", job_id, e, exc_info=True)
                modal.update("error", str(e)[:60])
            finally:
                modal.close()
                self._schedule(self._refresh_tray_jobs)

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

        self._schedule(show)

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
                    "SELECT id FROM jobs WHERE status IN ('pending', 'transcribed', 'analyzed')"
                ).fetchall()
            ]
            for job_id in job_ids:
                self._delete_job_files(conn, job_id)
        self._refresh_tray_jobs()

    def _on_dismiss_job(self, job_id: int) -> None:
        """Скрывает задание из «Необработанных» — устанавливает dismissed=1."""
        from app.storage.meetings_repo import set_dismissed
        set_dismissed(job_id, True)
        log.info("dismissed job %d", job_id)
        self._refresh_tray_jobs()

    def _on_open_meetings_window(self) -> None:
        from app.ui.meetings_window import MeetingsWindow
        self._schedule(lambda: MeetingsWindow(
            self._root,
            on_data_changed=self._refresh_tray_jobs,
        ))

    def _refresh_tray_jobs(self) -> None:
        with get_conn() as conn:
            pending_rows = conn.execute("""
                SELECT j.id, j.status, j.transcription_path, j.analysis_path,
                       s.title, s.started_at
                FROM jobs j JOIN sessions s ON s.id = j.session_id
                WHERE j.status IN ('pending', 'transcribed', 'analyzed') AND j.dismissed = 0
                ORDER BY j.created_at DESC
            """).fetchall()

            done_rows = conn.execute("""
                SELECT j.id, j.status, j.session_id, j.transcription_path, j.analysis_path, j.followup_path,
                       s.title, s.started_at
                FROM jobs j JOIN sessions s ON s.id = j.session_id
                WHERE j.status = 'done' OR j.dismissed = 1
                ORDER BY j.updated_at DESC
                LIMIT 20
            """).fetchall()

        pending_to_dict = lambda r: {
            "id": r["id"],
            "status": r["status"],
            "title": r["title"],
            "started_at": r["started_at"],
            "transcription_path": r["transcription_path"],
            "analysis_path": r["analysis_path"],
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
        self._running = False
        self._schedule(self._root.quit)


def main() -> None:
    try:
        App().run()
    except Exception:
        log.critical("Unhandled exception:\n%s", traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
