"""
Модальное окно статуса обработки задания.

Показывается при запуске пайплайна, обновляется по мере прохождения этапов,
закрывается автоматически после завершения. Нельзя закрыть вручную.
"""
from __future__ import annotations

import tkinter as tk
import tkinter.ttk as ttk
from typing import Optional

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_STAGE_LABELS: dict[str, str] = {
    "transcribing": "Транскрипция",
    "diarizing":    "Диаризация спикеров...",
    "aligning":     "Выравнивание...",
    "analysis":     "Смысловой анализ (LLM)...",
    "followup":     "Follow-up (LLM)...",
    "done":         "Готово ✓",
    "error":        "Ошибка",
}


def _fmt(sec: float) -> str:
    """Секунды → MM:SS."""
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


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
        self._detail_var: Optional[tk.StringVar] = None
        self._spinner_var: Optional[tk.StringVar] = None
        self._progress_var: Optional[tk.DoubleVar] = None
        self._progressbar: Optional[ttk.Progressbar] = None
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

        w, h = 380, 130
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

        # Строка: "Транскрипция  25:44 / 38:12  ⠹"
        row = tk.Frame(frame, bg="#1a1a2e")
        row.pack(fill="x", pady=(8, 0))

        self._stage_var = tk.StringVar(value="Запуск...")
        tk.Label(
            row, textvariable=self._stage_var,
            bg="#1a1a2e", fg="#ffffff",
            font=("Segoe UI", 11, "bold"),
            anchor="w",
        ).pack(side="left")

        self._detail_var = tk.StringVar(value="")
        tk.Label(
            row, textvariable=self._detail_var,
            bg="#1a1a2e", fg="#aaaacc",
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(side="left", padx=(8, 0))

        self._spinner_var = tk.StringVar(value="")
        tk.Label(
            row, textvariable=self._spinner_var,
            bg="#1a1a2e", fg="#5566cc",
            font=("Segoe UI", 13),
            anchor="e",
        ).pack(side="right")

        # Прогресс-бар
        style = ttk.Style(win)
        style.theme_use("default")
        style.configure(
            "meets.Horizontal.TProgressbar",
            troughcolor="#2a2a4e",
            background="#5566cc",
            bordercolor="#1a1a2e",
            lightcolor="#5566cc",
            darkcolor="#4455bb",
        )
        self._progress_var = tk.DoubleVar(value=0.0)
        self._progressbar = ttk.Progressbar(
            frame,
            variable=self._progress_var,
            maximum=100.0,
            mode="indeterminate",
            style="meets.Horizontal.TProgressbar",
            length=344,
        )
        self._progressbar.pack(fill="x", pady=(6, 0))
        self._progressbar.start(50)  # indeterminate по умолчанию

        self._win = win
        self._spinning = True
        self._tick()

    def _update(self, stage: str, detail: str) -> None:
        if not self._win or not self._win.winfo_exists():
            return

        label = _STAGE_LABELS.get(stage, stage)
        self._stage_var.set(label)
        self._spinning = stage not in ("done", "error")

        if stage == "transcribing" and "/" in detail:
            # detail = "1544/2292" — секунды
            try:
                cur, total = detail.split("/")
                cur_f, total_f = float(cur), float(total)
                pct = min(cur_f / total_f * 100, 100.0) if total_f > 0 else 0.0

                self._detail_var.set(f"{_fmt(cur_f)} / {_fmt(total_f)}")
                self._progress_var.set(pct)

                # Переключаем в determinate режим при первом реальном прогрессе
                if self._progressbar and self._progressbar["mode"] == "indeterminate":
                    self._progressbar.stop()
                    self._progressbar.configure(mode="determinate")
            except ValueError:
                pass
        elif stage in ("done", "error"):
            self._detail_var.set(detail if detail else "")
            if self._progressbar:
                self._progressbar.stop()
                self._progressbar.configure(mode="determinate")
                self._progress_var.set(100.0 if stage == "done" else 0.0)
            self._spinner_var.set("")
        else:
            self._detail_var.set("")
            # Возвращаем indeterminate для стадий без прогресса
            if self._progressbar and self._progressbar["mode"] == "determinate":
                self._progressbar.configure(mode="indeterminate")
                self._progressbar.start(50)

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
