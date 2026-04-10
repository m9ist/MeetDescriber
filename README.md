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
- ✅ Транскрипция (faster-whisper/CUDA на Windows, mlx-whisper/Apple Silicon на macOS)
- ✅ Диаризация спикеров (pyannote.audio 4.x, CPU)
- ✅ LLM-анализ и follow-up через `claude -p` (подписка Claude.ai)
- ✅ Tray-приложение с меню заданий
- ✅ Спектр-визуализатор АЧХ при записи

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
| `HUGGINGFACE_TOKEN` | Токен HuggingFace (gated repos: pyannote) |
| `CLAUDE_CLI` | Путь к `claude.exe` (опционально — обнаруживается автоматически) |
| `HF_HUB_DISABLE_SYMLINKS_WARNING` | Установить `1` на Windows без Developer Mode |
