"""
Виджет визуализации аудио-спектра во время записи.

Маленькое плавающее окно без рамки (300×75px), показывает FFT-спектр
в реальном времени (20 fps). Цвет баров: зелёный → жёлтый → красный.

Использование:
    widget = SpectrumWidget(root)
    capture.on_audio_frame = widget.push_frame
    widget.show()
    ...
    widget.hide()
"""
from __future__ import annotations

import logging
import struct
import threading
import tkinter as tk
import traceback
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)

N_BARS = 40
WIDTH = 300
BAR_H = 52
LABEL_H = 18
HEIGHT = BAR_H + LABEL_H
PAD = 5
UPDATE_MS = 50          # 20 fps
DECAY = 0.78            # плавное затухание баров
BUFFER_FRAMES = 2048    # кол-во PCM-фреймов в кольцевом буфере


class SpectrumWidget:
    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        self._win: Optional[tk.Toplevel] = None
        self._canvas: Optional[tk.Canvas] = None

        self._lock = threading.Lock()
        self._buf = bytearray()   # кольцевой буфер PCM int16
        self._rate = 48000
        self._channels = 2

        self._smoothed = [0.0] * N_BARS

    # ── Публичный API ────────────────────────────────────────────────────────

    def show(self) -> None:
        self._root.after(0, self._create_window)

    def hide(self) -> None:
        self._root.after(0, self._destroy_window)

    def push_frame(self, raw: bytes) -> None:
        """Вызывается из потока захвата на каждый read (512 фреймов)."""
        with self._lock:
            self._buf.extend(raw)
            # Храним не больше BUFFER_FRAMES фреймов (int16 * channels → 2*ch байт/фрейм)
            max_bytes = BUFFER_FRAMES * self._channels * 2
            if len(self._buf) > max_bytes:
                self._buf = self._buf[-max_bytes:]

    def set_format(self, rate: int, channels: int) -> None:
        self._rate = rate
        self._channels = channels

    # ── Окно ────────────────────────────────────────────────────────────────

    def _create_window(self) -> None:
        if self._win and self._win.winfo_exists():
            return

        win = tk.Toplevel(self._root)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.attributes("-alpha", 0.88)
        win.configure(bg="#111111")

        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = sw - WIDTH - 20
        y = sh - HEIGHT - 60   # чуть выше панели задач
        win.geometry(f"{WIDTH}x{HEIGHT}+{x}+{y}")

        canvas = tk.Canvas(win, width=WIDTH, height=HEIGHT,
                           bg="#111111", highlightthickness=0)
        canvas.pack()

        self._win = win
        self._canvas = canvas
        self._smoothed = [0.0] * N_BARS
        self._tick()

    def _destroy_window(self) -> None:
        if self._win and self._win.winfo_exists():
            self._win.destroy()
        self._win = None
        self._canvas = None

    # ── Анимация ────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        if not self._win or not self._win.winfo_exists():
            return
        try:
            bars = self._compute_bars()
            for i in range(N_BARS):
                self._smoothed[i] = max(bars[i], self._smoothed[i] * DECAY)
            self._draw(self._smoothed)
        except Exception:
            log.error("spectrum _tick error:\n%s", traceback.format_exc())
        self._root.after(UPDATE_MS, self._tick)

    def _compute_bars(self) -> list[float]:
        with self._lock:
            data = bytes(self._buf)

        ch = self._channels
        count = len(data) // (2 * ch)
        if count < 64:
            return [0.0] * N_BARS

        # Распаковываем только первый канал
        samples = struct.unpack(f"<{count * ch}h", data)
        mono = np.array(samples[::ch], dtype=np.float32) / 32768.0

        # Берём последние BUFFER_FRAMES семплов
        n = BUFFER_FRAMES
        if len(mono) < n:
            mono = np.pad(mono, (n - len(mono), 0))
        else:
            mono = mono[-n:]

        # Окно Ханна + FFT
        window = np.hanning(n)
        spectrum = np.abs(np.fft.rfft(mono * window))

        # Частотный диапазон: 80 Гц – 8000 Гц (речь + музыка)
        freq_res = self._rate / n
        lo_bin = max(1, int(80 / freq_res))
        hi_bin = min(len(spectrum), int(8000 / freq_res))
        spectrum = spectrum[lo_bin:hi_bin]
        if len(spectrum) == 0:
            return [0.0] * N_BARS

        # Логарифмическое разбиение на N_BARS бинов
        log_lo = np.log10(max(lo_bin, 1))
        log_hi = np.log10(hi_bin)
        edges = np.logspace(log_lo, log_hi, N_BARS + 1, base=10)
        total = len(spectrum)

        bars: list[float] = []
        for i in range(N_BARS):
            a = int(edges[i] - lo_bin)
            b = int(edges[i + 1] - lo_bin) + 1
            a = max(0, min(a, total))
            b = max(a + 1, min(b, total))
            val = float(np.mean(np.abs(spectrum[a:b])))
            bars.append(val)

        # Нормализация (скользящий пик через smoothed даст стабильность)
        peak = max(bars) if max(bars) > 0 else 1.0
        return [min(1.0, v / peak) for v in bars]

    def _draw(self, bars: list[float]) -> None:
        c = self._canvas
        c.delete("all")

        # Фон
        c.create_rectangle(0, 0, WIDTH, HEIGHT, fill="#111111", outline="")

        # REC-точка
        c.create_oval(5, 5, 13, 13, fill="#ff2222", outline="")
        c.create_text(17, 9, text="REC", fill="#ff5555",
                      anchor="w", font=("Segoe UI", 7, "bold"))

        # Бары
        usable_w = WIDTH - PAD * 2
        bar_slot = usable_w / N_BARS

        for i, v in enumerate(bars):
            x0 = PAD + i * bar_slot + 1
            x1 = PAD + (i + 1) * bar_slot - 1
            h = max(2, int(v * (BAR_H - 4)))
            y_bot = HEIGHT - 2
            y_top = y_bot - h

            # Цвет: зелёный → жёлтый → красный
            if v < 0.5:
                r = int(v * 2 * 220)
                g = 210
            else:
                r = 220
                g = int((1.0 - (v - 0.5) * 2) * 210)
            b = 30
            color = f"#{r:02x}{g:02x}{b:02x}"

            c.create_rectangle(x0, y_top, x1, y_bot, fill=color, outline="")

        # Тонкая рамка
        c.create_rectangle(0, 0, WIDTH - 1, HEIGHT - 1,
                           outline="#333333", width=1)
