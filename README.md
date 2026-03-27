# for_meets

Сервис для автоматической записи, расшифровки и анализа встреч (Google Meet и ручной запуск).

## Что делает

Захватывает системный звук и превращает запись в три структурированных документа:

1. **`_transcription.md`** — подробная расшифровка с временными метками и спикерами
2. **`_analysis.md`** — смысловой анализ: ключевые тезисы, решения, договорённости, открытые вопросы
3. **`_followup.md`** — follow-up с таблицей задач, следующими шагами и итогами встречи

## Статус

Базовый пайплайн реализован и протестирован:

- ✅ Захват системного аудио (WASAPI loopback, Windows)
- ✅ Автодетект Google Meet через Chrome расширение
- ✅ Транскрипция (faster-whisper, large-v3, CUDA)
- ✅ Диаризация спикеров (pyannote.audio 4.x, CPU)
- ✅ LLM-анализ и follow-up через `claude -p` (подписка Claude.ai)
- ✅ Tray-приложение с меню заданий
- ✅ Спектр-визуализатор АЧХ при записи

## Быстрый старт

### Требования

- Python 3.10+
- NVIDIA GPU (для транскрипции)
- [Claude Code](https://claude.ai/code) авторизован и в PATH (или автообнаружение из `%APPDATA%\Claude\claude-code\`)
- HuggingFace токен с доступом к gated-репозиториям pyannote
- Chrome с установленным расширением (для автодетекта Meet)

### Установка

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

### Запуск

```bash
python -m app.main
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
