"""
Генерация структурированного follow-up через claude CLI.

Промпт сохраняется в *_followup_prompt.md — можно запустить вручную.
Результат: _followup.md
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
Ты — бизнес-ассистент. Тебе дан смысловой анализ совещания.
Сгенерируй структурированный follow-up документ.

Правила:
- Пиши на русском языке
- Будь конкретным и лаконичным
- Задачи формулируй в виде действий (глагол + что сделать)
- Если ответственный не назван явно — пиши "не назначен"
- Если срок не назван — пиши "срок не указан"
"""

USER_PROMPT_TEMPLATE = """\
## Совещание: {title}
**Дата:** {date}

## Смысловой анализ

{analysis}

---

Сгенерируй follow-up документ со следующими разделами:

### Задачи
Таблица: | Задача | Ответственный | Срок |

### Следующие шаги
Нумерованный список конкретных действий в хронологическом порядке.

### Открытые вопросы
Вопросы, требующие уточнения или ответа до следующей встречи.

### Итоги встречи
2–3 предложения: что обсудили и к чему пришли.
"""


def _build_prompt(analysis_path: Path, title: str, started_at: str) -> str:
    analysis_text = analysis_path.read_text(encoding="utf-8")
    date = (started_at or "")[:10]
    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=title,
        date=date,
        analysis=analysis_text,
    )
    return SYSTEM_PROMPT + "\n\n---\n\n" + user_prompt


def _call_claude(prompt: str) -> str:
    """Вызывает claude -p через subprocess. Использует подписку, не API-кредиты."""
    log.info("Запуск claude CLI для follow-up...")
    result = subprocess.run(
        ["claude", "-p", prompt],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"claude CLI завершился с кодом {result.returncode}: {result.stderr[:500]}"
        )
    text = result.stdout.strip()
    if not text:
        raise RuntimeError("claude CLI вернул пустой ответ")
    return text


def write_followup_md(
    path: Path,
    title: str,
    started_at: str,
    analysis_path: Path,
    prompt_path: Optional[Path] = None,
) -> Path:
    """Генерирует _followup.md. Сохраняет промпт рядом. Возвращает путь."""
    prompt = _build_prompt(analysis_path, title, started_at)

    if prompt_path:
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(prompt, encoding="utf-8")
        log.info("Промпт follow-up сохранён: %s", prompt_path)

    followup_text = _call_claude(prompt)

    date = (started_at or "")[:10]
    header = f"# Follow-up: {title or 'Встреча'}\n\n**Дата:** {date}\n\n---\n\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + followup_text, encoding="utf-8")
    return path
