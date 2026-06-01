# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

# MeetDescriber — инструкции для Claude

## Описание проекта
Кроссплатформенное desktop-приложение (Windows + macOS, Python + tkinter) которое:
- записывает аудио с системного loopback (WASAPI на Windows, BlackHole на Mac) во время Google Meet
- автоматически стартует/останавливает запись через Chrome Extension (Native Messaging)
- транскрибирует через GigaAM (Sber, native RU, default на Windows) либо faster-whisper, mlx-whisper на Mac — выбор через `TRANSCRIPTION_ENGINE`
- диаризует через pyannote.audio 4.x (CUDA на Windows, CPU на Mac)
- генерирует три документа: транскрипцию, смысловой анализ, follow-up через Claude Code CLI
- показывает всё через tray/menubar иконку + tkinter диалоги

## Стек
- Python 3.10/3.11, tkinter, pystray, Pillow
- Windows транскрипция: **GigaAM v2_rnnt** (Sber native RU, default) либо **faster-whisper** (large-v3, CUDA). Switch через `config.TRANSCRIPTION_ENGINE`.
- Mac транскрипция: mlx-whisper (whisper-large-v3-mlx, Apple Silicon)
- Захват: PyAudioWPatch (Win, WASAPI loopback) / sounddevice + BlackHole (Mac); WAV-чанки по 10 сек (mic-driven)
- pyannote.audio 4.0.4 (CUDA на Win, CPU на Mac), SQLite (app/storage/db.py)
- ffmpeg: через `static-ffmpeg` pip-пакет (бандлит бинарь, без admin)
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
  user_actions.py      # log_action(name, **kwargs) → app.user_action в app.log; вызывать из всех UI-обработчиков
  capture/             # AudioCapture: WASAPI loopback (Win) / BlackHole (Mac), silence filter, drift warning
  diarization/         # pyannote_diarizer.py (тонкий фасад), diarize_worker.py (subprocess с pyannote + CUDA)
  extension/           # Chrome Native Messaging host + install_host.py
  processing/          # pipeline.py (оркестратор), analysis.py, followup.py
  storage/             # db.py (SQLite), file_manager.py (chunks, merge)
  transcription/       # backend.py (factory), gigaam_backend.py + gigaam_worker.py,
                       # faster_whisper_backend.py + transcribe_worker.py, mlx_whisper_backend.py
  ui/                  # tray.py, dialogs.py, spectrum.py, status_window.py,
                       # notifications.py, meetings_window.py, mac_window.py (harden_for_mac)
config.py              # пути, константы, TRANSCRIPTION_ENGINE, _find_claude_cli(), IS_MAC / IS_WINDOWS
prompts.py             # шаблоны промптов для анализа и follow-up (передаются в claude CLI)
data/documents/        # готовые MD-файлы (transcription, analysis, followup)
data/recordings/       # WAV-чанки по сессиям
data/meets.db          # SQLite с sessions, jobs, speakers

tools/
  transcribe_file.py   # CLI для разовой транскрипции одного аудио/видео файла без БД,
                       # см. docs/TRANSCRIBE_FILE.md

research/
  transcribe_compare/  # сравнение whisper-large-v3 vs WhisperX vs GigaAM vs Wav2Vec2 на RU,
                       # см. research/transcribe_compare/README.md
