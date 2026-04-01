# MeetDescriber — инструкции для Claude

## Описание проекта
Кроссплатформенное desktop-приложение (Windows + macOS, Python + tkinter) которое:
- записывает аудио с системного loopback (WASAPI на Windows, BlackHole на Mac) во время Google Meet
- автоматически стартует/останавливает запись через Chrome Extension (Native Messaging)
- транскрибирует через faster-whisper/CUDA (Windows) или mlx-whisper/Apple Silicon (Mac)
- диаризует через pyannote.audio 4.x (CPU на обоих платформах)
- генерирует три документа: транскрипцию, смысловой анализ, follow-up через Claude Code CLI
- показывает всё через tray/menubar иконку + tkinter диалоги

## Стек
- Python 3.11, tkinter, pystray, Pillow
- Windows: faster-whisper (whisper-large-v3, CUDA), PyAudioWPatch (WASAPI loopback)
- Mac: mlx-whisper (whisper-large-v3-mlx, Apple Silicon), sounddevice + BlackHole
- pyannote.audio 4.0.4 (CPU), SQLite (app/storage/db.py), WAV-чанки по 30 сек
- LLM: Claude Code CLI subprocess с флагами `-p - --allowedTools Write Edit`, `cwd` = корень проекта
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
- `ClaudeManualDialog` показывается **всегда** на этапах анализа и follow-up (не как fallback, а основной путь). Кнопки: «Запустить» (subprocess с `--allowedTools Write Edit`), «Скопировать команду», «Скопировать промпт», «Этап выполнен», «Пропустить»
- Путь к claude CLI: Windows — glob по `%APPDATA%\Claude\claude-code\*\claude.exe`; Mac — glob по `~/Library/Application Support/Claude/claude-code-vm/*/claude`. Fallback: `shutil.which`
- `sqlite3.Row` → всегда конвертировать в `dict()` перед `.get()`

## Mac-специфичные решения
- **Event loop**: `root.mainloop()` отпускает GIL в C-коде Tk; PyObjC NSMenu callback → SIGABRT. Используем ручной цикл: `NSApp.nextEventMatchingMask + root.update()`
- **Thread safety**: PyObjC callbacks не могут вызывать `root.after()` напрямую. `App._schedule(fn)` кладёт fn в `SimpleQueue`; main loop дренирует её между итерациями
- **Кнопки tkinter**: на Mac Aqua-тема игнорирует `bg`/`fg`/`relief="flat"` — не указывать эти параметры
- **Выход**: `os._exit(0)` после `tray.stop()` — `sys.exit()` и `ns_app.terminate_()` вызывают crash reporter
- **BlackHole**: 48кГц, 2ch. Настроить Multi-Output Device в Audio MIDI Setup. Требуется разрешение на микрофон (системные настройки)
- **Native Messaging**: `.sh`-лончер создаётся динамически через `create_sh_launcher()` в `install_host.py`

## Запуск
```bash
# Windows — из терминала
python -m app.main
# логи: Get-Content app.log -Wait

# Windows (без CMD-окна) — двойной клик
start_windows.vbs

# Mac — из терминала
bash start_mac.sh
# логи: tail -f app.log
```

## Бэклог (BACKLOG.md)
Подробности в BACKLOG.md — там список запланированных улучшений с ID (B1–B8).
