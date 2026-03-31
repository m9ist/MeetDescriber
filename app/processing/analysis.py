"""
Смысловой анализ расшифровки через claude CLI.

Промпт сохраняется в *_analysis_prompt.md — можно запустить вручную.
Результат: _analysis.md
"""
from __future__ import annotations

import logging
import os
import subprocess
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
        transcription=f"[файл: {transcription_path}]",
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

    if prompt_path:
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        log.info("Промпт анализа сохранён: %s", prompt_path)

    try:
        analysis_text = _call_claude_cli(prompt)
    except (FileNotFoundError, OSError, RuntimeError) as e:
        if ask_claude is None:
            raise
        log.warning("CLI недоступен (%s) — показываем диалог ручного запуска", e)
        cli = config._find_claude_cli()
        chat_prompt = _build_chat_prompt(transcription_path, title, started_at, agenda, path)
        result = ask_claude("анализ", prompt_path, cli, chat_prompt=chat_prompt)
        if result is None:
            raise RuntimeError("Пользователь отменил генерацию анализа") from e
        analysis_text = result

    date = (started_at or "")[:10]
    header = f"# Смысловой анализ: {title or 'Встреча'}\n\n**Дата:** {date}\n\n---\n\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + analysis_text, encoding="utf-8")
    return path
