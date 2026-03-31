"""
Смысловой анализ расшифровки через claude CLI.

Промпт сохраняется в *_analysis_prompt.md — можно запустить вручную.
Результат: _analysis.md
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import config
from prompts import ANALYSIS_SYSTEM_PROMPT as SYSTEM_PROMPT
from prompts import ANALYSIS_USER_TEMPLATE as USER_PROMPT_TEMPLATE

log = logging.getLogger(__name__)


def _build_prompt(
    transcription_path: Path,
    title: str,
    started_at: str,
    agenda: str,
) -> str:
    transcription_text = transcription_path.read_text(encoding="utf-8")
    date = (started_at or "")[:10]
    agenda_block = f"**Агенда:** {agenda.strip()}" if agenda and agenda.strip() else ""
    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=title,
        date=date,
        agenda_block=agenda_block,
        transcription=transcription_text,
    )
    return SYSTEM_PROMPT + "\n\n---\n\n" + user_prompt


def _build_chat_prompt(
    transcription_path: Path,
    title: str,
    started_at: str,
    agenda: str,
    output_path: Path,
) -> str:
    """Версия промпта для вставки в чат — расшифровка указывается файлом, не инлайн."""
    date = (started_at or "")[:10]
    agenda_block = f"**Агенда:** {agenda.strip()}" if agenda and agenda.strip() else ""
    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=title,
        date=date,
        agenda_block=agenda_block,
        transcription=f"[файл: {transcription_path}]\n(читай файл по 100 строк)",
    )
    return (
        SYSTEM_PROMPT
        + "\n\n---\n\n"
        + user_prompt
        + f"\n\n---\n\nЗапиши результат в файл:\n{output_path}"
    )


def write_analysis_md(
    path: Path,
    title: str,
    started_at: str,
    agenda: str,
    transcription_path: Path,
    prompt_path: Optional[Path] = None,
    ask_claude: Optional[Callable] = None,
) -> Path:
    """Генерирует _analysis.md. Сохраняет промпт рядом. Возвращает путь."""
    prompt = _build_prompt(transcription_path, title, started_at, agenda)
    chat_prompt = _build_chat_prompt(transcription_path, title, started_at, agenda, path)

    if prompt_path:
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(chat_prompt, encoding="utf-8")
        log.info("Промпт анализа сохранён: %s", prompt_path)

    if ask_claude is None:
        raise RuntimeError("ask_claude не передан — ручной запуск невозможен")
    cli = config._find_claude_cli()
    result = ask_claude("анализ", prompt_path, cli,
                        chat_prompt=chat_prompt, output_path=path)
    if result is None:
        raise RuntimeError("Пользователь отменил генерацию анализа")
    if result == "__STAGE_DONE__":
        log.info("Анализ: файл записан вручную, пропускаем запись → %s", path)
        return path
    analysis_text = result

    date = (started_at or "")[:10]
    header = f"# Смысловой анализ: {title or 'Встреча'}\n\n**Дата:** {date}\n\n---\n\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + analysis_text, encoding="utf-8")
    return path
