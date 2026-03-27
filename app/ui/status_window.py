"""
Модальное окно статуса обработки задания.

Показывается при запуске пайплайна, обновляется по мере прохождения этапов,
закрывается автоматически после завершения. Нельзя закрыть вручную.
"""
from __future__ import annotations

import tkinter as tk
from typing import Optional

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_STAGE_LABELS: dict[str, str] = {
    "transcribing": "Транскрипция...",
    "diarizing":    "Диаризация спикеров...",
    "aligning":     "Выравнивание...",
    "analysis":     "Смысловой анализ (LLM)...",
    "followup":     "Follow-up (LLM)...",
    "done":         "Готово ✓",
    "error":        "Ошибка",
}


class ProcessingStatusWindow:
    """
    Потокобезопасное окно прогресса.
    Все методы можно вызывать из фонового потока — они маршалируются в UI через after().
    """

    def __init__(self, root: tk.Tk, title: str) -> None:
        self._root = root
        self._meeting_title = title
        self._win: Optional[tk.Toplevel] = None
        self._stage_var: Optional[tk.StringVar] = None
        self._spinner_var: Optional[tk.StringVar] = None
        self._spinner_idx = 0
        self._spinning = False

    # ── Публичный API ─────────────────────────────────────────────────────────

    def show(self) -> None:
        self._root.after(0, self._create)

    def update(self, stage: str, detail: str = "") -> None:
        self._root.after(0, lambda: self._update(stage, detail))

    def close(self) -> None:
        self._root.after(800, self._destroy)

    # ── Внутреннее ────────────────────────────────────────────────────────────

    def _create(self) -> None:
        if self._win and self._win.winfo_exists():
            return

        win = tk.Toplevel(self._root)
        win.title("for_meets — обработка")
        win.resizable(False, False)
        win.attributes("-topmost", True)
        win.protocol("WM_DELETE_WINDOW", lambda: None)  # нельзя закрыть вручную

        w, h = 360, 115
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

        frame = tk.Frame(win, bg="#1a1a2e", padx=18, pady=14)
        frame.pack(fill="both", expand=True)

        tk.Label(
            frame,
            text=f"Обработка: {self._meeting_title}",
            bg="#1a1a2e", fg="#7777aa",
            font=("Segoe UI", 9),
            anchor="w",
        ).pack(fill="x")

        self._stage_var = tk.StringVar(value="Запуск...")
        tk.Label(
            frame, textvariable=self._stage_var,
            bg="#1a1a2e", fg="#ffffff",
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        ).pack(fill="x", pady=(8, 2))

        self._spinner_var = tk.StringVar(value="")
        tk.Label(
            frame, textvariable=self._spinner_var,
            bg="#1a1a2e", fg="#5566cc",
            font=("Segoe UI", 13),
            anchor="w",
        ).pack(fill="x")

        self._win = win
        self._spinning = True
        self._tick()

    def _update(self, stage: str, detail: str) -> None:
        if not self._win or not self._win.winfo_exists():
            return
        label = _STAGE_LABELS.get(stage, stage)
        if detail:
            label = f"{label}  {detail}"
        self._stage_var.set(label)
        self._spinning = stage not in ("done", "error")
        if not self._spinning:
            self._spinner_var.set("")

    def _tick(self) -> None:
        if not self._win or not self._win.winfo_exists():
            return
        if self._spinning and self._spinner_var:
            self._spinner_var.set(SPINNER[self._spinner_idx % len(SPINNER)])
            self._spinner_idx += 1
        self._root.after(100, self._tick)

    def _destroy(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.destroy()
        self._win = None
