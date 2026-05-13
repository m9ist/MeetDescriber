"""
Диалоговые окна.

- MeetingStartDialog: название совещания + агенда (+ выбор источника при ручном запуске)
- SourceSelectorDialog: выбор аудиоустройства или вкладки браузера
- ClaudeManualDialog: ручной запуск Claude когда CLI недоступен
"""
import logging
import os
import queue
import subprocess
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import scrolledtext, ttk
from typing import Optional

import config

log = logging.getLogger(__name__)


# Virtual Key Codes (Windows-стиль, но tk.event.keycode совпадает на Win + Linux).
# Используем для биндингов Ctrl+C/V/X/A независимо от раскладки клавиатуры —
# на русской раскладке `<Control-c>` не срабатывает (keysym = «с», не «c»).
_VK_A, _VK_C, _VK_V, _VK_X = 65, 67, 86, 88


def _bind_clipboard_shortcuts(widget: tk.Widget) -> None:
    """Биндит Ctrl+C/V/X/A на Entry/Text независимо от раскладки клавиатуры."""
    def handler(event: tk.Event):
        # state bit 0x4 = Control. На некоторых раскладках Ctrl приходит как mod1.
        if not (event.state & 0x4):
            return None
        kc = event.keycode
        if kc == _VK_C:
            widget.event_generate("<<Copy>>")
            return "break"
        if kc == _VK_V:
            widget.event_generate("<<Paste>>")
            return "break"
        if kc == _VK_X:
            widget.event_generate("<<Cut>>")
            return "break"
        if kc == _VK_A:
            if isinstance(widget, (tk.Entry, ttk.Entry)):
                widget.select_range(0, "end")
                widget.icursor("end")
            else:  # tk.Text / ScrolledText
                widget.tag_add("sel", "1.0", "end-1c")
                widget.mark_set("insert", "end-1c")
            return "break"
        return None
    widget.bind("<KeyPress>", handler)


class MeetingStartDialog:
    """
    Диалог заполнения метаданных перед записью.

    Результат:
        dialog.title   — строка или None
        dialog.agenda  — строка или None
        dialog.ok      — True если пользователь нажал OK
    """

    def __init__(
        self,
        parent: tk.Tk,
        default_title: str = "",
    ) -> None:
        self.ok = False
        self.title_value = default_title
        self.agenda_value = ""

        win = tk.Toplevel(parent)
        win.title("Начать запись")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.grab_set()

        self._win = win
        pad = {"padx": 12, "pady": 4}

        # Название
        tk.Label(win, text="Название совещания:", anchor="w").pack(fill="x", **pad)
        self._title_var = tk.StringVar(value=default_title)
        title_entry = tk.Entry(win, textvariable=self._title_var, width=45)
        title_entry.pack(fill="x", **pad)
        _bind_clipboard_shortcuts(title_entry)

        # Агенда
        tk.Label(win, text="Агенда (необязательно):", anchor="w").pack(fill="x", **pad)
        self._agenda_text = scrolledtext.ScrolledText(win, height=4, width=45)
        self._agenda_text.pack(fill="x", **pad)
        _bind_clipboard_shortcuts(self._agenda_text)

        # Кнопки
        btn_frame = tk.Frame(win)
        btn_frame.pack(fill="x", padx=12, pady=(8, 12))

        _btn_kw = {"padx": 10, "pady": 6} if config.IS_WINDOWS else {"padx": 10}

        tk.Button(
            btn_frame, text="Начать запись",
            command=self._on_ok,
            font=(config.UI_FONT, 9, "bold"),
            **_btn_kw,
        ).pack(side="right", padx=(6, 0))

        tk.Button(
            btn_frame, text="Отмена",
            command=win.destroy,
            **_btn_kw,
        ).pack(side="right")

        # Enter в поле названия = начать запись
        title_entry.bind("<Return>", lambda e: self._on_ok())

        # Центрируем окно
        win.update_idletasks()
        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        w = win.winfo_width()
        h = win.winfo_height()
        win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

        # Автофокус на поле названия + выделение default-текста,
        # чтобы можно было сразу набирать поверх. focus_force нужен потому
        # что окно с -topmost не всегда получает focus автоматически.
        title_entry.focus_force()
        if default_title:
            title_entry.select_range(0, "end")
            title_entry.icursor("end")

        parent.wait_window(win)

    def _on_ok(self) -> None:
        self.ok = True
        self.title_value = self._title_var.get().strip()
        self.agenda_value = self._agenda_text.get("1.0", "end").strip()
        self._win.destroy()


