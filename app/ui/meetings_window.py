"""
Окно управления совещаниями — «Все совещания».

Немодальное Toplevel-окно с таблицей всех сессий,
поиском/фильтром, контекстным меню и hover-tooltip.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Optional
import tkinter as tk
from tkinter import messagebox, ttk

import config
from app.storage import meetings_repo
from app.storage.db import get_conn
from app.ui.user_actions import log_action

log = logging.getLogger(__name__)

STATUS_RU = {
    "pending": "Ожидает",
    "processing": "Обработка",
    "transcribed": "Транскрибировано",
    "analyzed": "Проанализировано",
    "done": "Готово",
    "error": "Ошибка",
}

STATUS_FILTER_MAP = {
    "Все": "all",
    "Ожидает": "pending",
    "Транскрибировано": "transcribed",
    "Проанализировано": "analyzed",
    "Готово": "done",
    "Ошибка": "error",
}


def _fmt_duration(sec) -> str:
    if sec is None:
        return "—"
    sec = int(sec)
    if sec < 3600:
        m, s = divmod(sec, 60)
        return f"{m:02d}:{s:02d}"
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}"


def _fmt_stage_duration(sec) -> str:
    """Форматирует длительность этапа как 'M мин S с' или 'S с'."""
    if sec is None:
        return "—"
    sec = int(sec)
    if sec >= 60:
        m, s = divmod(sec, 60)
        return f"{m} мин {s} с"
    return f"{sec} с"


def _fmt_date(started_at: Optional[str]) -> str:
    if not started_at:
        return "—"
    # 2026-05-06T10:59:00 → 2026-05-06 10:59
    s = started_at[:16].replace("T", " ")
    return s


class MeetingsWindow:
    """Немодальное окно со списком всех совещаний."""

    def __init__(self, parent: tk.Tk, on_data_changed=None) -> None:
        """on_data_changed — коллбэк для обновления трея после изменений
        (delete/restart/edit). Если None — трей не обновляется."""
        self._parent = parent
        self._on_data_changed = on_data_changed

        win = tk.Toplevel(parent)
        win.title("for_meets — Все совещания")
        win.geometry("900x600")
        win.minsize(700, 400)
        self._win = win

        # Центрируем
        win.update_idletasks()
        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        win.geometry(f"+{(sw - 900) // 2}+{(sh - 600) // 2}")

        self._meetings: list[dict] = []

        # Tooltip state
        self._tooltip_win: Optional[tk.Toplevel] = None
        self._tooltip_after_id: Optional[str] = None
        self._tooltip_row_id: Optional[str] = None

        self._build_ui()
        self._reload_meetings()

        win.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        win = self._win

        # ── Шапка: поиск + фильтр ─────────────────────────────────────────────
        header = tk.Frame(win)
        header.pack(fill="x", padx=8, pady=(8, 4))

        tk.Label(header, text="Поиск:").pack(side="left")
        self._search_var = tk.StringVar()
        self._search_var.trace_add("write", lambda *_: self._reload_meetings())
        tk.Entry(header, textvariable=self._search_var, width=30).pack(
            side="left", padx=(4, 12)
        )

        tk.Label(header, text="Статус:").pack(side="left")
        self._filter_var = tk.StringVar(value="Все")
        status_cb = ttk.Combobox(
            header,
            textvariable=self._filter_var,
            values=list(STATUS_FILTER_MAP.keys()),
            state="readonly",
            width=18,
        )
        status_cb.pack(side="left", padx=(4, 0))
        status_cb.bind("<<ComboboxSelected>>", lambda _: self._reload_meetings())

        # ── Таблица ───────────────────────────────────────────────────────────
        table_frame = tk.Frame(win)
        table_frame.pack(fill="both", expand=True, padx=8, pady=4)

        columns = ("date", "title", "duration", "status", "audio")
        self._tree = ttk.Treeview(
            table_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self._tree.heading("date", text="Дата")
        self._tree.heading("title", text="Название")
        self._tree.heading("duration", text="Длительность")
        self._tree.heading("status", text="Статус")
        self._tree.heading("audio", text="Аудио")

        self._tree.column("date", width=130, minwidth=120, stretch=False)
        self._tree.column("title", width=370, minwidth=150, stretch=True)
        self._tree.column("duration", width=100, minwidth=80, stretch=False)
        self._tree.column("status", width=150, minwidth=100, stretch=False)
        self._tree.column("audio", width=55, minwidth=50, stretch=False, anchor="center")

        vsb = ttk.Scrollbar(table_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self._tree.pack(fill="both", expand=True)

        # Контекстное меню
        self._tree.bind("<Button-3>", self._on_right_click)

        # Tooltip
        self._tree.bind("<Motion>", self._on_motion)
        self._tree.bind("<Leave>", self._on_leave)

        # ── Низ: статистика + кнопка ──────────────────────────────────────────
        bottom = tk.Frame(win)
        bottom.pack(fill="x", padx=8, pady=(4, 8))

        self._stats_var = tk.StringVar(value="")
        tk.Label(bottom, textvariable=self._stats_var, anchor="w").pack(
            side="left", fill="x", expand=True
        )

        _btn_kw = {"padx": 8, "pady": 4} if config.IS_WINDOWS else {"padx": 8}
        tk.Button(
            bottom,
            text="Удалить аудио старше 14 дней",
            command=self._on_delete_old_audio,
            **_btn_kw,
        ).pack(side="right")

    # ── Данные ────────────────────────────────────────────────────────────────

    def _reload_meetings(self) -> None:
        search = self._search_var.get().strip()
        status_key = self._filter_var.get()
        status_filter = STATUS_FILTER_MAP.get(status_key, "all")

        self._meetings = meetings_repo.list_all_meetings(
            search=search, status_filter=status_filter
        )
        self._populate_table()
        self._update_stats()

    def _notify_data_changed(self) -> None:
        """Вызывается после действий, меняющих БД — чтобы обновить трей."""
        if self._on_data_changed:
            try:
                self._on_data_changed()
            except Exception:
                log.exception("on_data_changed callback failed")

    def _populate_table(self) -> None:
        self._tree.delete(*self._tree.get_children())
        for m in self._meetings:
            session_id = m.get("session_id")
            date_str = _fmt_date(m.get("started_at"))
            title = m.get("title") or "Без названия"
            duration = _fmt_duration(m.get("duration_sec"))
            status = STATUS_RU.get(m.get("status") or "", m.get("status") or "—")
            audio_dir = config.RECORDINGS_DIR / f"session_{session_id}"
            audio = "✓" if audio_dir.exists() and any(audio_dir.iterdir()) else ""
            self._tree.insert(
                "",
                "end",
                iid=str(session_id),
                values=(date_str, title, duration, status, audio),
            )

    def _update_stats(self) -> None:
        try:
            stats = meetings_repo.get_stats()
            n = stats.get("meetings_count", 0)
            rec_bytes = stats.get("recordings_size_bytes", 0)
            doc_bytes = stats.get("documents_size_bytes", 0)
            gb = rec_bytes / (1024 ** 3)
            mb = doc_bytes / (1024 ** 2)
            self._stats_var.set(
                f"Всего: {n} совещаний · {gb:.1f} GB записей · {mb:.0f} MB документов"
            )
        except Exception:
            log.exception("_update_stats failed")

    # ── Контекстное меню ──────────────────────────────────────────────────────

    def _on_right_click(self, event: tk.Event) -> None:
        row_id = self._tree.identify_row(event.y)
        if not row_id:
            return
        self._tree.selection_set(row_id)
        session_id = int(row_id)
        meeting = self._find_meeting(session_id)
        if not meeting:
            return
        self._show_context_menu(event, session_id, meeting)

    def _find_meeting(self, session_id: int) -> Optional[dict]:
        for m in self._meetings:
            if m.get("session_id") == session_id:
                return m
        return None

    def _show_context_menu(
        self, event: tk.Event, session_id: int, meeting: dict
    ) -> None:
        tr = meeting.get("transcription_path")
        an = meeting.get("analysis_path")
        fu = meeting.get("followup_path")
        status = meeting.get("status") or ""
        title = meeting.get("title") or "Без названия"
        job_id = meeting.get("job_id")

        def state(path) -> str:
            return "normal" if path and Path(path).exists() else "disabled"

        audio_dir = config.RECORDINGS_DIR / f"session_{session_id}"
        has_audio = audio_dir.exists() and any(audio_dir.iterdir())

        menu = tk.Menu(self._win, tearoff=0)
        menu.add_command(
            label="Открыть транскрипцию",
            state=state(tr),
            command=lambda: self._open_file(tr),
        )
        menu.add_command(
            label="Открыть анализ",
            state=state(an),
            command=lambda: self._open_file(an),
        )
        menu.add_command(
            label="Открыть follow-up",
            state=state(fu),
            command=lambda: self._open_file(fu),
        )
        menu.add_separator()
        menu.add_command(
            label="Редактировать название и агенду",
            command=lambda: self._edit_meeting(session_id, meeting),
        )
        menu.add_separator()
        menu.add_command(
            label="Перезапустить транскрипцию",
            state="normal" if has_audio else "disabled",
            command=lambda: self._restart_stage(job_id, "transcription"),
        )
        menu.add_command(
            label="Перезапустить анализ",
            command=lambda: self._restart_stage(job_id, "analysis"),
        )
        menu.add_command(
            label="Перезапустить follow-up",
            command=lambda: self._restart_stage(job_id, "followup"),
        )
        menu.add_separator()
        can_delete_audio = has_audio and status in ("transcribed", "analyzed", "done")
        menu.add_command(
            label="Удалить аудио",
            state="normal" if can_delete_audio else "disabled",
            command=lambda: self._delete_audio(session_id),
        )
        menu.add_command(
            label="Удалить совещание полностью",
            command=lambda: self._delete_meeting(session_id, title),
        )

        menu.tk_popup(event.x_root, event.y_root)

    # ── Действия контекстного меню ────────────────────────────────────────────

    def _open_file(self, path: Optional[str]) -> None:
        if not path:
            return
        log_action("meetings_open_file", path=path)
        try:
            if config.IS_WINDOWS:
                os.startfile(path)
            else:
                import subprocess
                subprocess.run(["open", path], check=False)
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось открыть файл:\n{e}", parent=self._win)

    def _edit_meeting(self, session_id: int, meeting: dict) -> None:
        from app.ui.dialogs import ask_edit_meeting_info

        log_action("meetings_edit_meeting", session_id=session_id,
                   title=meeting.get("title"))
        result = ask_edit_meeting_info(
            self._win,
            title=meeting.get("title") or "",
            agenda=meeting.get("agenda") or "",
        )
        if result is None:
            log_action("meetings_edit_meeting_cancel", session_id=session_id)
            return
        try:
            with get_conn() as conn:
                conn.execute(
                    "UPDATE sessions SET title=?, agenda=? WHERE id=?",
                    (result["title"], result["agenda"], session_id),
                )
        except Exception as e:
            messagebox.showerror("Ошибка", f"Не удалось сохранить:\n{e}", parent=self._win)
            return
        self._reload_meetings()
        self._notify_data_changed()

    def _restart_stage(self, job_id: Optional[int], stage: str) -> None:
        log_action("meetings_restart_stage", job_id=job_id, stage=stage)
        if job_id is None:
            messagebox.showwarning("Нет задания", "Задание не найдено в БД.", parent=self._win)
            return

        stage_labels = {
            "transcription": ("Перезапустить транскрипцию",
                              "Будут удалены все документы и сброшен статус. Продолжить?"),
            "analysis": ("Перезапустить анализ",
                         "Будут удалены анализ и follow-up, статус сброшен до «транскрибировано». Продолжить?"),
            "followup": ("Перезапустить follow-up",
                         "Будет удалён follow-up, статус сброшен до «проанализировано». Продолжить?"),
        }
        caption, question = stage_labels.get(stage, ("Перезапустить?", "Продолжить?"))

        if not messagebox.askyesno(caption, question, parent=self._win):
            log_action("meetings_restart_stage_cancel", job_id=job_id, stage=stage)
            return
        log_action("meetings_restart_stage_confirm", job_id=job_id, stage=stage)
        try:
            meetings_repo.reset_to_stage(job_id, stage)
        except Exception as e:
            messagebox.showerror("Ошибка", str(e), parent=self._win)
            return
        self._reload_meetings()
        self._notify_data_changed()

    def _delete_audio(self, session_id: int) -> None:
        log_action("meetings_delete_audio_click", session_id=session_id)
        if not messagebox.askyesno(
            "Удалить аудио",
            "Удалить аудиозаписи этого совещания?\nДокументы останутся.",
            parent=self._win,
        ):
            log_action("meetings_delete_audio_cancel", session_id=session_id)
            return
        log_action("meetings_delete_audio_confirm", session_id=session_id)
        try:
            meetings_repo.delete_audio(session_id)
        except Exception as e:
            messagebox.showerror("Ошибка", str(e), parent=self._win)
            return
        self._reload_meetings()
        self._notify_data_changed()

    def _delete_meeting(self, session_id: int, title: str) -> None:
        log_action("meetings_delete_meeting_click", session_id=session_id, title=title)
        if not messagebox.askyesno(
            "Удалить совещание",
            f'Удалить совещание "{title}"?\n'
            "Будут стёрты документы и аудиозаписи без возможности восстановления.",
            parent=self._win,
        ):
            log_action("meetings_delete_meeting_cancel", session_id=session_id)
            return
        log_action("meetings_delete_meeting_confirm", session_id=session_id, title=title)
        try:
            meetings_repo.delete_meeting(session_id)
        except Exception as e:
            messagebox.showerror("Ошибка", str(e), parent=self._win)
            return
        self._reload_meetings()
        self._notify_data_changed()

    # ── Кнопка удаления старого аудио ────────────────────────────────────────

    def _on_delete_old_audio(self) -> None:
        log_action("meetings_delete_old_audio_click")
        try:
            count, total_bytes = meetings_repo.count_old_audio(14)
        except Exception as e:
            messagebox.showerror("Ошибка", str(e), parent=self._win)
            return

        if count == 0:
            messagebox.showinfo(
                "Нечего удалять",
                "Не найдено папок с аудио старше 14 дней.",
                parent=self._win,
            )
            return

        gb = total_bytes / (1024 ** 3)
        if not messagebox.askyesno(
            "Удаление",
            f"Найдено {count} папок аудио ({gb:.1f} GB). Удалить?",
            parent=self._win,
        ):
            log_action("meetings_delete_old_audio_cancel", count=count)
            return

        log_action("meetings_delete_old_audio_confirm", count=count, gb=round(gb, 1))
        try:
            deleted = meetings_repo.delete_old_audio(14)
        except Exception as e:
            messagebox.showerror("Ошибка", str(e), parent=self._win)
            return

        messagebox.showinfo("Готово", f"Удалено {deleted} папок.", parent=self._win)
        self._reload_meetings()
        self._notify_data_changed()

    # ── Tooltip ───────────────────────────────────────────────────────────────

    def _on_motion(self, event: tk.Event) -> None:
        row_id = self._tree.identify_row(event.y)
        if not row_id:
            self._cancel_tooltip()
            self._hide_tooltip()
            return

        if row_id == self._tooltip_row_id:
            return  # курсор остался на той же строке

        self._cancel_tooltip()
        self._hide_tooltip()
        self._tooltip_row_id = row_id
        # Сохраняем координаты для позиционирования tooltip
        self._tooltip_x = event.x_root
        self._tooltip_y = event.y_root
        self._tooltip_after_id = self._win.after(500, lambda: self._show_tooltip(row_id))

    def _on_leave(self, event: tk.Event) -> None:
        self._cancel_tooltip()
        self._hide_tooltip()
        self._tooltip_row_id = None

    def _cancel_tooltip(self) -> None:
        if self._tooltip_after_id is not None:
            self._win.after_cancel(self._tooltip_after_id)
            self._tooltip_after_id = None

    def _hide_tooltip(self) -> None:
        if self._tooltip_win is not None:
            try:
                self._tooltip_win.destroy()
            except Exception:
                pass
            self._tooltip_win = None

    def _show_tooltip(self, row_id: str) -> None:
        self._tooltip_after_id = None
        session_id = int(row_id)
        meeting = self._find_meeting(session_id)
        if not meeting:
            return

        # Строим текст
        agenda = meeting.get("agenda") or "—"
        tr = meeting.get("transcription_path") or "—"
        an = meeting.get("analysis_path") or "—"
        fu = meeting.get("followup_path") or "—"

        transcribe_dur = _fmt_stage_duration(meeting.get("transcribe_duration_sec"))
        diarize_dur = _fmt_stage_duration(meeting.get("diarize_duration_sec"))
        analyze_dur = _fmt_stage_duration(meeting.get("analyze_duration_sec"))
        followup_dur = _fmt_stage_duration(meeting.get("followup_duration_sec"))

        lines = [
            "Агенда:",
            f"  {agenda}",
            "",
            "Файлы:",
            f"  Транскрипция: {tr}",
            f"  Анализ:       {an}",
            f"  Follow-up:    {fu}",
            "",
            "Длительности этапов:",
            f"  Транскрипция: {transcribe_dur}",
            f"  Диаризация:   {diarize_dur}",
            f"  Анализ:       {analyze_dur}",
            f"  Follow-up:    {followup_dur}",
        ]

        error = meeting.get("error")
        if error:
            lines.extend(["", f"Ошибка:", f"  {error}"])

        text = "\n".join(lines)

        tip = tk.Toplevel(self._win)
        tip.overrideredirect(True)
        tip.attributes("-topmost", True)
        self._tooltip_win = tip

        lbl = tk.Label(
            tip,
            text=text,
            justify="left",
            anchor="nw",
            background="#ffffdd",
            relief="solid",
            borderwidth=1,
            font=(config.UI_FONT, 8),
            padx=6,
            pady=4,
        )
        lbl.pack()

        tip.update_idletasks()
        tw = tip.winfo_width()
        th = tip.winfo_height()
        sw = self._win.winfo_screenwidth()
        sh = self._win.winfo_screenheight()

        x = self._tooltip_x + 16
        y = self._tooltip_y + 16
        if x + tw > sw:
            x = sw - tw - 4
        if y + th > sh:
            y = self._tooltip_y - th - 4

        tip.geometry(f"+{x}+{y}")

    # ── Закрытие ──────────────────────────────────────────────────────────────

    def _on_close(self) -> None:
        log_action("meetings_window_close")
        self._cancel_tooltip()
        self._hide_tooltip()
        self._win.destroy()