```

## Переменные окружения (.env)
```
HUGGINGFACE_TOKEN=...        # обязательно — gated pyannote (speaker-diarization-3.1) + GigaAM longform VAD
CLAUDE_CLI=...               # опционально — путь к claude CLI (иначе автодетект)
TRANSCRIPTION_ENGINE=gigaam  # опционально: gigaam (default Win) | whisper
GIGAAM_MODEL=v2_rnnt         # опционально: v2_rnnt (default) | v2_ctc
```
Автодетект claude CLI: `%APPDATA%\Claude\claude-code\*\claude.exe` (Win) → `~/Library/Application Support/Claude/claude-code-vm/*/claude` (Mac) → fallback: `shutil.which("claude")`.

`HUGGINGFACE_TOKEN` копируется в `HF_TOKEN` (faster-whisper и gigaam читают разные имена).

## Правила разработки
- **Коммитить без запроса подтверждения** — пользователь разрешил
- **Не создавать ветки** — работаем напрямую в `main`
- Все изменения — напрямую в файлы, без временных скриптов
- Логи пишутся в `app.log` (root проекта)
- `.env` содержит секреты — никогда не коммитить (уже в .gitignore)

## Ключевые архитектурные решения

### Subprocess-изоляция тяжёлых ML-этапов (Windows)
ctranslate2 (faster-whisper) и PyTorch (pyannote/gigaam) бандлят **разные** версии `cudnn64_9.dll`. Совместная загрузка в один процесс → stack corruption (0xC0000409, BEX64) через ~8 минут работы, процесс убивается Windows без traceback. Решение — три отдельных worker-процесса:

- `app/diarization/diarize_worker.py` — pyannote + PyTorch на CUDA
- `app/transcription/transcribe_worker.py` — ctranslate2 (faster-whisper), **`sys.modules["torch"] = None`** до `import ctranslate2` (иначе ctranslate2/converters/transformers.py делает `import torch` транзитивно)
- `app/transcription/gigaam_worker.py` — gigaam + PyTorch на CUDA

Все workers стартуются через `subprocess.Popen([sys.executable, "-u", worker.py, ...])` с `PYTHONUNBUFFERED=1`, `bufsize=1`. Stdout = JSON-результат, stderr = логи + `PROGRESS:cur/total`. Parent читает stderr в daemon-треде, stdout — `proc.stdout.read()` (НЕ `communicate()` — она запускает свой stderr-reader и гонится с нашим).

VRAM освобождается автоматически при exit worker'а — `unload_model()` теперь no-op.

### Транскрипция: переключатель движка
- `config.TRANSCRIPTION_ENGINE = "gigaam"` (default) | `"whisper"`
- `app/transcription/backend.py:get_backend(engine=None)` — factory
- GigaAM: лучше покрытие на сложном аудио, без пунктуации, ~28x realtime
- faster-whisper: пунктуация + капитализация, ~8x realtime, может терять чанки с длинными внутренними паузами (см. research/transcribe_compare/README.md)

Для faster-whisper жёстко зафиксированы параметры против зависаний:
```python
temperature=(0.0,), compression_ratio_threshold=None, log_prob_threshold=None,
condition_on_previous_text=False, repetition_penalty=1.2, no_repeat_ngram_size=3
```

### Capture: drift warning
В `_flush_chunk` логируем разницу loopback↔mic. При `|diff| > DRIFT_WARN_THRESHOLD_SEC = 5.0` срабатывает `on_drift_warning(drift_sec)` → toast «⚠ Микрофон лагает». Throttling 60 сек. Симптом: другое приложение держит mic в exclusive mode, USB-драйвер тормозит.

### Логирование действий пользователя
Каждое UI-действие пишется в app.log через `app.user_actions.log_action(name, **kwargs)`. Префикс логгера — `app.user_action`. Покрыты: tray-меню, контекстные меню в meetings_window, кнопки start/stop, диалоги. При добавлении нового действия — обязательно вызвать `log_action`.

### Pipeline / БД
- pipeline идемпотентен: каждый этап проверяет наличие файла перед запуском
- статусы job: `pending` → `processing` → `transcribed` → `analyzed` → `done` (+ `error`)
- `init_db()` сбрасывает зависшие `processing` → `pending` при старте
- LLM-промпты сохраняются в `*_analysis_prompt.md` / `*_followup_prompt.md` для ручного перезапуска
- `ClaudeManualDialog` показывается **всегда** на этапах анализа и follow-up (не как fallback, а основной путь). Кнопки: «Запустить» (subprocess с `--allowedTools Write Edit`), «Скопировать команду», «Скопировать промпт», «Этап выполнен», «Пропустить»
- `sqlite3.Row` → всегда конвертировать в `dict()` перед `.get()`
- Шаблоны промптов — в `prompts.py` (функции `build_analysis_prompt()` / `build_followup_prompt()`)
- quality-модель (whisper-tiny) на CPU — CUDA context crash из ThreadPoolExecutor

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
- **`root.after()` опасен**: TK на macOS регистрирует `_runBackgroundLoop` в CFRunLoop. Когда pystray открывает меню, `NSMenuTrackingSession` запускает вложенный NSApp loop, который дёргает TK background loop, который вытаскивает `after`-таймер и зовёт Python — но GIL уже отпущен → SIGABRT `_Py_FatalError_TstateNULL`. Решение: `App._schedule(fn, delay_ms=0)` — на Mac кладёт fn в `_mac_queue` (через `threading.Timer` для delay>0), main loop дренирует только когда вне NSMenu. Передаётся в `SpectrumWidget`, `ProcessingStatusWindow`, `notifications.set_schedule()`.
- **`WM_DELETE_WINDOW` опасен**: при клике в красный «X» окна `NSControlTrackMouse → NSWindow.close → TKApplication windowShouldClose → Tk WM_DELETE_WINDOW protocol → PythonCmd` → тот же SIGABRT. Решение: `app/ui/mac_window.harden_for_mac(win)` — снимает `NSWindowStyleMaskClosable` и `NSWindowStyleMaskMiniaturizable` с NSWindow. Применяется ко всем Toplevel: `ClaudeManualDialog`, `MeetingsWindow`, `ProcessingStatusWindow`. Закрытие — только через in-app кнопки или ESC.

## Бэклог (docs/BACKLOG.md)
Подробности в docs/BACKLOG.md — там список запланированных улучшений с ID (B1–B14).
