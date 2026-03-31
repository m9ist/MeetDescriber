"""
Генерация структурированного follow-up через claude CLI.

Промпт сохраняется в *_followup_prompt.md — можно запустить вручную.
Результат: _followup.md
"""
from __future__ import annotations

import logging
import os
import subprocess
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


def _call_claude_cli(prompt: str) -> str:
    """Пробует вызвать claude CLI через stdin. Бросает OSError/FileNotFoundError если CLI недоступен."""
    import tempfile
    cli = config._find_claude_cli()
    log.info("Запуск claude CLI: %r", cli)

    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as f:
        f.write(prompt.encode("utf-8"))
        tmp_path = f.name

    try:
        with open(tmp_path, "rb") as fh:
            result = subprocess.run(
                [cli, "-p", "-"],
                stdin=fh,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=300,
            )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    stdout_text = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (result.stderr or b"").decode("utf-8", errors="replace")
    log.info("claude rc=%d stdout=%d chars stderr=%r",
             result.returncode, len(stdout_text), stderr_text[:200])

    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI завершился с кодом {result.returncode}: {stderr_text[:300]}"
        )
    text = stdout_text.strip()
    if not text:
        raise RuntimeError("claude CLI вернул пустой ответ")
    return text


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

    try:
        followup_text = _call_claude_cli(prompt)
    except (FileNotFoundError, OSError, RuntimeError) as e:
        if ask_claude is None:
            raise
        log.warning("CLI недоступен (%s) — показываем диалог ручного запуска", e)
        cli = config._find_claude_cli()
        result = ask_claude("follow-up", prompt_path, cli,
                            chat_prompt=chat_prompt, output_path=path)
        if result is None:
            raise RuntimeError("Пользователь отменил генерацию follow-up") from e
        if result == "__STAGE_DONE__":
            log.info("Follow-up: файл записан вручную, пропускаем запись → %s", path)
            return path
        followup_text = result

    date = (started_at or "")[:10]
    header = f"# Follow-up: {title or 'Встреча'}\n\n**Дата:** {date}\n\n---\n\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + followup_text, encoding="utf-8")
    return path
