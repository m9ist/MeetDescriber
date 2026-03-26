# План реализации: for_meets

## Финальный стек

| Компонент | Решение | Примечание |
|-----------|---------|------------|
| Язык | Python 3.11+ | |
| Tray-приложение | `pystray` | Кроссплатформенный tray/menubar |
| Захват аудио (Windows) | `PyAudioWPatch` | WASAPI loopback |
| Захват аудио (Mac) | `sounddevice` + BlackHole | Виртуальное аудиоустройство |
| Транскрипция (Windows) | `faster-whisper` + CUDA | GPU-ускорение через NVIDIA |
| Транскрипция (Mac) | `mlx-whisper` | Apple Silicon (MLX framework) |
| Диаризация | `pyannote.audio 3.1` | Требует HuggingFace token + принятие лицензии |
| LLM-анализ | `anthropic` SDK + Claude API | Смысловой анализ и follow-up |
| Браузерное расширение | Chrome Manifest V3 + Native Messaging | Список вкладок + автодетект Meet |
| База данных | SQLite (файл `data/meets.db`) | Сессии, спикеры, статусы |
| Конфиг | `.env` | Токены, пути, настройки |
| Формат документов | Markdown `.md` | |

---

## Структура проекта

```
for_meets/
├── app/
│   ├── main.py                  # Точка входа, tray-приложение
│   ├── capture/
│   │   ├── audio_capture.py     # Захват системного аудио
│   │   └── source_selector.py   # Диалог выбора источника
│   ├── transcription/
│   │   ├── backend.py           # Абстракция TranscriptionBackend
│   │   ├── faster_whisper.py    # Windows/CUDA реализация
│   │   └── mlx_whisper.py       # Mac/Apple Silicon реализация
│   ├── diarization/
│   │   └── pyannote.py          # Диаризация + сопоставление спикеров
│   ├── processing/
│   │   ├── analysis.py          # Смысловой анализ (Claude API)
│   │   └── followup.py          # Генерация follow-up (Claude API)
│   ├── storage/
│   │   ├── db.py                # SQLite: сессии, спикеры, задания
│   │   └── file_manager.py      # Именование и сохранение .md файлов
│   ├── ui/
│   │   ├── tray.py              # Tray-меню
│   │   ├── dialogs.py           # Диалоги запуска, агенды
│   │   └── notifications.py     # Toast-уведомления
│   └── extension/
│       ├── native_host.py       # Native Messaging хост
│       └── chrome/              # Исходники расширения
│           ├── manifest.json
│           ├── background.js
│           └── icons/
├── data/                        # Создаётся автоматически, в .gitignore
│   ├── meets.db
│   ├── recordings/
│   └── documents/
├── setup.py                     # Скрипт первоначальной настройки
├── .env                         # Токены (не в git)
├── .env.example
├── requirements-windows.txt
├── requirements-mac.txt
└── SPEC.md
```

---

## Этапы реализации

### Этап 0 — Скелет и setup-скрипт
- [ ] Структура папок и пустые модули
- [ ] `setup.py`: проверка зависимостей, установка расширения, тест захвата и транскрипции
- [ ] `.env` загрузка конфига
- [ ] SQLite схема: таблицы `sessions`, `speakers`, `jobs`

### Этап 1 — Захват аудио
- [ ] `PyAudioWPatch` / `sounddevice` — запись системного звука чанками
- [ ] Сохранение чанков на диск
- [ ] Режим ожидания сигнала (silence detection)
- [ ] Оценка качества чанка (confidence на лету через mini-whisper)
- [ ] Toast при низком качестве

### Этап 2 — Браузерное расширение
- [ ] Chrome расширение: мониторинг вкладок, детект `meet.google.com`
- [ ] Native Messaging хост на Python
- [ ] Диалог выбора источника с вкладками браузера

### Этап 3 — Tray и UI
- [ ] `pystray` tray-иконка
- [ ] Меню: статус, необработанные/обработанные задания, ручной запуск
- [ ] Диалог запуска: название совещания + агенда
- [ ] Уведомление при автодетекте Meet + кнопка "не записывать"
- [ ] Попап "Обработать сейчас?" по окончании

### Этап 4 — Транскрипция и диаризация
- [ ] `TranscriptionBackend` абстракция
- [ ] `faster-whisper` (Windows/CUDA)
- [ ] `mlx-whisper` (Mac)
- [ ] `pyannote.audio` диаризация
- [ ] Совмещение транскрипции с диаризацией по временны́м меткам
- [ ] Идентификация спикеров по именам + сохранение в БД
- [ ] Генерация `_transcription.md`

### Этап 5 — LLM-обработка
- [ ] Промпт смыслового анализа с временны́ми метками и авторами → `_analysis.md`
- [ ] Промпт follow-up → `_followup.md`

### Этап 6 — Полировка
- [ ] Обработка ошибок и переподключение
- [ ] Логирование
- [ ] Тесты на ключевые модули

---

## Переменные окружения

| Переменная | Описание |
|------------|----------|
| `HUGGINGFACE_TOKEN` | Токен для скачивания pyannote моделей |
| `ANTHROPIC_API_KEY` | API ключ Claude |
