"""
Уведомления и всплывающие окна.

Все методы потокобезопасны — можно вызывать из любого потока.
Показ выполняется через root.after() в главном потоке.
"""
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable, Optional

import config

_root: Optional[tk.Tk] = None


def set_root(root: tk.Tk) -> None:
    """Привязывает к главному tk.Tk() экземпляру."""
    global _root
    _root = root


def _schedule(fn: Callable) -> None:
    if _root:
        _root.after(0, fn)


# ── Запись началась ─────────────────────────────────────────────────────────

def recording_started(meeting_title: str, on_skip: Callable[[], None]) -> None:
    _schedule(lambda: _show_recording_started(meeting_title, on_skip))


def _show_recording_started(meeting_title: str, on_skip: Callable[[], None]) -> None:
    win = tk.Toplevel(_root)
    win.title("for_meets")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    win.overrideredirect(True)

    _position_bottom_right(win, w=320, h=110)

    frame = tk.Frame(win, bg="#2b2b2b", padx=14, pady=10)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame, text="⏺  Запись началась",
        bg="#2b2b2b", fg="#ffffff",
        font=(config.UI_FONT, 11, "bold"),
        anchor="w",
    ).pack(fill="x")

    tk.Label(
        frame, text=meeting_title or "Google Meet",
        bg="#2b2b2b", fg="#aaaaaa",
        font=(config.UI_FONT, 9),
        anchor="w",
    ).pack(fill="x", pady=(2, 8))

    def skip():
        on_skip()
        win.destroy()

    tk.Button(
        frame, text="Не записывать эту встречу",
        command=skip,
        bg="#444444", fg="#ffffff",
        relief="flat", padx=8, pady=4,
        cursor="hand2",
        font=(config.UI_FONT, 9),
    ).pack(anchor="w")

    win.after(9000, lambda: win.destroy() if win.winfo_exists() else None)


# ── Предупреждение о качестве ────────────────────────────────────────────────

def quality_warning(chunk_idx: int, score: float) -> None:
    _schedule(lambda: _show_quality_toast(chunk_idx, score))


def _show_quality_toast(chunk_idx: int, score: float) -> None:
    win = tk.Toplevel(_root)
    win.title("")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    win.overrideredirect(True)

    _position_bottom_right(win, w=280, h=60, offset_y=130)

    frame = tk.Frame(win, bg="#3a2a00", padx=12, pady=8)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame,
        text=f"⚠  Плохо слышно (последние ~30 сек)",
        bg="#3a2a00", fg="#ffcc44",
        font=(config.UI_FONT, 9),
        anchor="w",
    ).pack(fill="x")

    tk.Label(
        frame, text=f"Уверенность: {score:.0%}",
        bg="#3a2a00", fg="#888866",
        font=(config.UI_FONT, 8),
        anchor="w",
    ).pack(fill="x")

    win.after(5000, lambda: win.destroy() if win.winfo_exists() else None)


# ── Обработать сейчас? ───────────────────────────────────────────────────────

def process_now(
    session_title: str,
    on_process: Callable[[], None],
    on_later: Callable[[], None],
) -> None:
    _schedule(lambda: _show_process_now(session_title, on_process, on_later))


def _show_process_now(
    session_title: str,
    on_process: Callable[[], None],
    on_later: Callable[[], None],
) -> None:
    win = tk.Toplevel(_root)
    win.title("for_meets")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    win.overrideredirect(True)

    _position_bottom_right(win, w=320, h=120)

    frame = tk.Frame(win, bg="#1e2a1e", padx=14, pady=10)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame, text="✓  Встреча завершена",
        bg="#1e2a1e", fg="#ffffff",
        font=(config.UI_FONT, 11, "bold"),
        anchor="w",
    ).pack(fill="x")

    tk.Label(
        frame, text=session_title or "Встреча",
        bg="#1e2a1e", fg="#aaaaaa",
        font=(config.UI_FONT, 9),
        anchor="w",
    ).pack(fill="x", pady=(2, 8))

    btn_frame = tk.Frame(frame, bg="#1e2a1e")
    btn_frame.pack(fill="x")

    def do_process():
        on_process()
        win.destroy()

    def do_later():
        on_later()
        win.destroy()

    tk.Button(
        btn_frame, text="Обработать сейчас",
        command=do_process,
        bg="#2d6a2d", fg="#ffffff",
        relief="flat", padx=10, pady=4,
        cursor="hand2",
        font=(config.UI_FONT, 9, "bold"),
    ).pack(side="left", padx=(0, 6))

    tk.Button(
        btn_frame, text="Позже",
        command=do_later,
        bg="#444444", fg="#cccccc",
        relief="flat", padx=10, pady=4,
        cursor="hand2",
        font=(config.UI_FONT, 9),
    ).pack(side="left")

    win.after(30000, lambda: (do_later(), None) if win.winfo_exists() else None)


# ── Утилиты ──────────────────────────────────────────────────────────────────

def _position_bottom_right(win: tk.Toplevel, w: int, h: int, offset_y: int = 20) -> None:
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = sw - w - 20
    y = sh - h - 60 - offset_y
    win.geometry(f"{w}x{h}+{x}+{y}")
