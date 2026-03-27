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
| Диаризация | `pyannote.audio 4.x` | Установлена 4.0.4; требует 3 gated-репо + токен с правом на gated repos |
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

## Проверка гипотез

Перед или в рамках Этапа 0 явно проверяем рискованные допущения.
Если гипотеза не подтверждается — фиксируем и ищем альтернативу до того, как строим на ней логику.

| # | Гипотеза | Статус | Результат / Примечание |
|---|----------|--------|------------------------|
| H1 | WASAPI loopback захватывает системный звук на Windows | ✅ Подтверждена | HyperX Amp Chat loopback, 304 KB / 5 сек |
| H2 | BlackHole захватывает звук на Mac | ⏳ Отложена | Проверим на Mac |
| H3 | faster-whisper (CUDA) даёт приемлемое качество для русского live-аудио | ✅ Подтверждена | CUDA работает; качество на реальной речи — Этап 4 |
| H4 | mlx-whisper работает на Mac M4 Pro | ⏳ Отложена | Проверим на Mac |
| H5 | pyannote.audio разделяет спикеров в русской речи | ✅ Технически работает | Pipeline загружается и работает на CUDA. Качество на русской речи — Этап 4. Нюанс: pyannote 4.x требует передавать аудио как тензор (torchcodec не работает на Windows без FFmpeg full-shared) |
| H6 | Confidence score Whisper коррелирует с реальным качеством | ✅ Подтверждена | Чистый сигнал → высокий score, шум → низкий |
| H7 | Native Messaging между Chrome и Python работает стабильно | ✅ Подтверждена | Chrome детектит meet.google.com → расширение отправляет meet_started/meet_ended → Python-хост получает. Нюанс: .bat не работает с Chrome CreateProcess — нужен .exe (PyInstaller --onedir) |
| H8 | Транскрипция при 2x скорости воспроизведения приемлема | ⏳ Этап 4 | Проверим на реальной записи |

**Выводы по установке (Windows):**
- CUDA DLL от pip (`nvidia-cublas-cu12`) нужно добавлять в PATH вручную — решено в `config.py`
- `torch` при установке через `--user` не заменяет системную версию без `--force-reinstall`
- pyannote 4.x зависит от трёх gated-репозиторов: `speaker-diarization-3.1`, `segmentation-3.0`, `speaker-diarization-community-1`
- pyannote 4.x принимает аудио как `{"waveform": Tensor, "sample_rate": int}` — torchcodec не нужен
- Chrome Native Messaging на Windows требует .exe — .bat файлы не работают через CreateProcess
- PyInstaller `--onefile` может блокироваться Defender при распаковке во temp; `--onedir` надёжнее
- После регистрации хоста в реестре требуется полный перезапуск Chrome

---

## Этапы реализации

### Этап 0 — Скелет и setup-скрипт ✅
- [x] Структура папок и пустые модули
- [x] `.env` загрузка конфига
- [x] SQLite схема: таблицы `sessions`, `speakers`, `jobs`
- [x] `setup.py`: проверка зависимостей, последовательный запуск проверок гипотез H1–H8
- [x] Результат setup — читаемый отчёт: что прошло, что нет, что делать дальше

### Этап 1 — Захват аудио ✅ `[H1, H2]`
- [x] `PyAudioWPatch` / `sounddevice` — запись системного звука чанками
- [x] Сохранение чанков на диск
- [x] Режим ожидания сигнала (silence detection)
- [x] Оценка качества чанка (confidence на лету через mini-whisper) `[H6]`
- [ ] Toast при низком качестве — реализуем в Этапе 3 (нужен UI)

### Этап 2 — Браузерное расширение ✅ `[H7]`
- [x] Chrome расширение: мониторинг вкладок, детект `meet.google.com`
- [x] Native Messaging хост на Python (скомпилирован в .exe через PyInstaller)
- [ ] Диалог выбора источника с вкладками браузера — реализуем в Этапе 3

### Этап 3 — Tray и UI
- [ ] `pystray` tray-иконка
- [ ] Меню: статус, необработанные/обработанные задания, ручной запуск
- [ ] Диалог запуска: название совещания + агенда
- [ ] Уведомление при автодетекте Meet + кнопка "не записывать"
- [ ] Попап "Обработать сейчас?" по окончании

### Этап 4 — Транскрипция и диаризация `[H3, H4, H5, H6, H8]`
- [ ] `TranscriptionBackend` абстракция
- [ ] `faster-whisper` (Windows/CUDA) `[H3]`
- [ ] `mlx-whisper` (Mac) `[H4]`
- [ ] `pyannote.audio` диаризация `[H5]`
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
| `HUGGINGFACE_TOKEN` | Токен для скачивания pyannote моделей (должен иметь доступ к gated repos) |
| `HF_TOKEN` | Дубль HUGGINGFACE_TOKEN — нужен для faster-whisper и huggingface_hub |
| `HF_HUB_DISABLE_SYMLINKS_WARNING` | Установить в `1` на Windows без Developer Mode |
| `ANTHROPIC_API_KEY` | API ключ Claude |
