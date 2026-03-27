"""
Диалоговые окна.

- MeetingStartDialog: название совещания + агенда (+ выбор источника при ручном запуске)
- SourceSelectorDialog: выбор аудиоустройства или вкладки браузера
"""
import tkinter as tk
from tkinter import scrolledtext, ttk
from typing import Optional

import config


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
