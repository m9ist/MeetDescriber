"""
Генерация структурированного follow-up через claude CLI.

Промпт сохраняется в *_followup_prompt.md — можно запустить вручную.
Результат: _followup.md
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Optional

import config
from prompts import FOLLOWUP_SYSTEM_PROMPT as SYSTEM_PROMPT
from prompts import FOLLOWUP_USER_TEMPLATE as USER_PROMPT_TEMPLATE

log = logging.getLogger(__name__)


def _build_prompt(analysis_path: Path, title: str, started_at: str) -> str:
    analysis_text = analysis_path.read_text(encoding="utf-8")
    date = (started_at or "")[:10]
    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=title,
        date=date,
        analysis=analysis_text,
    )
    return SYSTEM_PROMPT + "\n\n---\n\n" + user_prompt


def _build_chat_prompt(
    analysis_path: Path,
    title: str,
    started_at: str,
    output_path: Path,
) -> str:
    """Версия промпта для вставки в чат — анализ указывается файлом, не инлайн."""
    date = (started_at or "")[:10]
    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=title,
        date=date,
        analysis=f"[файл: {analysis_path}]",
    )
    return (
        SYSTEM_PROMPT
        + "\n\n---\n\n"
        + user_prompt
        + f"\n\n---\n\nЗапиши результат в файл:\n{output_path}"
    )


def write_followup_md(
    path: Path,
    title: str,
    started_at: str,
    analysis_path: Path,
    prompt_path: Optional[Path] = None,
    ask_claude: Optional[Callable] = None,
) -> Path:
    """Генерирует _followup.md. Сохраняет промпт рядом. Возвращает путь."""
    prompt = _build_prompt(analysis_path, title, started_at)
    chat_prompt = _build_chat_prompt(analysis_path, title, started_at, path)

    if prompt_path:
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(chat_prompt, encoding="utf-8")
        log.info("Промпт follow-up сохранён: %s", prompt_path)

    if ask_claude is None:
        raise RuntimeError("ask_claude не передан — ручной запуск невозможен")
    cli = config._find_claude_cli()
    result = ask_claude("follow-up", prompt_path, cli,
                        chat_prompt=chat_prompt, output_path=path)
    if result is None:
        raise RuntimeError("Пользователь отменил генерацию follow-up")
    if result == "__STAGE_DONE__":
        log.info("Follow-up: файл записан вручную, пропускаем запись → %s", path)
        return path
    followup_text = result

    date = (started_at or "")[:10]
    header = f"# Follow-up: {title or 'Встреча'}\n\n**Дата:** {date}\n\n---\n\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + followup_text, encoding="utf-8")
    return path
