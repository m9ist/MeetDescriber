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
        show_source_selector: bool = False,
        audio_sources: Optional[list] = None,
        browser_tabs: Optional[list] = None,
    ) -> None:
        self.ok = False
        self.title_value = default_title
        self.agenda_value = ""
        self.selected_source: Optional[dict] = None

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
        tk.Entry(win, textvariable=self._title_var, width=45).pack(fill="x", **pad)

        # Агенда
        tk.Label(win, text="Агенда (необязательно):", anchor="w").pack(fill="x", **pad)
        self._agenda_text = scrolledtext.ScrolledText(win, height=4, width=45)
        self._agenda_text.pack(fill="x", **pad)

        # Выбор источника (только при ручном запуске)
        if show_source_selector:
            self._build_source_selector(win, audio_sources or [], browser_tabs or [])

        # Кнопки
        btn_frame = tk.Frame(win)
        btn_frame.pack(fill="x", padx=12, pady=(8, 12))

        tk.Button(
            btn_frame, text="Начать запись",
            command=self._on_ok,
            bg="#2d6a2d", fg="white",
            relief="flat", padx=14, pady=6,
            font=("Segoe UI", 9, "bold"),
        ).pack(side="right", padx=(6, 0))

        tk.Button(
            btn_frame, text="Отмена",
            command=win.destroy,
            relief="flat", padx=10, pady=6,
        ).pack(side="right")

        # Центрируем окно
        win.update_idletasks()
        sw = parent.winfo_screenwidth()
        sh = parent.winfo_screenheight()
        w = win.winfo_width()
        h = win.winfo_height()
        win.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

        parent.wait_window(win)

    def _build_source_selector(
        self,
        win: tk.Toplevel,
        audio_sources: list,
        browser_tabs: list,
    ) -> None:
        tk.Label(win, text="Источник звука:", anchor="w").pack(fill="x", padx=12, pady=(8, 2))

        self._source_var = tk.StringVar()
        self._source_map: dict[str, dict] = {}

        frame = tk.Frame(win, relief="sunken", bd=1)
        frame.pack(fill="x", padx=12, pady=(0, 4))

        listbox = tk.Listbox(frame, height=6, selectmode="single", activestyle="none")
        scrollbar = tk.Scrollbar(frame, orient="vertical", command=listbox.yview)
        listbox.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        listbox.pack(fill="both", expand=True)

        self._source_listbox = listbox

        # Loopback устройства — первыми, со звёздочкой
        first_loopback_idx = None
        for i, src in enumerate(audio_sources):
            if src.get("is_loopback"):
                label = f"🔊 {src['name']} ✓"
                listbox.insert("end", label)
                self._source_map[label] = {"type": "audio", **src}
                if first_loopback_idx is None:
                    first_loopback_idx = listbox.size() - 1

        # Остальные устройства
        for src in audio_sources:
            if not src.get("is_loopback"):
                label = f"🎤 {src['name']}"
                listbox.insert("end", label)
                self._source_map[label] = {"type": "audio", **src}

        for tab in browser_tabs:
            label = f"🌐 {tab.get('title') or tab.get('url', 'Вкладка')}"
            listbox.insert("end", label)
            self._source_map[label] = {"type": "tab", **tab}

        # Выбираем первый loopback по умолчанию
        default = first_loopback_idx if first_loopback_idx is not None else 0
        if listbox.size() > 0:
            listbox.select_set(default)
            listbox.see(default)

    def _on_ok(self) -> None:
        self.ok = True
        self.title_value = self._title_var.get().strip()
        self.agenda_value = self._agenda_text.get("1.0", "end").strip()

        if hasattr(self, "_source_listbox"):
            sel = self._source_listbox.curselection()
            if sel:
                label = self._source_listbox.get(sel[0])
                self.selected_source = self._source_map.get(label)

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

    def __init__(
        self,
        parent: tk.Tk,
        stage: str,
        prompt_path: Optional[Path],
        cli: str,
        result_queue: queue.Queue,
        chat_prompt: str = "",
    ) -> None:
        self._prompt_path = prompt_path
        self._cli = cli
        self._queue = result_queue
        self._chat_prompt = chat_prompt  # готовый промпт для вставки в чат

        win = tk.Toplevel(parent)
        win.title(f"Claude CLI недоступен — {stage}")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", self._on_skip)
        self._win = win

        pad = {"padx": 14, "pady": 4}

        tk.Label(
            win,
            text=f"Не удалось запустить claude для этапа «{stage}».",
            font=("Segoe UI", 10, "bold"),
        ).pack(fill="x", **pad)

        if prompt_path:
            tk.Label(
                win,
                text=f"Промпт сохранён:\n{prompt_path}",
                anchor="w",
                justify="left",
                wraplength=460,
                fg="#555",
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

        self._run_btn = tk.Button(
            btn_frame,
            text="Запустить",
            command=self._on_run,
            bg="#2d6a2d", fg="white",
            relief="flat", padx=12, pady=6,
            font=("Segoe UI", 9, "bold"),
        )
        self._run_btn.pack(side="left", padx=(0, 6))

        tk.Button(
            btn_frame,
            text="Скопировать команду",
            command=self._on_copy_cmd,
            relief="flat", padx=10, pady=6,
        ).pack(side="left", padx=(0, 6))

        tk.Button(
            btn_frame,
            text="Скопировать промпт",
            command=self._on_copy_prompt,
            relief="flat", padx=10, pady=6,
        ).pack(side="left")

        # Кнопка закрыть
        tk.Button(
            win,
            text="Пропустить этот этап",
            command=self._on_skip,
            relief="flat", padx=10, pady=4,
            fg="#888",
        ).pack(anchor="e", padx=14, pady=(0, 12))

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
                            [self._cli, "-p", "-"],
                            stdin=fh,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            timeout=300,
                        )
                finally:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                if r.returncode == 0:
                    text = r.stdout.decode("utf-8", errors="replace").strip()
                    self._queue.put(text)
                    self._win.after(0, self._win.destroy)
                else:
                    err = r.stderr.decode("utf-8", errors="replace")[:200]
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
        cmd = f'"{cli_unix}" -p - < "{prompt_unix}"'
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

    def _on_skip(self) -> None:
        self._queue.put(None)
        self._win.destroy()

    def _set_status(self, msg: str, error: bool = False) -> None:
        self._status_var.set(msg)
        self._status_lbl.config(fg="#c0392b" if error else "#2d6a2d")


def ask_meeting_info(
    parent: tk.Tk,
    default_title: str = "",
    show_source_selector: bool = False,
    audio_sources: Optional[list] = None,
    browser_tabs: Optional[list] = None,
) -> Optional[dict]:
    """
    Показывает диалог и возвращает dict или None если отменено.

    Returns:
        {"title": str, "agenda": str, "source": dict | None} | None
    """
    d = MeetingStartDialog(
        parent,
        default_title=default_title,
        show_source_selector=show_source_selector,
        audio_sources=audio_sources,
        browser_tabs=browser_tabs,
    )
    if not d.ok:
        return None
    return {
        "title": d.title_value,
        "agenda": d.agenda_value,
        "source": d.selected_source,
    }
