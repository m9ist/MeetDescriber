# MeetDescriber — инструкции для Claude

## Описание проекта
Desktop-приложение для Windows (Python + tkinter) которое:
- записывает аудио с системного loopback (WASAPI) или микрофона во время Google Meet
- автоматически стартует/останавливает запись через Chrome Extension (Native Messaging)
- транскрибирует через faster-whisper (CUDA/CPU), диаризует через pyannote.audio (CPU)
- генерирует три документа: транскрипцию, смысловой анализ, follow-up через `claude -p`
- показывает всё через tray-иконку + tkinter диалоги

## Стек
- Python 3.10, tkinter, pystray, Pillow
- faster-whisper (whisper-large-v3, CUDA), pyannote.audio 3.x (CPU)
- SQLite (app/storage/db.py), WAV-чанки по 30 сек
- LLM: `claude -p "prompt"` subprocess (Claude.ai subscription, не API credits)
- **Anthropic API намеренно не используется**: корпоративные Google Workspace аккаунты не разрешают подключать сторонние OAuth/API приложения. Используем только CLI.

## Структура
```
app/
  main.py              # точка входа, App class, tkinter mainloop
  capture/             # AudioCapture, WASAPI loopback, quality check
  diarization/         # PyannoteDiarizer
  extension/           # Chrome Native Messaging host
  processing/          # pipeline.py, analysis.py, followup.py
  storage/             # db.py, file_manager.py
  transcription/       # FasterWhisperBackend, MLXWhisperBackend
  ui/                  # tray.py, dialogs.py, spectrum.py, status_window.py
config.py              # пути, константы
data/documents/        # готовые MD-файлы (transcription, analysis, followup)
data/recordings/       # WAV-чанки по сессиям
```

## Правила разработки
- **Коммитить без запроса подтверждения** — пользователь разрешил
- **Не создавать ветки** — работаем напрямую в `main`
- Все изменения — напрямую в файлы, без временных скриптов
- Логи пишутся в `app.log` (root проекта)
- `.env` содержит секреты — никогда не коммитить (уже в .gitignore)

## Ключевые архитектурные решения
- pyannote принудительно на CPU (`_pipeline.to(torch.device("cpu"))`) — иначе CUDA OOM с whisper-large-v3
- quality-модель (whisper-tiny) тоже CPU — CUDA context crash из ThreadPoolExecutor
- pipeline идемпотентен: каждый этап проверяет наличие файла перед запуском
- `init_db()` сбрасывает зависшие `processing` → `pending` при старте
- LLM-промпты сохраняются в `*_analysis_prompt.md` / `*_followup_prompt.md` для ручного перезапуска
- Когда CLI недоступен — показывается `ClaudeManualDialog` с 3 кнопками: «Запустить», «Скопировать команду», «Скопировать промпт»
- `sqlite3.Row` → всегда конвертировать в `dict()` перед `.get()`

## Запуск
```bash
python -m app.main
# логи: Get-Content app.log -Wait
```

## Бэклог (BACKLOG.md)
Подробности в BACKLOG.md — там список запланированных улучшений с ID (B1–B8).
