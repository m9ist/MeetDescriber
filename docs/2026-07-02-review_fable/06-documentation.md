# Ревью документации MeetDescriber (for_meets)

Дата: 2026-07-02. Ревьюер: Claude (Fable 5), роль — technical writer + senior engineer.
Проверено: README.md, CLAUDE.md, LICENSE, .env.example, docs/{SPEC,PLAN,PLAN_MAC,PLAN_meetings_window,TRANSCRIBE_FILE,BACKLOG}.md, research/transcribe_compare/README.md, vendor/audiotee/{README.md,COMMIT.txt} — и выборочная сверка с кодом (config.py, app/main.py, app/transcription/backend.py, app/extension/install_host.py, app/capture/audio_capture.py, app/capture/mac_tap.py, app/storage/{db,meetings_repo}.py, app/ui/{tray,user_actions}.py, app/processing/pipeline.py, setup.py, create_mac_app.sh, requirements-*.txt, tools/transcribe_file.py, история git).

---

## Резюме — оценка 6.5/10

Документация **живая и необычно богатая для личного проекта**: у всех ключевых модулей есть внятные module-docstrings на русском, CLAUDE.md фиксирует нетривиальные архитектурные решения (subprocess-изоляция cudnn, Mac SIGABRT-мины), BACKLOG ведётся со статусами, PLAN_meetings_window — образцово закрыт («Статус: реализовано» + коммиты + отмеченные acceptance-критерии).

Что тянет оценку вниз:

1. **Дрейф после двух больших миграций** (BlackHole → audiotee; whisper → gigaam-default). Часть документов обновили (README, BACKLOG), часть — нет (SPEC, PLAN, PLAN_MAC, requirements, .env.example, комментарий в config.py, куски CLAUDE.md).
2. **requirements-windows.txt фактически сломан относительно документации**: не содержит `gigaam` (дефолтный движок!), `static-ffmpeg`, пинит `pyannote.audio==3.3.2` вместо задокументированной 4.0.4, зато содержит неиспользуемый `anthropic`. Новичок по README на Windows проект **не поднимет**.
3. **README не рассказывает** ни как установить Chrome-расширение (шаги есть только в SPEC), ни что на HuggingFace нужно принять условия трёх gated-репозиториев, ни сколько нужно VRAM. Troubleshooting-раздела нет.
4. Три последних фичи (merge встреч, «Не записывать» = stop+discard, удаление prompt-файлов после этапа) не отражены нигде, а пункт CLAUDE.md про prompt-файлы теперь прямо противоречит коду.

---

## 1. Расхождения дока ↔ код

