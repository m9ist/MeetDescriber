"""
Mac-специфичные хелперы для tk.Toplevel.

Проблема: на macOS Tk регистрирует свой NSApp delegate (TKApplication). Когда
пользователь кликает в красный «X» окна, NSControlTrackMouse → NSWindow.close
→ TKApplication windowShouldClose → Tk WM_DELETE_WINDOW protocol → PythonCmd.
PythonCmd пытается восстановить GIL, но tstate=NULL → SIGABRT.

Решение: на Mac прячем кнопку закрытия в заголовке. Окно можно закрыть
только in-app кнопками (Skip / Cancel / ОК), которые работают штатно.
"""
import logging
import tkinter as tk

import config

log = logging.getLogger(__name__)


def harden_for_mac(win: tk.Toplevel) -> None:
    """Убирает кнопку закрытия (красный X) в заголовке окна на Mac.

    На Windows/Linux ничего не делает. На Mac — после рендера окна находит
    соответствующий NSWindow и убирает NSWindowStyleMaskClosable.
    """
    if not config.IS_MAC:
        return
    try:
        win.update_idletasks()
        from AppKit import NSApp, NSWindowStyleMaskClosable, NSWindowStyleMaskMiniaturizable
        w = win.winfo_width()
        h = win.winfo_height()
        title = win.title()
        for nswin in NSApp.windows():
            if str(nswin.title()) == title:
                mask = nswin.styleMask()
                nswin.setStyleMask_(mask & ~NSWindowStyleMaskClosable & ~NSWindowStyleMaskMiniaturizable)
                return
        # Fallback: по размеру
        for nswin in NSApp.windows():
            fr = nswin.frame()
            if int(fr.size.width) == w and int(fr.size.height) == h:
                mask = nswin.styleMask()
                nswin.setStyleMask_(mask & ~NSWindowStyleMaskClosable & ~NSWindowStyleMaskMiniaturizable)
                return
        log.warning("harden_for_mac: NSWindow для %r (%dx%d) не найден", title, w, h)
    except Exception:
        log.exception("harden_for_mac failed")
