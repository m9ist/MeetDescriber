# for_meets

Сервис для автоматической записи, расшифровки и анализа встреч (Google Meet и ручной запуск).

## Что делает

Захватывает системный звук и превращает запись в три структурированных документа:

1. **`_transcription.md`** — подробная расшифровка с временными метками и спикерами
2. **`_analysis.md`** — смысловой анализ: ключевые тезисы, решения, договорённости, открытые вопросы
3. **`_followup.md`** — follow-up с таблицей задач, следующими шагами и итогами встречи

## Статус

Базовый пайплайн реализован и протестирован:

- ✅ Захват системного аудио (WASAPI loopback на Windows, BlackHole на macOS)
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

### Установка (macOS)

> **Требования:** Python 3.11 (системный macOS Python — 3.9, не подходит).
> Tkinter идёт отдельным пакетом от Homebrew.

```bash
# Установить Python 3.11 и tkinter
brew install python@3.11 python-tk@3.11

# Предварительно: установить BlackHole (виртуальный аудиодрайвер)
# После установки требуется перезагрузка
brew install blackhole-2ch

# Создать venv и поставить зависимости
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements-mac.txt

# Скопировать и заполнить .env
cp .env.example .env
# HUGGINGFACE_TOKEN=hf_...
# CLAUDE_CLI=/Users/username/.claude/local/claude

# Зарегистрировать Native Messaging хост для Chrome
python app/extension/install_host.py
```

### Настройка аудио (macOS)

После установки BlackHole настрой маршрутизацию звука:

1. Открой **Audio MIDI Setup** (`/Applications/Utilities/`)
2. Нажми `+` → **"Create Multi-Output Device"**
3. Включи галочки: **BlackHole 2ch** и **MacBook Pro Speakers** (или наушники)
4. Установи частоту **48,0 кГц**
5. **System Settings → Sound → Output** → выбрать **Multi-Output Device**

При первом запуске приложения macOS запросит доступ к микрофону — разрешить.

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