| # | Утверждение в доке | Факт в коде | Где исправить |
|---|---|---|---|
| 1 | CLAUDE.md «Структура»: `app/user_actions.py` («лежит в app/») и текст «`app.user_actions.log_action`» | Файл лежит в **`app/ui/user_actions.py`** (модуль `app.ui.user_actions`; имя *логгера* — `app.user_action`) | CLAUDE.md §Структура, §Логирование действий |
| 2 | CLAUDE.md «Структура»: в `storage/` только `db.py`, `file_manager.py`; в `capture/` не упомянут `source_selector.py` | Есть **`app/storage/meetings_repo.py`** (репозиторий окна «Все совещания», merge встреч) и `app/capture/source_selector.py` | CLAUDE.md §Структура |
| 3 | CLAUDE.md: автодетект claude CLI на Mac — `~/Library/Application Support/Claude/claude-code-vm/*/claude` | config.py **намеренно НЕ использует** этот путь (комментарий: «это Linux ARM ELF (VM-binary), не Mac-исполняемый»). Реальные кандидаты: `~/.local/bin/claude`, `~/.claude/local/claude`, `/opt/homebrew/bin/claude`, `/usr/local/bin/claude` | CLAUDE.md §Переменные окружения; тот же стейл в PLAN_MAC.md («Находки», CP2) |
| 4 | CLAUDE.md «Стек»: «WAV-чанки **10 сек**» | `config.CHUNK_DURATION_SEC = 30` | CLAUDE.md §Стек |
| 5 | CLAUDE.md «Диагностика»: «H1–H8: WASAPI, **BlackHole**, whisper…» и «Тест аудио-захвата BlackHole: `python test_blackhole.py`» | В setup.py H2 = **Core Audio Tap**, не BlackHole. Файла **`test_blackhole.py` не существует** (при этом Mac-раздел CLAUDE.md говорит «остаётся test_blackhole.py для диагностики») | CLAUDE.md §Диагностика и §Mac-специфичные решения |
| 6 | CLAUDE.md: «Подробности в docs/BACKLOG.md — список … с ID (**B1–B14**)» | В BACKLOG.md уже есть **B15 и B16** | CLAUDE.md §Бэклог |
| 7 | CLAUDE.md «Pipeline/БД»: «LLM-промпты **сохраняются** в `*_analysis_prompt.md`/`*_followup_prompt.md` для ручного перезапуска» (без оговорок) | Коммит `002cb4d`: prompt-файл **удаляется после успешного завершения этапа**. Сохраняются только для незавершённых | CLAUDE.md §Pipeline/БД; README §Структура документов (стрелки «промпт для ручного запуска») |
| 8 | CLAUDE.md, README: «pyannote.audio **4.0.4/4.x** (CUDA на Win)» | **requirements-windows.txt пинит `pyannote.audio==3.3.2`** — версия, которую PLAN_MAC.md сам называет несовместимой с huggingface_hub ≥ 0.23 | requirements-windows.txt (обновить до 4.0.4) |
| 9 | CLAUDE.md, README: GigaAM — **default** движок на Windows; стек «ffmpeg: через `static-ffmpeg` pip-пакет» | В requirements-windows.txt **нет ни `gigaam`, ни `static-ffmpeg`**; зато есть `anthropic>=0.25.0`, хотя «Anthropic API намеренно не используется» | requirements-windows.txt |
| 10 | requirements-mac.txt: «Предварительно: **brew install blackhole-2ch**», «Захват системного аудио (через BlackHole…)» | BlackHole не нужен с миграции на audiotee (коммит `634e88d`); sounddevice теперь только для микрофона | requirements-mac.txt, комментарии |
| 11 | .env.example: `ANTHROPIC_API_KEY=sk-ant-...` | Ключ нигде не читается; противоречит принципиальному решению «только CLI». Отсутствуют документированные `TRANSCRIPTION_ENGINE` и `GIGAAM_MODEL` | .env.example |
| 12 | config.py, комментарий к движку: «"whisper" — faster-whisper (**default**, …)» | Строкой ниже: `TRANSCRIPTION_ENGINE = os.getenv(..., "gigaam")` — default **gigaam**. Комментарий противоречит собственному коду | config.py:68–73 |
| 13 | README (macOS < 14.4): «используйте legacy-фолбэк с BlackHole. См. `app/capture/audio_capture.py` historical **`_capture_blackhole`**» | Функции `_capture_blackhole` в коде **нет** (вычищена при миграции); фолбэка для macOS < 14.4 не существует | README §Захват системного звука (macOS) |
| 14 | README: «Python **3.11+**»; CLAUDE.md: «Python 3.10/3.11»; research README: «Python 3.10» | Реальный Windows-runtime — 3.10 (все `__pycache__/*.cpython-310.pyc`); код требует минимум 3.10 (`str \| None`) | Унифицировать: «3.10+ (на Mac — 3.11 из brew)» |
| 15 | docs/TRANSCRIBE_FILE.md: «`pip install -r requirements.txt`» | Файла **`requirements.txt` не существует** — есть `requirements-windows.txt` / `requirements-mac.txt` | TRANSCRIBE_FILE.md §Требования |
| 16 | docs/TRANSCRIBE_FILE.md: «subprocess `transcribe_worker.py` грузит **whisper-large-v3**», «тот же whisper+pyannote pipeline» | `tools/transcribe_file.py` вызывает `get_backend()` → уважает `TRANSCRIPTION_ENGINE`, т.е. **по умолчанию GigaAM** на Windows | TRANSCRIBE_FILE.md §Что внутри, §Параметры whisper |
| 17 | docs/SPEC.md §Технологии: «Смысловой анализ … **Claude API (`anthropic` SDK)**», «Захват (Mac): sounddevice + **BlackHole**», транскрипция Win — только faster-whisper; toast «Плохо слышно» | Реальность: claude CLI subprocess (API запрещён); audiotee; GigaAM default; toast удалён (B4 ✅, коммит `7fa3ffe`) | SPEC.md — пометить как исторический или обновить таблицу |
| 18 | docs/PLAN.md §Структура: `faster_whisper.py`, `mlx_whisper.py`, `diarization/pyannote.py`, `SPEC.md` в корне | Реальные имена: `faster_whisper_backend.py`, `mlx_whisper_backend.py`, `pyannote_diarizer.py` (+ workers); SPEC — в `docs/` | PLAN.md (или пометка «исторический план») |
| 19 | docs/PLAN_MAC.md CP1: «[x] Создать **`start_mac.sh`**»; CP5 — установка BlackHole/Multi-Output | `start_mac.sh` в репозитории **нет** (заменён `create_mac_app.sh`); CP5 полностью superseded миграцией на audiotee | PLAN_MAC.md — пометка «выполнено/superseded» |
| 20 | CLAUDE.md/README: audiotee требует «macOS **14.4+**» | vendor/audiotee/README.md: «macOS **14.2** or later» | Уточнить одну цифру (если 14.4 — из-за конкретного API, написать почему) |
| 21 | tray.py module-docstring: схема меню без пункта «Все совещания…» | Пункт «Все совещания…» есть (tray.py:241) | docstring app/ui/tray.py |
| 22 | LICENSE (Unlicense, весь репозиторий — public domain) | `vendor/audiotee` — **чужой MIT-код** (Nick Payne); в vendor нет файла LICENSE, COMMIT.txt — голый SHA без URL upstream | Добавить vendor/audiotee/LICENSE + строку об upstream в COMMIT.txt; оговорка в корневом LICENSE/README |

