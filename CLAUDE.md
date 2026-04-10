# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

## Запуск
```bash
# Mac — двойной клик из Finder (без Terminal):
bash create_mac_app.sh   # создаёт MeetDescriber.app
open MeetDescriber.app    # или двойной клик в Finder
# Mac — из терминала:
source .venv/bin/activate && python -m app.main
# логи: tail -f app.log

# Windows
python -m app.main
# логи: Get-Content app.log -Wait
# или двойной клик: start_windows.vbs
```

## Диагностика и тесты
```bash
# Проверка всех гипотез (H1–H8): WASAPI, BlackHole, whisper, pyannote, native messaging
python setup.py            # setup.py — это диагностический скрипт, не сборочный

# Тест аудио-захвата BlackHole (macOS): запись 5 сек, RMS-проверка
python test_blackhole.py
```

## Структура
```
app/
  main.py              # точка входа, App class, ручной event loop (Mac) или mainloop (Win)
  capture/             # AudioCapture: WASAPI loopback (Win) / BlackHole (Mac), silence filter
  diarization/         # PyannoteDiarizer (CPU)
  extension/           # Chrome Native Messaging host + install_host.py
  processing/          # pipeline.py (оркестратор), analysis.py, followup.py
  storage/             # db.py (SQLite), file_manager.py (chunks, merge)
  transcription/       # backend.py (абстракция), faster_whisper_backend.py, mlx_whisper_backend.py
  ui/                  # tray.py, dialogs.py, spectrum.py, status_window.py
config.py              # пути, константы, _find_claude_cli(), IS_MAC / IS_WINDOWS
prompts.py             # шаблоны промптов для анализа и follow-up (передаются в claude CLI)
data/documents/        # готовые MD-файлы (transcription, analysis, followup)
data/recordings/       # WAV-чанки по сессиям
```

## Переменные окружения (.env)
```
HUGGINGFACE_TOKEN=...   # обязательно — для гейтед-модели pyannote
CLAUDE_CLI=...          # опционально — путь к claude CLI (иначе автодетект)
```
Автодетект claude CLI: `%APPDATA%\Claude\claude-code\*\claude.exe` (Win) → `~/Library/Application Support/Claude/claude-code-vm/*/claude` (Mac) → fallback: `shutil.which("claude")`.

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
- `sqlite3.Row` → всегда конвертировать в `dict()` перед `.get()`
- Шаблоны промптов — в `prompts.py` (функции `build_analysis_prompt()` / `build_followup_prompt()`)

## Модель потоков
- **Main thread:** Tkinter event loop (Win) / ручной NSApp loop (Mac)
- **Native host thread:** daemon, читает сообщения Chrome extension
- **Capture thread:** daemon, audio buffer → WAV chunks
- **Quality assessment:** ThreadPoolExecutor(2) — только CPU
- **Pipeline:** отдельный background thread, не блокирует UI
- **macOS:** `App._schedule(fn)` кладёт fn в `SimpleQueue`; main loop дренирует между итерациями — единственный безопасный способ вызова tkinter из PyObjC callbacks

## Mac-специфичные решения
- **Event loop**: `root.mainloop()` отпускает GIL в C-коде Tk; PyObjC NSMenu callback → SIGABRT. Используем ручной цикл: `NSApp.nextEventMatchingMask + root.update()`
- **Thread safety**: PyObjC callbacks не могут вызывать `root.after()` напрямую → используем `App._schedule(fn)`
- **Кнопки tkinter**: на Mac Aqua-тема игнорирует `bg`/`fg`/`relief="flat"` — не указывать эти параметры
- **Выход**: `os._exit(0)` после `tray.stop()` — `sys.exit()` и `ns_app.terminate_()` вызывают crash reporter
- **BlackHole**: 48кГц, 2ch. Настроить Multi-Output Device в Audio MIDI Setup. Требуется разрешение на микрофон
- **Native Messaging**: `.sh`-лончер создаётся динамически через `create_sh_launcher()` в `install_host.py`

## Бэклог (BACKLOG.md)
Подробности в BACKLOG.md — там список запланированных улучшений с ID (B1–B14).