class ClaudeManualDialog:
    """
    Диалог ручного запуска Claude когда CLI недоступен.

    Показывает 3 кнопки:
    - Запустить — повторная попытка CLI прямо из диалога (в фоновом потоке)
    - Скопировать команду — cmd-строка для запуска из bash
    - Скопировать промпт — текст промпта для вставки в чат

    result_queue получает str (текст результата) или None (пользователь пропустил).
    """

    # Sentinel: файл записан вручную, pipeline пропускает запись
    STAGE_DONE = "__STAGE_DONE__"

    def __init__(
        self,
        parent: tk.Tk,
        stage: str,
        prompt_path: Optional[Path],
        cli: str,
        result_queue: queue.Queue,
        chat_prompt: str = "",
        output_path: Optional[Path] = None,
    ) -> None:
        self._stage = stage
        self._prompt_path = prompt_path
        self._cli = cli
        self._queue = result_queue
        self._chat_prompt = chat_prompt
        self._output_path = output_path

        win = tk.Toplevel(parent)
        win.title(f"for_meets — {stage}")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self._on_skip)
        self._win = win

        pad = {"padx": 14, "pady": 4}

        tk.Label(
            win,
            text=f"Этап: {stage}",
            font=(config.UI_FONT, 10, "bold"),
        ).pack(fill="x", **pad)

        tk.Label(
            win,
            text="Запустите Claude CLI командой ниже, дождитесь завершения\n"
                 "и нажмите «Этап выполнен». Или скопируйте промпт и выполните вручную.",
            anchor="w",
            justify="left",
            wraplength=460,
            fg="#555",
        ).pack(fill="x", **pad)

        if prompt_path:
            tk.Label(
                win,
                text=f"Промпт: {prompt_path}",
                anchor="w",
                justify="left",
                wraplength=460,
                fg="#888",
            ).pack(fill="x", **pad)

        # Статус (обратная связь после нажатия кнопок)
        self._status_var = tk.StringVar(value="")
        self._status_lbl = tk.Label(
            win, textvariable=self._status_var, fg="#2d6a2d", anchor="w"
        )
        self._status_lbl.pack(fill="x", padx=14, pady=(2, 0))

        # Кнопки действий
        btn_frame = tk.Frame(win)
        btn_frame.pack(fill="x", padx=14, pady=8)

        _btn_kw = {"padx": 10, "pady": 5} if config.IS_WINDOWS else {"padx": 10}

        self._run_btn = tk.Button(
            btn_frame,
            text="Запустить",
            command=self._on_run,
            font=(config.UI_FONT, 9, "bold"),
            **_btn_kw,
        )
        self._run_btn.pack(side="left", padx=(0, 6))

        tk.Button(
            btn_frame,
            text="Скопировать команду",
            command=self._on_copy_cmd,
            **_btn_kw,
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            btn_frame,
            text="Скопировать промпт",
            command=self._on_copy_prompt,
            **_btn_kw,
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            btn_frame,
            text="Открыть CMD",
            command=self._on_open_cmd,
            **_btn_kw,
        ).pack(side="left")

        # Кнопка "Этап выполнен" + "Пропустить"
        bottom_frame = tk.Frame(win)
        bottom_frame.pack(fill="x", padx=14, pady=(0, 12))

        tk.Button(
            bottom_frame,
            text="Этап выполнен",
            command=self._on_stage_done,
            font=(config.UI_FONT, 9, "bold"),
            **_btn_kw,
        ).pack(side="left")

        tk.Button(
            bottom_frame,
            text="Пропустить этот этап",
            command=self._on_skip,
            **_btn_kw,
        ).pack(side="right")

        # Центрируем
        win.update_idletasks()
        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        w = win.winfo_width()
        h = win.winfo_height()
        win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    def _on_run(self) -> None:
        if self._prompt_path is None:
            self._set_status("Промпт не найден", error=True)
            return
        self._run_btn.config(state="disabled")
        self._set_status("Запускаю claude, подождите...")

        def worker() -> None:
            try:
                with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as f:
                    f.write(self._prompt_path.read_bytes())
                    tmp = f.name
                try:
                    with open(tmp, "rb") as fh:
                        r = subprocess.run(
                            [self._cli, "-p", "-",
                             "--allowedTools", "Write", "Edit"],
                            stdin=fh,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            timeout=300,
                            cwd=str(config.ROOT_DIR),
                        )
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                if r.returncode == 0:
                    stdout = r.stdout.decode("utf-8", errors="replace").strip()
                    if stdout:
                        log.info("claude stdout [%s]:\n%s", self._stage, stdout)
                    # Claude записал файл сам через Write/Edit.
                    # Отправляем STAGE_DONE — pipeline не перезаписывает файл stdout-ом.
                    self._queue.put(self.STAGE_DONE)
                    self._win.after(0, self._win.destroy)
                else:
                    err = r.stderr.decode("utf-8", errors="replace")[:200]
                    log.error("claude failed [%s] rc=%d stderr: %s", self._stage, r.returncode, err)
                    self._win.after(0, lambda: self._run_failed(f"rc={r.returncode}: {err}"))
            except Exception as e:
                self._win.after(0, lambda: self._run_failed(str(e)))

        threading.Thread(target=worker, daemon=True).start()

    def _run_failed(self, msg: str) -> None:
        self._set_status(f"Ошибка: {msg}", error=True)
        self._run_btn.config(state="normal")

    def _on_copy_cmd(self) -> None:
        if self._prompt_path is None:
            self._set_status("Промпт не найден", error=True)
            return
        # Путь в формате Unix-слешей для bash/Git Bash
        prompt_unix = str(self._prompt_path).replace("\\", "/")
        cli_unix = self._cli.replace("\\", "/")
        root_unix = str(config.ROOT_DIR).replace("\\", "/")
        cmd = (
            f'cd "{root_unix}" && '
            f'"{cli_unix}" -p - --allowedTools Write Edit < "{prompt_unix}"'
        )
        self._win.clipboard_clear()
        self._win.clipboard_append(cmd)
        self._set_status("Команда скопирована в буфер обмена")

    def _on_copy_prompt(self) -> None:
        if not self._chat_prompt:
            self._set_status("Промпт не сформирован", error=True)
            return
        self._win.clipboard_clear()
        self._win.clipboard_append(self._chat_prompt)
        self._set_status("Промпт скопирован в буфер обмена")
        self._set_status("Промпт скопирован в буфер обмена")

    def _on_open_cmd(self) -> None:
        cwd = str(config.ROOT_DIR)
        try:
            if config.IS_WINDOWS:
                subprocess.Popen(["cmd.exe"], cwd=cwd,
                                 creationflags=subprocess.CREATE_NEW_CONSOLE)
            else:
                subprocess.Popen(["open", "-a", "Terminal", cwd])
        except Exception as e:
            self._set_status(f"Не удалось открыть терминал: {e}", error=True)

    def _on_stage_done(self) -> None:
        if self._output_path is None:
            self._set_status("Путь к выходному файлу не задан", error=True)
            return
        if not self._output_path.exists():
            self._set_status(f"Файл не найден: {self._output_path}", error=True)
            return
        self._queue.put(self.STAGE_DONE)
        self._win.destroy()

    def _on_skip(self) -> None:
        self._queue.put(None)
        self._win.destroy()

    def _set_status(self, msg: str, error: bool = False) -> None:
        self._status_var.set(msg)
        self._status_lbl.config(fg="#c0392b" if error else "#2d6a2d")


