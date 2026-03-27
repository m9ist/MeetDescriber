"""
Смысловой анализ расшифровки через Claude API.

Промпт извлекает:
  - Ключевые тезисы с временными метками и авторами
  - Принятые решения
  - Договорённости
  - Открытые вопросы

Результат: _analysis.md
"""
from __future__ import annotations

from pathlib import Path

import config

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


def run_analysis(
    transcription_path: Path,
    title: str,
    started_at: str,
    agenda: str,
) -> str:
    """Отправляет расшифровку в Claude и возвращает текст анализа."""
    import anthropic

    transcription_text = transcription_path.read_text(encoding="utf-8")
    date = (started_at or "")[:10]
    agenda_block = f"**Агенда:** {agenda.strip()}" if agenda and agenda.strip() else ""

    user_prompt = USER_PROMPT_TEMPLATE.format(
        title=title,
        date=date,
        agenda_block=agenda_block,
        transcription=transcription_text,
    )

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_prompt}],
    )

    return message.content[0].text


def write_analysis_md(
    path: Path,
    title: str,
    started_at: str,
    agenda: str,
    transcription_path: Path,
) -> Path:
    """Генерирует _analysis.md. Возвращает путь."""
    analysis_text = run_analysis(transcription_path, title, started_at, agenda)

    date = (started_at or "")[:10]
    header = f"# Смысловой анализ: {title or 'Встреча'}\n\n**Дата:** {date}\n\n---\n\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + analysis_text, encoding="utf-8")
    return path
