"""
Логирование пользовательских действий из UI.

Все клики/нажатия в трее, окнах, диалогах должны проходить через log_action() —
чтобы в app.log была единая лента «что юзер делал». Полезно при разборе багов
типа «нажал X — приложение упало» — сразу видим что именно нажали и с какими
параметрами.

Формат:
    2026-05-14 17:30:12,123 INFO     app.user_action: start_recording_manual
    2026-05-14 17:31:45,456 INFO     app.user_action: restart_stage  job_id=42 stage='transcription'

Грепать удобно: `grep "user_action" app.log`.
"""
import logging

log = logging.getLogger("app.user_action")


def log_action(name: str, **details) -> None:
    """Логирует пользовательское действие. Безопасно вызывать из любого треда."""
    if details:
        parts = "  ".join(f"{k}={v!r}" for k, v in details.items())
        log.info("%s  %s", name, parts)
    else:
        log.info("%s", name)