class MeetingEditDialog:
    """
    Диалог редактирования информации о совещании.
    Поля: название, агенда. При смене названия — файлы переименовываются.
    """

    def __init__(
        self,
        parent: tk.Tk,
        title: str = "",
        agenda: str = "",
    ) -> None:
        self.ok = False
        self.title_value = title
        self.agenda_value = agenda

        win = tk.Toplevel(parent)
        win.title("Информация о совещании")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.grab_set()
        self._win = win

        pad = {"padx": 12, "pady": 4}

        tk.Label(win, text="Название совещания:", anchor="w").pack(fill="x", **pad)
        self._title_var = tk.StringVar(value=title)
        title_entry = tk.Entry(win, textvariable=self._title_var, width=45)
        title_entry.pack(fill="x", **pad)
        _bind_clipboard_shortcuts(title_entry)

        tk.Label(win, text="Агенда (необязательно):", anchor="w").pack(fill="x", **pad)
        self._agenda_text = scrolledtext.ScrolledText(win, height=4, width=45)
        self._agenda_text.pack(fill="x", **pad)
        _bind_clipboard_shortcuts(self._agenda_text)
        if agenda:
            self._agenda_text.insert("1.0", agenda)

        btn_frame = tk.Frame(win)
        btn_frame.pack(fill="x", padx=12, pady=(8, 12))

        _btn_kw = {"padx": 10, "pady": 6} if config.IS_WINDOWS else {"padx": 10}

        tk.Button(
            btn_frame, text="Сохранить",
            command=self._on_ok,
            font=(config.UI_FONT, 9, "bold"),
            **_btn_kw,
        ).pack(side="right", padx=(6, 0))

        tk.Button(
            btn_frame, text="Отмена",
            command=win.destroy,
            **_btn_kw,
        ).pack(side="right")

        win.update_idletasks()
        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        w = win.winfo_width()
        h = win.winfo_height()
        win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

        title_entry.focus_force()
        if title:
            title_entry.select_range(0, "end")
            title_entry.icursor("end")

        parent.wait_window(win)

    def _on_ok(self) -> None:
        self.ok = True
        self.title_value = self._title_var.get().strip()
        self.agenda_value = self._agenda_text.get("1.0", "end").strip()
        self._win.destroy()


def ask_edit_meeting_info(
    parent: tk.Tk,
    title: str = "",
    agenda: str = "",
) -> Optional[dict]:
    """Показывает диалог редактирования и возвращает dict или None если отменено."""
    d = MeetingEditDialog(parent, title=title, agenda=agenda)
    if not d.ok:
        return None
    return {"title": d.title_value, "agenda": d.agenda_value}


def ask_meeting_info(parent: tk.Tk, default_title: str = "") -> Optional[dict]:
    """
    Показывает диалог и возвращает dict или None если отменено.

    Returns:
        {"title": str, "agenda": str} | None
    """
    d = MeetingStartDialog(parent, default_title=default_title)
    if not d.ok:
        return None
    return {"title": d.title_value, "agenda": d.agenda_value}
