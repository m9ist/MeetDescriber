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
        log.info("tmp exists=%s size=%s", os.path.exists(tmp_path), os.path.getsize(tmp_path) if os.path.exists(tmp_path) else "N/A")
        # Тест: работает ли type вообще?
        r_type = subprocess.run(f'type "{tmp_path}"', shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        log.info("type test rc=%d bytes=%d stderr=%r", r_type.returncode, len(r_type.stdout or b""), (r_type.stderr or b"")[:80])
        # Тест: запускается ли вообще что-нибудь через shell?
        r_echo = subprocess.run("echo ok", shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        log.info("echo test rc=%d out=%r", r_echo.returncode, r_echo.stdout[:20])
        cli_dir = os.path.dirname(cli)
        import glob as _glob
        log.info("CLAUDE_CLI env=%r", os.environ.get("CLAUDE_CLI"))
        log.info("isfile=%s isdir=%s", os.path.isfile(cli), os.path.isdir(cli_dir))
        _pat = str(Path.home()) + r"\AppData\Roaming\Claude\claude-code\*\claude.exe"
        log.info("glob=%r", _glob.glob(_pat))
        # Тест 1: доступна ли директория claude через cmd?
        r_dir = subprocess.run(f'dir "{cli_dir}"', shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        log.info("dir test rc=%d bytes=%d err=%r", r_dir.returncode, len(r_dir.stdout), r_dir.stderr[:60])
        r_dir2 = subprocess.run('echo %APPDATA%', shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5)
        log.info("cmd APPDATA=%r", r_dir2.stdout.strip())
        # Тест 2: PowerShell вместо cmd
        r_ps = subprocess.run(
            ['powershell', '-NoProfile', '-NonInteractive', '-Command', f'& "{cli}" --version'],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20,
        )
        log.info("powershell claude rc=%d out=%r err=%r", r_ps.returncode, r_ps.stdout[:80], r_ps.stderr[:80])
        # Основной вызов — пробуем PowerShell pipe
        if r_ps.returncode == 0:
            log.info("PowerShell работает — используем его для основного вызова")
            result = subprocess.run(
                ['powershell', '-NoProfile', '-NonInteractive', '-Command',
                 f'Get-Content -Raw -Encoding UTF8 "{tmp_path}" | & "{cli}" -p -'],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
            )
        else:
            result = subprocess.run(
                f'type "{tmp_path}" | "{cli}" -p -',
                shell=True, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=300,
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
