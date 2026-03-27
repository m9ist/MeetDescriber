"""
Генерация структурированного follow-up через Claude API.

Использует analysis.md как входные данные (уже отфильтрованный смысл).
Результат: _followup.md
"""
from __future__ import annotations

from pathlib import Path

import config

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


def run_followup(
    analysis_path: Path,
    title: str,
    started_at: str,
) -> str:
    """Отправляет анализ в Claude и возвращает текст follow-up."""
    import anthropic

    analysis_text = analysis_path.read_text(encoding="utf-8")
    date = (started_at or "")[:10]

    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=title,
        date=date,
        analysis=analysis_text,
    )

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return message.content[0].text


def write_followup_md(
    path: Path,
    title: str,
    started_at: str,
    analysis_path: Path,
) -> Path:
    """Генерирует _followup.md. Возвращает путь."""
    followup_text = run_followup(analysis_path, title, started_at)

    date = (started_at or "")[:10]
    header = f"# Follow-up: {title or 'Встреча'}\n\n**Дата:** {date}\n\n---\n\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + followup_text, encoding="utf-8")
    return path
