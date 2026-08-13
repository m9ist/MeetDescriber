# for_meets

Сервис для автоматической записи, расшифровки и анализа встреч (Google Meet и ручной запуск).

## Что делает

Захватывает системный звук и превращает запись в три структурированных документа:

1. **`_transcription.md`** — подробная расшифровка с временными метками и спикерами
2. **`_analysis.md`** — смысловой анализ: ключевые тезисы, решения, договорённости, открытые вопросы
3. **`_followup.md`** — follow-up с таблицей задач, следующими шагами и итогами встречи

## Статус

Базовый пайплайн реализован и протестирован:

- ✅ Захват системного аудио (WASAPI loopback на Windows, Core Audio Tap на macOS 14.4+)
- ✅ Автодетект Google Meet через Chrome расширение
- ✅ Транскрипция:
  - Windows: **GigaAM v2_rnnt** (Sber, default) или faster-whisper (large-v3) — переключатель `TRANSCRIPTION_ENGINE`
  - macOS: mlx-whisper / Apple Silicon
- ✅ Диаризация спикеров (pyannote.audio 4.x, CUDA на Win)
- ✅ LLM-анализ и follow-up через `claude -p` (подписка Claude.ai)
- ✅ Tray-приложение с меню заданий, окно «Все совещания»
- ✅ Спектр-визуализатор АЧХ при записи
- ✅ Alert «⚠ Микрофон лагает» при drift >5s между mic и loopback
- ✅ Логирование UI-действий пользователя в `app.log`
- ✅ Heartbeat и логирование любого варианта выхода — если приложение исчезло, в логе видно почему
- ✅ Утилита разовой транскрипции файла: `python tools/transcribe_file.py <file>` (см. `docs/TRANSCRIBE_FILE.md`)

## Быстрый старт

### Требования

- Python 3.11+
- NVIDIA GPU + CUDA 12.x (Windows) или Apple Silicon (macOS)
- [Claude Code](https://code.claude.com/docs/en/overview) установлен и авторизован
- HuggingFace токен с доступом к gated-репозиториям pyannote
- Chrome с установленным расширением (для автодетекта Meet)

### Установка (Windows)

```bash
# Создать venv и поставить зависимости
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements-windows.txt

# Скопировать и заполнить .env
cp .env.example .env
# HUGGINGFACE_TOKEN=hf_...

# Зарегистрировать Native Messaging хост для Chrome
python app/extension/install_host.py
```

### Запуск (Windows)

```bash
python -m app.main
# или двойной клик на start_windows.vbs
```

Логи — `app.log` в корне проекта (при >20 МБ предыдущий уезжает в `app.log.1`).
Рядом `app.err.log` — stderr от лончера, в норме пустой.

### Установка (macOS)

> **Требования:** Python 3.11 (системный macOS Python — 3.9, не подходит) + macOS 14.4+ для Core Audio Tap. Tkinter идёт отдельным пакетом от Homebrew. Для сборки `audiotee` (Swift) нужны Xcode Command Line Tools: `xcode-select --install`.

```bash
# Установить Python 3.11, tkinter, Xcode CLT (если ещё нет)
brew install python@3.11 python-tk@3.11
xcode-select --install  # для swift compiler (audiotee)

# Создать venv и поставить зависимости
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-mac.txt

# Скопировать и заполнить .env
cp .env.example .env
# HUGGINGFACE_TOKEN=hf_...
# CLAUDE_CLI=/Users/username/.local/bin/claude (опционально, есть автодетект)

# Зарегистрировать Native Messaging хост для Chrome
python app/extension/install_host.py
```

### Захват системного звука (macOS)

Используется **Core Audio Tap** (Apple API, macOS 14.4+) через бандлированный
`audiotee` бинарь — **без BlackHole, без Multi-Output Device, без перенастройки
системного output'а**.

При первом запуске macOS покажет два диалога:
- **"MeetDescriber хочет получить доступ к микрофону"** → Allow
- **"MeetDescriber хочет записывать системный звук"** → Allow

Если разрешения не были выданы — открыть **System Settings → Privacy & Security**:
- Microphone → MeetDescriber ✓
- Audio Recording (или "System Audio Recording") → MeetDescriber ✓

> **Старые версии macOS (< 14.4):** Core Audio Taps недоступны, используйте
> legacy-фолбэк с BlackHole. См. `app/capture/audio_capture.py` historical
> `_capture_blackhole`.

### Запуск (macOS)

```bash
# Создать .app бандл (один раз)
bash create_mac_app.sh

# Запуск — двойной клик MeetDescriber.app в Finder
# или из терминала:
open MeetDescriber.app
# или напрямую:
python3 -m app.main
```

## Структура документов

Документы сохраняются в `data/documents/` по шаблону:

```
YYYY-MM-DD_<title>_transcription.md
YYYY-MM-DD_<title>_analysis.md
YYYY-MM-DD_<title>_followup.md
YYYY-MM-DD_<title>_analysis_prompt.md   ← промпт для ручного запуска
YYYY-MM-DD_<title>_followup_prompt.md   ← промпт для ручного запуска
```

## Переменные окружения

| Переменная | Описание |
|------------|----------|
| `HUGGINGFACE_TOKEN` | Токен HuggingFace (gated repos: pyannote/speaker-diarization-3.1) |
| `CLAUDE_CLI` | Путь к `claude.exe` (опционально — обнаруживается автоматически) |
| `TRANSCRIPTION_ENGINE` | `gigaam` (default на Win) или `whisper` — выбор движка транскрипции |
| `GIGAAM_MODEL` | `v2_rnnt` (default) или `v2_ctc` — модель GigaAM |
| `HF_HUB_DISABLE_SYMLINKS_WARNING` | Установить `1` на Windows без Developer Mode |

## Дополнительные документы

- [`docs/SPEC.md`](docs/SPEC.md) — техническое задание
- [`docs/PLAN.md`](docs/PLAN.md) — исходный план реализации
- [`docs/BACKLOG.md`](docs/BACKLOG.md) — открытые задачи и идеи
- [`docs/TRANSCRIBE_FILE.md`](docs/TRANSCRIBE_FILE.md) — утилита разовой транскрипции одного файла
- [`research/transcribe_compare/README.md`](research/transcribe_compare/README.md) — сравнение движков транскрипции на RU
