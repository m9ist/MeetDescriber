"""
Пауза / продолжение / остановка ML-worker'ов (диаризация, транскрипция).

Worker'ы — отдельные subprocess'ы, изнутри не прерываются. Пауза замораживает
процесс целиком (NtSuspendProcess на Windows, SIGSTOP на POSIX): CPU/GPU он больше
не грузит, но VRAM и RAM остаются занятыми до продолжения или остановки.
Остановка убивает процесс: terminate → kill по таймауту.

Один ProcessControl на запуск пайплайна: backend регистрирует в нём свой Popen
через `with control.attached(proc)`, UI зовёт pause/resume/stop из главного потока.
"""
from __future__ import annotations

import logging
import subprocess
import threading
from contextlib import contextmanager
from typing import Iterator, Optional

import config

log = logging.getLogger("app")

STOP_KILL_TIMEOUT_SEC = 5.0


class PipelineCancelledError(Exception):
    pass


def _suspend(proc: subprocess.Popen) -> None:
    if config.IS_WINDOWS:
        _nt_call("NtSuspendProcess", proc.pid)
    else:
        import signal
        proc.send_signal(signal.SIGSTOP)


def _resume(proc: subprocess.Popen) -> None:
    if config.IS_WINDOWS:
        _nt_call("NtResumeProcess", proc.pid)
    else:
        import signal
        proc.send_signal(signal.SIGCONT)


def _nt_call(fn_name: str, pid: int) -> None:
    import ctypes
    PROCESS_SUSPEND_RESUME = 0x0800
    kernel32 = ctypes.windll.kernel32
    h = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not h:
        raise OSError(f"OpenProcess({pid}) failed: {ctypes.GetLastError()}")
    try:
        status = getattr(ctypes.windll.ntdll, fn_name)(h)
        if status != 0:
            raise OSError(f"{fn_name}({pid}) NTSTATUS=0x{status & 0xFFFFFFFF:08X}")
    finally:
        kernel32.CloseHandle(h)


class ProcessControl:
    """Потокобезопасно: pause/resume/stop из UI, attach/detach из потока пайплайна."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._paused = False
        self._cancel = threading.Event()

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def check_cancel(self) -> None:
        if self._cancel.is_set():
            raise PipelineCancelledError()

    # ── Из потока пайплайна ───────────────────────────────────────────────────

    @contextmanager
    def attached(self, proc: subprocess.Popen) -> Iterator[None]:
        """Регистрирует запущенный worker на время блока. Применяет паузу/стоп,
        если их нажали между этапами, пока worker'а не было."""
        with self._lock:
            self._proc = proc
            if self._cancel.is_set():
                self._kill_locked()
            elif self._paused:
                self._suspend_locked()
        try:
            yield
        finally:
            with self._lock:
                self._proc = None

    # ── Из UI ─────────────────────────────────────────────────────────────────

    def pause(self) -> None:
        with self._lock:
            if self._paused or self._cancel.is_set():
                return
            self._paused = True
            if self._proc is not None:
                self._suspend_locked()

    def resume(self) -> None:
        with self._lock:
            if not self._paused:
                return
            self._paused = False
            if self._proc is not None and self._proc.poll() is None:
                try:
                    _resume(self._proc)
                    log.info("worker pid=%d resumed", self._proc.pid)
                except OSError:
                    log.error("не удалось продолжить worker pid=%d", self._proc.pid, exc_info=True)

    def stop(self) -> None:
        """Ставит флаг отмены и убивает текущий worker. Не блокирует дольше
        STOP_KILL_TIMEOUT_SEC; вызывать можно из главного потока."""
        with self._lock:
            self._cancel.set()
            if self._proc is not None:
                self._kill_locked()

    # ── Внутреннее (под self._lock) ───────────────────────────────────────────

    def _suspend_locked(self) -> None:
        proc = self._proc
        if proc.poll() is not None:
            return
        try:
            _suspend(proc)
            log.info("worker pid=%d suspended", proc.pid)
        except OSError:
            log.error("не удалось приостановить worker pid=%d", proc.pid, exc_info=True)

    def _kill_locked(self) -> None:
        proc = self._proc
        if proc.poll() is not None:
            return
        # Остановленный SIGSTOP процесс не обработает SIGTERM, пока его не продолжат.
        # На Windows TerminateProcess работает и по замороженному, resume безвреден.
        if self._paused:
            try:
                _resume(proc)
            except OSError:
                log.warning("resume перед остановкой не удался pid=%d", proc.pid, exc_info=True)
            self._paused = False
        proc.terminate()
        try:
            proc.wait(timeout=STOP_KILL_TIMEOUT_SEC)
        except subprocess.TimeoutExpired:
            log.warning("worker pid=%d не завершился за %.0f с — kill", proc.pid, STOP_KILL_TIMEOUT_SEC)
            proc.kill()
        log.info("worker pid=%d stopped by user", proc.pid)
