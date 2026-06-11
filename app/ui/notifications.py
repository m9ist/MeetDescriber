"""
Уведомления и всплывающие окна.

Все методы потокобезопасны — можно вызывать из любого потока.
Показ выполняется через root.after() в главном потоке.
"""
import tkinter as tk
from tkinter import font as tkfont
from typing import Callable, Optional

import config
from app.ui.user_actions import log_action

_root: Optional[tk.Tk] = None
_schedule_app: Optional[Callable] = None  # App._schedule(fn, delay_ms=0)


def set_root(root: tk.Tk) -> None:
    """Привязывает к главному tk.Tk() экземпляру."""
    global _root
    _root = root


def set_schedule(schedule_fn: Callable) -> None:
    """Устанавливает thread-safe планировщик App._schedule(fn, delay_ms=0).

    На Mac обязательно — без него используется root.after, который небезопасен
    при NSMenu-tracking (SIGABRT _Py_FatalError_TstateNULL).
    """
    global _schedule_app
    _schedule_app = schedule_fn


def _schedule(fn: Callable, delay_ms: int = 0) -> None:
    if _schedule_app:
        _schedule_app(fn, delay_ms)
    elif _root:
        _root.after(delay_ms, fn)


def _btn_kwargs(bg: str, fg: str, bold: bool = False) -> dict:
    """Стиль кнопки. На Mac Aqua-тема игнорирует bg/fg/relief — используем дефолт."""
    if config.IS_MAC:
        return {
            "font": (config.UI_FONT, 11, "bold") if bold else (config.UI_FONT, 11),
            "cursor": "hand2",
        }
    return {
        "bg": bg, "fg": fg,
        "relief": "flat", "padx": 10, "pady": 4,
        "cursor": "hand2",
        "font": (config.UI_FONT, 9, "bold") if bold else (config.UI_FONT, 9),
    }


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
        log_action("notification_skip_recording", title=meeting_title)
        on_skip()
        win.destroy()

    tk.Button(
        frame, text="Не записывать эту встречу",
        command=skip,
        **_btn_kwargs(bg="#444444", fg="#ffffff"),
    ).pack(anchor="w")

    def _auto_dismiss():
        try:
            if win.winfo_exists():
                win.destroy()
        except tk.TclError:
            pass
    _schedule(_auto_dismiss, 9000)


# ── Алерт: дрейф микрофона ───────────────────────────────────────────────────

def mic_drift_warning(drift_sec: float) -> None:
    """Показывает toast «Микрофон лагает». Срабатывает из audio_capture когда
    mic_stream.read() блокируется существенно дольше реального времени."""
    _schedule(lambda: _show_mic_drift(drift_sec))


def _show_mic_drift(drift_sec: float) -> None:
    win = tk.Toplevel(_root)
    win.title("")
    win.resizable(False, False)
    win.attributes("-topmost", True)
    win.overrideredirect(True)

    _position_bottom_right(win, w=320, h=80, offset_y=130)

    frame = tk.Frame(win, bg="#3a1f1f", padx=14, pady=10)
    frame.pack(fill="both", expand=True)

    tk.Label(
        frame, text="⚠ Микрофон лагает",
        bg="#3a1f1f", fg="#ff8888",
        font=(config.UI_FONT, 11, "bold"),
        anchor="w",
    ).pack(fill="x")

    tk.Label(
        frame,
        text=f"Mic отстаёт от системного звука на {abs(drift_sec):.0f}с.\n"
             f"Возможно, mic захватило другое приложение.",
        bg="#3a1f1f", fg="#cc9999",
        font=(config.UI_FONT, 9),
        anchor="w", justify="left",
    ).pack(fill="x", pady=(4, 0))

    def _auto_dismiss():
        try:
            if win.winfo_exists():
                win.destroy()
        except tk.TclError:
            pass
    _schedule(_auto_dismiss, 8000)


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
        log_action("notification_process_now", title=session_title)
        on_process()
        win.destroy()

    def do_later():
        log_action("notification_process_later", title=session_title)
        on_later()
        win.destroy()

    tk.Button(
        btn_frame, text="Обработать сейчас",
        command=do_process,
        **_btn_kwargs(bg="#2d6a2d", fg="#ffffff", bold=True),
    ).pack(side="left", padx=(0, 6))

    tk.Button(
        btn_frame, text="Позже",
        command=do_later,
        **_btn_kwargs(bg="#444444", fg="#cccccc"),
    ).pack(side="left")

    def _auto_dismiss_process_later():
        try:
            if win.winfo_exists():
                do_later()
        except tk.TclError:
            pass
    _schedule(_auto_dismiss_process_later, 30000)


# ── Утилиты ──────────────────────────────────────────────────────────────────

# Правый нижний угол занят спектр-виджетом (SpectrumWidget, 300x70,
# y = sh-130..sh-60) — тосты всегда поднимаем выше его зоны, иначе
# REC-оверлей ложится на кнопки уведомлений.
_SPECTRUM_CLEARANCE = 80  # высота спектра (70) + зазор (10)


def _position_bottom_right(win: tk.Toplevel, w: int, h: int, offset_y: int = 0) -> None:
    win.update_idletasks()
    sw = win.winfo_screenwidth()
    sh = win.winfo_screenheight()
    x = sw - w - 20
    y = sh - h - 60 - _SPECTRUM_CLEARANCE - offset_y
    win.geometry(f"{w}x{h}+{x}+{y}")
