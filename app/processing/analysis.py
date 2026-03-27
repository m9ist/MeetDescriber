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
from typing import Optional

import config

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты — аналитик деловых переговоров. Тебе дана расшифровка совещания.
Твоя задача — извлечь смысловую суть в структурированном виде.

Правила:
- Пиши на русском языке
- Каждый тезис сопровождай временной меткой и автором из расшифровки
- Если тезис — синтез нескольких реплик, укажи все временные метки
- Не добавляй ничего от себя — только то, что реально было сказано
- Формат тезиса:
  **Тезис:** <текст>
  **Автор:** <имя или Спикер N>
  **Источник:** `[HH:MM:SS]` (через запятую если несколько)
"""

USER_PROMPT_TEMPLATE = """\
## Совещание: {title}
**Дата:** {date}
{agenda_block}

## Расшифровка

{transcription}

---

Извлеки из этой расшифровки следующие разделы:

1. **Ключевые тезисы** — главные мысли и утверждения участников
2. **Решения** — что было решено в ходе встречи
3. **Договорённости** — кто что берёт на себя
4. **Открытые вопросы** — то, что осталось без ответа или требует уточнения

Для каждого пункта используй формат:
**Тезис/Решение/Договорённость/Вопрос:** <текст>
**Автор:** <имя>
**Источник:** `[HH:MM:SS]`
"""


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


def _call_claude(prompt: str) -> str:
    """Вызывает claude -p, передавая промпт через временный файл и pipe cmd.exe."""
    import tempfile
    cli = config._find_claude_cli()
    log.info("Запуск claude CLI: %r", cli)
    with tempfile.NamedTemporaryFile(mode="wb", suffix=".txt", delete=False) as f:
        f.write(prompt.encode("utf-8"))
        tmp_path = f.name
    try:
        result = subprocess.run(
            f'type "{tmp_path}" | "{cli}" -p -',
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=300,
        )
    finally:
        os.unlink(tmp_path)
    stdout_text = (result.stdout or b"").decode("utf-8", errors="replace")
    stderr_text = (result.stderr or b"").decode("cp1251", errors="replace")
    log.info("claude rc=%d stdout=%d chars stderr=%r", result.returncode, len(stdout_text), stderr_text[:200])
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
) -> Path:
    """Генерирует _analysis.md. Сохраняет промпт рядом. Возвращает путь."""
    prompt = _build_prompt(transcription_path, title, started_at, agenda)

    if prompt_path:
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        log.info("Промпт анализа сохранён: %s", prompt_path)

    analysis_text = _call_claude(prompt)

    date = (started_at or "")[:10]
    header = f"# Смысловой анализ: {title or 'Встреча'}\n\n**Дата:** {date}\n\n---\n\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + analysis_text, encoding="utf-8")
    return path