Отдельно (не расхождение, но неточность): CLAUDE.md называет `install_host.py` регистрацией в «Mac **LaunchAgents**» — на самом деле манифест пишется в `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/` (docstring самого install_host.py это описывает верно во второй строке, но первая строка про LaunchAgents вводит в заблуждение).

---

## 2. Полнота для новичка (README-путь «с нуля»)

**Windows — не взлетит по README.** Блокеры и пробелы:

1. **requirements-windows.txt неполон** (см. таблицу, #8–9): после `pip install -r requirements-windows.txt` не будет ни gigaam (default!), ни torch/CUDA (упомянут только комментарием), ни static-ffmpeg, ни pyannote нужной версии. Нужен либо честный requirements, либо раздел «Установка ML-стека» с точными командами.
2. **Chrome-расширение**: README говорит «Chrome с установленным расширением» и даёт только `python app/extension/install_host.py`. Как поставить само расширение (chrome://extensions → Developer mode → Load unpacked → `app/extension/chrome`), как передать ID (`python -m app.extension.install_host --update-id <ID>` — флаг в коде есть), что нужен полный перезапуск Chrome — всё это есть только в SPEC.md/PLAN.md, куда новичок не пойдёт. Не сказано и что на Windows нужно собрать `for_meets_host.exe` через PyInstaller (install_host.py упадёт с FileNotFoundError, если `dist/` нет; в репо exe закоммичен — но это стоит проговорить).
3. **HuggingFace gated-модели**: README упоминает «токен с доступом к gated-репозиториям», но не говорит, что нужно зайти на HF и **принять условия** каждого из трёх репо (`pyannote/speaker-diarization-3.1`, `segmentation-3.0`, `speaker-diarization-community-1` — список есть только в PLAN.md, в «Выводах по установке»).
4. **Железо**: нигде в README нет требований VRAM. По PLAN/research: whisper-large-v3 ≈ 6 GB, GigaAM ≈ 0.7 GB, тестировалось на 12 GB (RTX 3060/4070 class). TRANSCRIBE_FILE.md говорит «≥6 GB» — единственное место.
5. **Troubleshooting-раздела нет.** Готовый материал уже написан — «Выводы по установке (Windows)» в PLAN.md (CUDA DLL в PATH, Defender vs PyInstaller --onefile, перезапуск Chrome, WinError 2) и Mac-находки в PLAN_MAC.md. Их стоит вынести/слинковать из README.
6. «`python setup.py`» как диагностика — хорошая штука, но README о ней **вообще не упоминает** (только CLAUDE.md и SPEC). Для новичка это лучший первый шаг.

**Mac** — заметно лучше (раздел свежий, permissions расписаны), но: нет упоминания `python setup.py`, и совет для macOS < 14.4 ведёт на несуществующую функцию (#13).

**Олег через полгода** — CLAUDE.md вытянет, но споткнётся о те же requirements и о неверные пути (user_actions, claude-code-vm).

---

## 3. Качество CLAUDE.md как инструкции для AI-агента

**Сильные стороны:** плотный, конкретный, с «минами» (SIGABRT-паттерны на Mac, cudnn-конфликт, `sqlite3.Row → dict()`, запрет `communicate()`). Правила git-политики однозначны и продублированы с глобальным CLAUDE.md без противоречий. Раздел «Mac-специфичные решения» — лучший в файле.

**Проблемы:**
- **Фактические ошибки** (таблица #1–7) — для AI-агента это хуже, чем для человека: агент доверчиво пойдёт в `app/user_actions.py` и в `claude-code-vm`.
- **Дубли**: Core Audio Tap описан трижды (Описание, Стек, Mac-решения), «Anthropic API не используется» — дважды (Стек + config.py). Дубли не противоречат друг другу — терпимо, но при обновлении легко рассинхронизировать (что уже случилось с чанками 10/30 сек).
- **Нет упоминания** `meetings_repo.py`, merge-функциональности, TCP-моста native host ↔ app (коммит `829dc21`, `BRIDGE_HOST/BRIDGE_PORT` в main.py — архитектурно значимая деталь: native host общается с приложением через локальный TCP, в CLAUDE.md модель потоков говорит только «Native host thread: daemon, читает сообщения Chrome»).
- Раздел «Диагностика» отсылает к несуществующему `test_blackhole.py` — агент попытается запустить.

---

## 4. Статус PLAN-файлов и BACKLOG

| Файл | Статус фактический | Пометка в файле | Рекомендация |
|---|---|---|---|
| docs/PLAN.md | Этапы 0–5 ✅, Этап 6 частично (ошибки pipeline = B13, e2e = B14, тестов нет). Таблица гипотез закрыта кроме H8 | Нет общей пометки | Дописать шапку «Исторический план, выполнен кроме Этапа 6 (см. B13/B14)»; либо перенести живые остатки в BACKLOG и заархивировать (docs/archive/) |
| docs/PLAN_MAC.md | Выполнен; CP5 (BlackHole) superseded audiotee; 2 незакрытых чекбокса (pystray-проверка CP3 — де-факто сделана, автодетект Meet CP7) | Нет | Пометить «выполнен, аудио-часть superseded (см. BACKLOG “Захват без BlackHole”)», закрыть/актуализировать 2 чекбокса |
| docs/PLAN_meetings_window.md | Выполнен | ✅ Есть: «Статус: реализовано» + коммиты | Эталон. Ничего не делать |
| docs/SPEC.md | ТЗ образца начала проекта; технологии устарели (API/BlackHole/whisper-only), часть UX-решений пересмотрена (toast убран) | Нет | Пометить «исходное ТЗ, актуальную картину см. README/CLAUDE.md» |

**BACKLOG.md — в целом актуален** (обновлялся 2026-06-18). Выборочная сверка с кодом:
- B7 (запрет сна): `SetThreadExecutionState` в коде отсутствует — корректно числится открытым. ✓
- B2 («Остановить сейчас» в попапе): в коде строки нет — открыт корректно. Но коммит `438dd13` («Не записывать эту встречу» теперь останавливает и **удаляет** запись) закрывает соседний сценарий — стоит приписать к B2 примечание, чтобы не сделать дважды.
- B16 (include-PIDs для audiotee): не реализовано (`mac_tap.py` без include_pids в вызове) — корректно открыт. ✓
- B15, B1, B3–B5 помечены ✅ — подтверждается кодом/историей. ✓
- **Пробел**: раздел «Сделано вне исходного бэклога» не пополнен фичами после 2026-06-18 — merge двух встреч (`af58bc2`, есть `meetings_repo.merge_meetings`), удаление prompt-файлов (`002cb4d`), stop+discard (`438dd13`).

---

## 5. Docstrings и комментарии в коде (выборка 8 модулей)

Уровень **выше среднего**, единый стиль (русский, «зачем», не «что»):

- `app/main.py` — отличный: жизненный цикл + сценарии Meet/manual; полезные inline-комментарии (faulthandler, flush-handler).
- `app/ui/user_actions.py` — образцовый: мотивация, формат лога, даже grep-команда.
- `app/capture/mac_tap.py` — образцовый: сравнение с BlackHole, формат PCM, архитектура stdout/stderr.
- `app/storage/db.py` — хорошо: перечень таблиц; сама схема прокомментирована поколоночно — это де-факто и есть документация формата БД.
- `app/processing/pipeline.py`, `app/transcription/backend.py`, `setup.py`, `app/extension/install_host.py` — внятные докстринги с назначением и ссылками.
- Минусы: `app/ui/tray.py` — docstring со схемой меню устарел (нет «Все совещания…», нет «Скрыть из списка»); `config.py` — комментарий про default-движок противоречит коду (#12); `install_host.py` — «Mac LaunchAgents» в первой строке (неверно, см. выше).

---

## 6. Чего нет совсем

1. **ARCHITECTURE.md с диаграммой.** Сейчас архитектура размазана по CLAUDE.md + docstrings. Одна схема (Chrome ext → native host → TCP bridge → App → capture threads → pipeline → workers → claude CLI) закрыла бы 80% онбординга. Диаграмму потоков/процессов (3 worker-процесса, что в каком venv/CUDA) — туда же.
2. **CHANGELOG.** История восстановима только по `git log` (сообщения хорошие — материал для CHANGELOG уже есть).
3. **Описание схемы БД** вне docstring db.py: статусная машина jobs (`pending → processing → transcribed → analyzed → done/error`) описана в CLAUDE.md, но колонки/миграции (dismissed, *_duration_sec, ended_at) — только в PLAN_meetings_window.md и в коде.
4. **Инструкция по обновлению моделей**: как сменить `WHISPER_MODEL`, где кэш HF, что перекачается при смене `GIGAAM_MODEL`, как обновить pyannote (и что диаризация завязана на принятие новых gated-условий).
5. **Документация Chrome-расширения**: manifest.json/background.js без README; протокол сообщений (meet_started/meet_ended, TCP-мост) не описан нигде.
6. **Troubleshooting/FAQ** (см. §2, п.5).
7. Опционально: заметка о приватности/хранении записей (data/ содержит реальные рабочие встречи; в README не сказано, что data/ в .gitignore и как чистить старое аудио — фича «Удалить аудио старше 14 дней» упомянута только в PLAN_meetings_window).

---

## Топ-5 приоритетов

1. **Починить requirements-windows.txt** — добавить `gigaam`, `static-ffmpeg`, поднять `pyannote.audio` до 4.0.4, убрать `anthropic`, вписать команду установки torch+CUDA; это единственный пункт, который реально блокирует установку с нуля.
2. **Синхронизировать CLAUDE.md с кодом** — путь `app/ui/user_actions.py` и `meetings_repo.py` в структуре, убрать `claude-code-vm` из Mac-автодетекта (заменить реальными путями из config.py), чанки 30 сек, убрать `test_blackhole.py`/BlackHole из «Диагностики», B1–B16, оговорка об удалении prompt-файлов после успешного этапа, упомянуть TCP-мост native host.
3. **Дописать README до самодостаточного quick-start** — пошаговая установка Chrome-расширения (+ `--update-id`), принятие условий трёх gated-репо на HF, требования VRAM, `python setup.py` как первый диагностический шаг, раздел Troubleshooting (перенести «Выводы по установке» из PLAN.md); убрать ссылку на несуществующую `_capture_blackhole`.
4. **Разметить исторические документы** — шапки «выполнено/историческое» в PLAN.md, PLAN_MAC.md, SPEC.md (по образцу PLAN_meetings_window.md); почистить .env.example (убрать `ANTHROPIC_API_KEY`, добавить `TRANSCRIPTION_ENGINE`/`GIGAAM_MODEL`); исправить комментарий про default-движок в config.py.
5. **Закрыть документационный долг по свежим фичам и завести ARCHITECTURE.md + CHANGELOG** — merge встреч, stop+discard, удаление prompt-файлов → в BACKLOG «Сделано» и README-статус; одна диаграмма процессов/потоков; CHANGELOG из git log.
