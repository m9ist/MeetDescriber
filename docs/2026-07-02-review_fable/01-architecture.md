# Архитектурное ревью MeetDescriber (for_meets)

**Дата:** 2026-07-02
**Ревьюер:** Claude (Fable 5), зона — архитектура
**Объём:** config.py, app/main.py, app/capture/*, app/transcription/*, app/diarization/*, app/processing/*, app/storage/*, app/extension/*, app/ui/tray.py + по диагонали остальной UI, prompts.py

---

## Краткое резюме

**Оценка: 7/10.**

Это зрелый, прагматичный инженерный проект уровня «личный production-инструмент». Сильнейшая сторона — глубина понимания платформенных ограничений: subprocess-изоляция cuDNN-конфликтов, ручной NSApp-цикл на Mac, `_schedule`-очередь вместо `root.after`, PostMessage-refresh для pystray — всё это решения реальных, тяжело диагностируемых проблем, и все они *задокументированы прямо в коде* (комментарии объясняют «почему», а не «что»). Пайплайн идемпотентен, статусы восстанавливаются после падений, worker-протокол (stdout=JSON, stderr=логи+PROGRESS) прост и работоспособен.

Оценка не выше из-за системных долгов: `App` в main.py — god-object с сырым SQL, размазанным по UI-обработчикам при живом repo-слое; идентичность документов построена на `дата+название` файла — две встречи с одинаковым названием в один день **молча перезаписывают документы друг друга**; у ML-worker'ов нет ни таймаутов, ни watchdog'а, ни guard'а от двойного запуска одного job; код запуска subprocess-worker'ов скопирован трижды почти дословно; автоматических тестов нет вообще. Для однопользовательского инструмента это терпимо, но именно эти места сломаются первыми при любом росте (второй пользователь, batch-обработка, новый источник записи).

---

## 1. Общая структура и разбиение на модули

### 🟢 Сильное: слоистая структура каталогов в целом честная

`capture / transcription / diarization / processing / storage / extension / ui` — границы осмысленные, направление зависимостей почти всюду правильное: `pipeline.py` не знает про tkinter (LLM-диалог инжектится колбэком `ask_claude` — `app/processing/pipeline.py:203`, `app/main.py:495-511`), `native_host.py` намеренно stdlib-only (`app/extension/native_host.py:20-22`), `meetings_repo.py` явно декларирует «Не зависит от UI» (`app/storage/meetings_repo.py:5`).

### 🟡 App в main.py — god-object с сырым SQL при живом repo-слое

`App` (app/main.py:76-714) совмещает пять ролей: TCP-сервер моста (`_run_bridge_server`, :185-245), lifecycle записи (`_start_session`/`_stop_and_offer_processing`/`_discard_current_session`, :365-480), CRUD заданий (`_create_job`, `_delete_job_files`, `_on_delete_all_pending`, :482-646), оркестрацию пайплайна (`_on_process_job`, :513-545) и платформенный event loop (:136-181).

Хуже сам объём — то, что SQL живёт в двух местах одновременно. В `app/storage/meetings_repo.py` есть аккуратный data-access слой, но main.py содержит **11 мест с сырым SQL**: `_handle_meet_title` (:316-320), `_start_session` (:376-381), `_stop_and_offer_processing` (:423-436), `_discard_current_session` (:471-474), `_create_job` (:483-491), `_on_process_job` (:518-523), `_on_edit_job` (:550-556), `_delete_job_files` (:591-628), `_on_delete_all_pending` (:638-645), `_refresh_tray_jobs` (:663-679). Схема БД по факту размазана по трём файлам.

**Почему это проблема:** дублируются целые сценарии. `App._delete_job_files` (main.py:586-628) и `meetings_repo.delete_meeting` (:120-163) делают одно и то же с тонкими различиями (repo дополнительно чистит speakers и сверяет пути из БД с вычисленными; main.py — нет, зато чистит prompt-файлы). Правка логики удаления в одном месте не попадёт во второе — классический источник рассинхрона.

**Рекомендация:** перенести весь SQL из main.py в `meetings_repo` / новый `jobs_repo` (функции уровня `create_job_for_session`, `list_tray_jobs`, `delete_job_with_files`, `update_session_title`). Дальше можно (не обязательно) выделить из App класс `RecordingController` (start/stop/discard + capture) и `MeetBridge` (TCP-сервер + обработчики Chrome-событий) — тогда App останется композиционным корнем на ~200 строк.

### 🟡 Трижды скопированный subprocess-runner

`FasterWhisperBackend.transcribe` (app/transcription/faster_whisper_backend.py:42-101), `GigaAMBackend.transcribe` (app/transcription/gigaam_backend.py:36-93) и `PyannoteDiarizer.diarize` (app/diarization/pyannote_diarizer.py:39-72) — это один и тот же код: `Popen([sys.executable, "-u", worker, path], ...)` → stderr-тред с парсингом `PROGRESS:` → `stdout.read()` → `wait()` → `json.loads`. Различия — только путь к worker'у и парсинг результата.

**Почему это проблема:** любая починка протокола (таймаут, kill при cancel, включение хвоста stderr в текст ошибки) требует трёх синхронных правок. Комментарий про гонку `communicate()` уже пришлось продублировать во все три файла.

**Рекомендация:** один модуль `app/workers/runner.py` с функцией `run_worker(worker_path, args, on_progress=None, timeout=None, cancel_event=None) -> dict`. Бэкенды сведутся к маппингу JSON → dataclass'ы. Это одновременно и точка, куда естественно добавить таймаут/watchdog (см. раздел 3).

### 🟢 Мёртвый модуль и дрейф документации

- `app/capture/source_selector.py` — пустой файл (0 байт). Либо удалить, либо это заготовка — тогда TODO-комментарий.
- CLAUDE.md указывает `app/user_actions.py`, реально файл лежит в `app/ui/user_actions.py`. Мелочь, но CLAUDE.md — это инструкция для LLM-агентов, дрейф путей ведёт к лишним поискам.
- Docstring в `app/ui/dialogs.py:170-172` описывает ClaudeManualDialog как «ручной запуск когда CLI недоступен», хотя по CLAUDE.md это теперь основной путь. Комментарий отстал от архитектурного решения.

### 🟡 LLM-вызов живёт в UI-слое

Запуск `claude -p` subprocess'ом находится внутри `ClaudeManualDialog._on_run` (app/ui/dialogs.py:321-358). То есть бизнес-операция «выполнить LLM-этап» реализована в обработчике кнопки tkinter. `analysis.py`/`followup.py` при этом лезут в приватную функцию конфига `config._find_claude_cli()` (app/processing/analysis.py:97, app/processing/followup.py:72) и передают CLI-путь **в диалог**, который его исполняет.

**Почему это проблема:** LLM-этап невозможно выполнить без UI (headless-режим, batch «обработай все pending ночью», тесты). Логика ретраев/таймаутов CLI оказывается свойством окна, а не пайплайна.

**Рекомендация:** выделить `app/processing/claude_runner.py` с функцией `run_claude(prompt_path, cwd, timeout) -> Result`; диалог зовёт её, пайплайн при желании тоже — напрямую. Заодно `_find_claude_cli` переименовать в публичную `find_claude_cli`.

---

## 2. Модель потоков и конкурентность

### 🟢 Сильное: Mac-модель `_schedule` — образцовое решение

`App._schedule` (app/main.py:344-361) + дренаж `_mac_queue` в ручном цикле (:157-163) — правильный ответ на реально существующую проблему GIL/NSMenu (SIGABRT `_Py_FatalError_TstateNULL`). Единая точка маршалинга в main thread, прокинутая во все подсистемы (`notifications.set_schedule`, `dialogs.set_schedule`, `SpectrumWidget`, `ProcessingStatusWindow`). Аналогично `ForMeetsTray._refresh` через PostMessage `_WM_TRAY_REFRESH` (app/ui/tray.py:153-181) — корректная передача обновления в поток-владелец HWND/HMENU. Это лучше, чем в большинстве tkinter-приложений, которые я видел.

### 🔴 Нет guard'а от параллельного запуска пайплайна на одном job

`App._on_process_job` (app/main.py:513-545) не проверяет текущий статус job'а и не ведёт реестра запущенных пайплайнов. `run_transcription` выставляет `status='processing'` (app/processing/pipeline.py:253), но никто этот статус не читает перед стартом. Tray-меню обновляется только **после** завершения пайплайна (main.py:543), т.е. всё время обработки job виден в «Необработанных» и кликается повторно; то же самое доступно из meetings_window.

**Сценарий:** пользователь кликает «Обработать», не видит мгновенной реакции (загрузка pyannote — десятки секунд), кликает ещё раз → два diarize_worker'а конкурируют за VRAM (у каждого — pyannote на CUDA), два потока пишут в одни и те же файлы `merged.wav` / `diarization.json` / `*_transcription.md` и в одну строку jobs. Итог — от OOM на GPU до битого документа.

**Рекомендация:** в `_on_process_job` — множество `self._active_jobs: set[int]` (или проверка `status == 'processing'` в БД) с ранним выходом + немедленный `_refresh_tray_jobs()` после выставления processing, чтобы job исчез из «Необработанных» сразу.

### 🟡 Разделяемое состояние App читается/пишется из bridge-треда без синхронизации

Обработчики Chrome-событий работают в тредах `ext-bridge-conn`. Большинство корректно уходит в `_schedule`, но `_handle_meet_title` (app/main.py:298-321) прямо из bridge-треда: читает `self._current_tab_id`, `self._current_session_id`, `self._current_title`, `self._capture.is_recording` и пишет в БД, — в то время как main thread в `_stop_and_offer_processing` (:401-445) эти же поля обнуляет. TOCTOU: между проверкой `self._current_session_id is None` и `UPDATE sessions` сессия может быть остановлена и даже удалена (`_discard_current_session` делает `DELETE FROM sessions`). UPDATE по несуществующему id безвреден, но UPDATE по id **следующей** сессии, стартовавшей в этот зазор — уже реальная порча данных (title чужой встречи).

Аналогично `_handle_meet_started` читает `self._capture.is_recording` в bridge-треде (:270) до `_schedule(show)` — двойная проверка расходится по времени с фактическим стартом.

**Рекомендация:** завернуть тело `_handle_meet_title` в `self._schedule(...)`, как это уже сделано для started/ended — тогда всё состояние сессии трогает только main thread, и никакие локи не нужны. Это дешёвая правка, устраняющая целый класс гонок.

### 🟡 ask_claude: таймауты не согласованы, очередь-сирота после timeout

Цепочка: пайплайн-тред блокируется на `result_q.get(timeout=1800)` (app/main.py:509), а кнопка «Запустить» внутри диалога выполняет CLI с `timeout=300` (app/ui/dialogs.py:334). Два несогласованных лимита:
1. Анализ длинной встречи через `claude -p` легко идёт дольше 5 минут → subprocess.TimeoutExpired → пользователь видит «Ошибка», хотя ждать было можно.
2. Если пользователь ушёл на 30+ минут, `queue.Empty` вылетает из `ask_claude`, ловится общим `except Exception` в `run()` (main.py:538-540) → job уходит в error, **но окно диалога остаётся открытым**: пользователь возвращается, жмёт «Этап выполнен» — put уходит в мёртвую очередь, ничего не происходит, никакой обратной связи.

**Рекомендация:** (a) поднять/убрать CLI-таймаут в диалоге (или сделать его конфигом); (b) при `queue.Empty` закрывать диалог через `_schedule`; (c) лучше — вообще убрать 1800-таймаут: диалог модален по смыслу, очередь получит значение когда пользователь решит.

### 🟡 ThreadPoolExecutor качества: течёт по экземпляру на сессию и мешает выходу

`AudioCapture.__init__` создаёт `ThreadPoolExecutor(max_workers=2)` (app/capture/audio_capture.py:72), но `stop()` его не закрывает, а App создаёт новый `AudioCapture` на каждую запись (app/main.py:386). Потоки executor'а **не daemon** — каждая сессия навсегда оставляет 2 живых потока; на Windows при выходе `concurrent.futures` через atexit дожидается завершения запущенных `_evaluate_quality` (whisper-tiny на CPU — может быть десятки секунд), т.е. «Выход» из трея подвисает. На Mac это маскируется `os._exit(0)`.

**Рекомендация:** `self._executor.shutdown(wait=False, cancel_futures=True)` в `AudioCapture.stop()`; либо один переиспользуемый executor уровня модуля.

### 🟢 `root.after()` из чужих потоков на Windows

На Windows `_schedule` — это `root.after` (app/main.py:361), вызываемый из bridge-треда, pipeline-треда, pystray-треда, capture-колбэков. Официально tkinter не thread-safe; на практике Tcl, собранный с threads (стандарт для Windows-сборок CPython), это переживает, и весь Mac-опыт проекта показывает, что автор про класс проблем знает. Отмечаю как осознанный риск: если появятся редкие `RuntimeError: main thread is not in main loop` на Windows — вот источник. Возможное усиление — та же очередь `SimpleQueue` + периодический `after`-дренаж и на Windows (унификация с Mac-путём).

### 🟢 Мелочи конкурентности

- `_evaluate_quality` — `except Exception: pass` (app/capture/audio_capture.py:630-631): молча гасит всё, включая отсутствие модели/битый WAV. Нарушает ваш же принцип «Log Fully, Never Swallow» из CLAUDE.md. Достаточно `log.debug(..., exc_info=True)`.
- Mac-цикл (app/main.py:147-168) крутится с шагом 0.01с даже в полном простое — ~100 просыпаний/сек, заметный фоновой CPU на ноутбуке. Можно поднять `untilDate` до 0.05-0.1с, когда нет записи.
- `_capture_mac`: `threading.Event().wait(0.1)` создаёт новый Event на каждой итерации (app/capture/audio_capture.py:487) — работает, но `time.sleep(0.1)` честнее.
- Двойной `stop()` (main thread из `_stop_and_offer_processing` + pystray-тред из `_on_quit`, app/main.py:710-714) — оба сначала проверяют `is_recording`; при одновременном вызове оба пройдут проверку. `AudioCapture.stop()` почти идемпотентен, но `self._thread = None` из двух потоков — гонка. Дёшево чинится: `_on_quit` тоже через `_schedule`.

---

## 3. Subprocess-изоляция ML-этапов

### 🟢 Сильное: сама изоляция и протокол

Решение «три worker-процесса вместо одного адресного пространства» — правильный и единственно надёжный ответ на конфликт бандлированных cudnn64_9.dll, и он подробно объяснён в заголовке каждого файла (app/diarization/diarize_worker.py:1-12, app/transcription/transcribe_worker.py:62-69). Отдельно отмечу:
- `sys.modules["torch"] = None` до импорта ctranslate2 (transcribe_worker.py:16) — хирургически точный хак с проверкой-подтверждением в логе (:97);
- faulthandler + `dump_traceback_later(30)` в fault-лог (transcribe_worker.py:32-37) — диагностика зависаний встроена заранее;
- partial-сохранение сегментов каждые 10 сек (transcribe_worker.py:100-183) — задел на восстановление после краша;
- осознанный отказ от `communicate()` с объяснением гонки stderr-ридеров (faster_whisper_backend.py:73-75) — глубже, чем обычно копают.

VRAM-освобождение через смерть процесса вместо ручного unload — вернейший способ, и `unload_model()` честно превращён в no-op.

### 🟡 Нет таймаутов и watchdog'а: зависший worker = вечно висящий пайплайн

Во всех трёх раннерах `proc.stdout.read()` и `proc.wait()` без таймаута (faster_whisper_backend.py:76-78, gigaam_backend.py:68-70, pyannote_diarizer.py:62-64). GPU-стек умеет зависать намертво (сам проект это знает — `dump_traceback_later` добавлен именно поэтому). При зависании: pipeline-тред блокирован навсегда, статус-окно крутит спиннер, job застрял в processing до перезапуска приложения, GPU занят. «Отмена» не помогает — `cancel_event` проверяется только между этапами (pipeline.py:239-241, 288, 306) и не убивает subprocess; комментарий в status_window.py:242-244 это честно признаёт.

**Рекомендация:** в общем runner'е (см. раздел 1) — (a) вычислять таймаут от длительности аудио (например, `max(600, duration*2)` при 8-28x realtime — щедрый запас); (b) при `cancel_event.is_set()` — `proc.terminate()`/`kill()` и `PipelineCancelledError`; (c) при таймауте — kill + RuntimeError с хвостом stderr.

### 🟡 Осиротевшие worker'ы при падении родителя

Если tray-приложение убито (крэш, kill, Windows-shutdown) во время транскрипции, дочерний Python с моделью на CUDA продолжает жить и держать несколько ГБ VRAM — на Windows дочерние процессы не умирают с родителем автоматически. Следующий запуск транскрипции получит меньше VRAM и, возможно, OOM.

**Рекомендация:** на Windows — Job Object с `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (~15 строк ctypes, вешается на Popen-handle); на Mac worker'ов нет (mlx in-process). Альтернатива-минимум: worker периодически проверяет `os.getppid()`.

### 🟢 Хрупкость stdout-канала и мелочи worker'ов

- Результат — единственный `print(json.dumps(...))` в stdout. Любая сторонняя библиотека, печатающая в stdout (у gigaam/torch такое случается: прогресс-бары загрузки весов и т.п.), ломает `json.loads` всего результата. gigaam_worker уводит логи в stderr, но stdout третьих библиотек не контролирует. Дёшево укрепить: worker пишет JSON в последнюю строку с маркер-префиксом (`RESULT:{...}`), либо в отдельный файл, путь к которому передан аргументом.
- Ошибка воркера доносится до пользователя как «завершился с кодом 1» (gigaam_backend.py:73-75) — без последних строк stderr, которые уже прочитаны в лог. Стоит буферизовать хвост stderr (deque на 20 строк) и включать в RuntimeError.
- `tempfile.mktemp` в gigaam_worker.py:159 — deprecated и гоночный; `NamedTemporaryFile(delete=False)` или `mkstemp`.
- partial-JSON транскрипции пишется, но **никем не читается** (grep по проекту: только transcribe_worker.py). Механизм восстановления начат и брошен — либо доделать (пайплайн подхватывает partial при рестарте), либо убрать, чтобы не создавать ложное ощущение защиты.

---

## 4. Поток данных и состояние

### 🟢 Сильное: идемпотентность и восстановление статусов

Каждый этап проверяет наличие своего артефакта перед запуском (pipeline.py:250, 339, 366), diarization кэшируется в JSON (pipeline.py:182-197, 266-268), `merge_chunks` не пересобирает существующий merged.wav (file_manager.py:76-77), `init_db` сбрасывает зависшие processing → pending (db.py:67-69). Перезапуск упавшего пайплайна доводит job до конца, не повторяя дорогие GPU-этапы. Миграции колонок через `PRAGMA table_info` (db.py:71-85) — просто и достаточно для SQLite.

### 🔴 Идентичность документов = `дата + название`: коллизии молча уничтожают данные

`get_doc_paths(title, started_at)` (app/storage/file_manager.py:26-43) строит имена файлов только из даты и sanitized-названия. session_id в имени нет. Последствия:

1. **Две встречи с одним названием в один день** (совершенно реальный кейс: два звонка «Синк с командой», или два авто-названных «Google Meet» подряд) → одинаковые пути. Пайплайн второго job'а видит, что `transcription_path` у него пуст, и **перезаписывает** transcription/analysis/followup первой встречи. Ни ошибки, ни предупреждения.
2. `delete_meeting` (meetings_repo.py:148-152) и `reset_to_stage` (:251-276) удаляют файлы по *вычисленным* путям → удаление одной встречи стирает документы её тёзки.
3. Вся механика `rename_session_docs` + `update_job_paths` (file_manager.py:46-61, db.py:101-117) существует потому, что имя файла — это идентичность; при этом пайплайн, запущенный параллельно с редактированием названия (main.py:547-584 не проверяет, идёт ли обработка), будет писать по старым путям после переименования.

**Рекомендация:** включить session_id в базовое имя: `YYYY-MM-DD_<title>_s<id>_transcription.md` (или каталог `documents/<id>_<title>/`). Это убирает коллизии, упрощает rename (менять только «красивую» часть) и делает delete/reset детерминированными. Единственная миграция — существующие файлы можно не трогать, пути в БД уже хранятся.

### 🟡 Крэш во время записи → сессия-сирота без job

Job создаётся только в `_stop_and_offer_processing` (main.py:429-431). Если приложение упало посреди записи: строка sessions без `ended_at`, чанки на диске, job'а нет. После рестарта `init_db` чинит только статус processing (db.py:67-69); сессия не появится в «Необработанных» (там JOIN по jobs, main.py:663-670) — записанный час аудио тихо лежит мёртвым грузом. В meetings_window она видна (LEFT JOIN, meetings_repo.py:53-54), но со status NULL — обработать её оттуда нечем.

**Рекомендация:** в `init_db` (или при старте App) — «UPDATE sessions SET ended_at=… WHERE ended_at IS NULL» + `INSERT job(pending)` для сессий, у которых есть чанки, но нет job. Три строки, закрывают самый обидный сценарий потери данных (упавшая запись важной встречи).

### 🟡 merge_meetings: файловые операции до транзакции

`merge_meetings` (meetings_repo.py:307-415) сначала физически переносит чанки (:344-372), затем в отдельном подключении правит БД (:379-411). Крэш между ними оставляет гибрид: чанки уже слиты в базовую сессию, но в БД обе сессии живы, причём у поздней папка пуста. Повторный merge упадёт на «нет аудиозаписей». Для однопользовательского инструмента приемлемо, но хотя бы порядок стоит инвертировать частично: сначала БД-операции удаления документов, перенос файлов последним шагом, чтобы неконсистентность была восстановимой повтором.

### 🟢 Статусы — строковые литералы в шести файлах

`'pending' | 'processing' | 'transcribed' | 'analyzed' | 'done' | 'error'` захардкожены в db.py:40, main.py:641/668/677, meetings_repo.py:183/209, tray.py:266-269, pipeline.py. Опечатка в одном фильтре — и job невидим. Рекомендация: `class JobStatus(str, Enum)` в storage/db.py + переиспользование; заодно кортеж `UNPROCESSED = (PENDING, TRANSCRIBED, ANALYZED)` для трёх дублей IN-списка.

### 🟢 get_conn: корректно, с оговорками

Контекст-менеджер (db.py:51-60) коммитит на успехе; на исключении commit пропускается, close откатывает — правильно. Подключение на операцию из разных потоков — валидный паттерн для SQLite; дефолтный busy-timeout 5с спасает от «database is locked» в типичных пересечениях (pipeline-тред + main thread). FK не включены (`PRAGMA foreign_keys` по умолчанию OFF) — каскады сделаны вручную и последовательно, но включение `foreign_keys=ON` в get_conn обошлось бы в одну строку и ловило бы будущие ошибки.

---

## 5. Кроссплатформенность

### 🟢 Сильное: платформа изолирована в правильных местах

- Захват: `_capture_wasapi` / `_capture_mac` — две ветки внутри одного класса с общим `_process_chunk`/`_mix_audio` (audio_capture.py:112-122). Не strategy-паттерн, но для двух платформ честная и читаемая организация.
- Транскрипция: платформенный выбор скрыт в factory `get_backend()` (backend.py:52-72) — вызывающий код платформы не видит.
- Mac-специфика собрана в выделенные модули: `mac_tap.py`, `mac_window.py`, и знание о ней сосредоточено в `_schedule`. CLAUDE.md-раздел «Mac-специфичные решения» — образцовая документация институциональных знаний.

### 🟡 harden_for_mac применён не ко всем Toplevel

Собственная документация проекта говорит: клик в «X» любого Toplevel на Mac → SIGABRT, поэтому `harden_for_mac` снимает Closable. Применено: ClaudeManualDialog (dialogs.py:311), ProcessingStatusWindow (status_window.py:176-177), MeetingsWindow (meetings_window.py:107-108). **Не применено:** `MeetingStartDialog` (dialogs.py:99-159) и `MeetingEditDialog` (dialogs.py:440-492) — оба обычные Toplevel с системной рамкой и кнопкой закрытия. Пользователь на Mac, отменяющий запись кликом в «X» вместо «Отмена», получает crash всего приложения — причём именно в диалоге, который показывается чаще всех.

**Рекомендация:** вызвать `harden_for_mac(win)` в обоих; ESC уже стоит добавить как альтернативу отмене.

### 🟢 Мелкие протечки платформы

- `_bind_clipboard_shortcuts` (dialogs.py:46-77) опирается на VK-коды «Win + Linux» — на Mac keycode другие и Cmd вместо Ctrl; хоткеи копирования в диалогах на Mac скорее всего не работают (Aqua даёт свои дефолты для Cmd-C/V, так что деградация мягкая).
- `_btn_kw = {...} if config.IS_WINDOWS else {...}` повторён в 4+ местах dialogs.py (:125, 251, 465) — просится в общий хелпер рядом с `notifications._btn_kwargs`, который уже существует (notifications.py:41-53) и делает то же самое.
- `pipeline._set_thread_priority_below_normal` (pipeline.py:41-63) — честный no-op на Mac, нормально.

---

## 6. Конфигурация

### 🟢 В целом прагматично, но config.py — модуль с побочными эффектами

`import config` делает: регистрацию DLL-директорий и мутацию PATH (config.py:13-25), чтение .env (:33), экспорт HF_TOKEN (:43-44), добавление Homebrew в PATH (:51-54). Для приложения это удобно (worker'ы просто импортируют config и получают DLL-пути), но означает, что порядок импортов значим, а импорт config в тестах/скриптах тянет весь этот багаж. Приемлемый компромисс — но стоит держать в голове; вынос side-effects в явную `config.bootstrap()` сделал бы поведение предсказуемым.

### 🟡 Разбросанные магические константы вместо конфига

- `BRIDGE_PORT = 48765` — в native_host.py:33 (осознанно, host stdlib-only; ок).
- `DRIFT_WARN_THRESHOLD_SEC/COOLDOWN` — в audio_capture.py:34-35, а `CHUNK_DURATION_SEC/SILENCE_THRESHOLD_RMS` — в config.py. Однородные настройки захвата разложены по двум файлам.
- Таймауты: 1800с (main.py:509), 300с (dialogs.py:334), 10с join capture (audio_capture.py:105), 10с stream_start (mac_tap.py:157) — ни один не в config, при этом пара 1800/300 конфликтует по смыслу (см. раздел 2).
- `SILENCE_THRESHOLD_RMS = 100` — сильно зависит от микрофона/уровней; кандидат №1 на вынос в .env.

**Рекомендация:** собрать тайминги/пороги в config.py секцией «Timeouts / Capture tuning» — не ради гибкости, а чтобы ревизия таких значений была в одном месте.

### 🟢 Хорошее

`_find_claude_cli` с приоритетом env → платформенные пути → PATH → fallback и предупреждением про Linux-ELF в claude-code-vm (config.py:102-106) — видно, что путь выстрадан. Копирование HUGGINGFACE_TOKEN → HF_TOKEN с комментарием, зачем — тоже.

---

## 7. Расширяемость

### 🟢 Новый транскрипционный движок — легко

`TranscriptionBackend` ABC + dataclass-контракт (backend.py:14-49) + factory — добавление движка это один backend-файл (+worker при необходимости) и ветка в `get_backend`. Дублирование runner-кода (раздел 1) — единственное, что превращает это в copy-paste вместо переиспользования.

### 🟡 Новый источник записи (Zoom/Telegram) — средне, Meet-семантика вросла в App

Знание о Google Meet живёт в App: `_skip_tab_ids`, `_current_tab_id`, `_clean_meet_title`, словарь `_bridge_handlers` с типами meet_* (main.py:83-123, 249-324). БД готова (`source` колонка), `_start_session(source=...)` готов. Но абстракции «источник встречи» (start/stop-события + метаданные) нет — новый источник означает второй набор полей и обработчиков внутри App. Терпимо для второго источника, на третьем понадобится интерфейс `MeetingSource` с колбэками `on_meeting_started/ended/title_changed`.

### 🟡 Новый LLM-этап — shotgun surgery по ~6 файлам

Добавить этап (например, «краткое резюме для рассылки») сегодня значит: колонки `X_path`, `X_duration_sec` + миграция (db.py), копия analysis.py/followup.py, блок в pipeline.py (:337-388 — два уже почти идентичных блока), новый статус + правка IN-списков (main.py, meetings_repo), метки в tray.py:266-273 и status_window._STAGE_LABELS, ветка в reset_to_stage (meetings_repo.py:225-283). Сама пара analysis/followup уже дублирует друг друга на ~80% (сравни analysis.py:77-114 и followup.py:53-89).

**Рекомендация:** декларативный список этапов — `STAGES = [Stage(name, input_key, output_key, build_prompt, status_after), ...]`; pipeline итерируется по нему, reset_to_stage режет по индексу, tray/status берут метки из Stage. Это не «гибкость впрок»: три LLM-этапа фактически уже есть (analysis, followup + описанный в бэклоге потенциал), а дублирование — уже сегодня.

### 🟢 Что уже хорошо для расширения

Идемпотентность по файлам означает, что новые этапы автоматически получают «пропустить если сделано». Prompt-шаблоны вынесены в prompts.py с понятным контрактом переменных — редактирование поведения LLM не требует трогать код.

---

## 8. Технический долг и риски

### 🟡 Нет ни одного автоматического теста

В репозитории нет tests/. При этом значительная часть логики — чистые функции, тестируемые без ML и без tkinter: `_assign_speakers`, `_detect_names`, `_build_speaker_map` (pipeline.py:69-147), `_mix_audio`, `_calc_rms` (audio_capture.py:575-601), `_safe_name`, `merge_chunks` (file_manager.py), `merge_meetings`/`reset_to_stage` на временной БД, парсинг PROGRESS-строк. Именно эти места правятся чаще всего (по истории коммитов — merge встреч, drift, silence filter), и именно микс/выравнивание аудио — то, где регрессию не заметишь до следующей испорченной записи реальной встречи. Час на pytest-скелет + десяток тестов на перечисленное — лучшая инвестиция из всего этого отчёта.

### 🟡 Второй экземпляр приложения не блокируется

При занятом BRIDGE_PORT второй экземпляр лишь логирует и продолжает работать (main.py:196-201): две tray-иконки, общая SQLite и общая папка recordings; ручной старт записи возможен в обоих. Пользователь, не заметивший старый экземпляр, получает загадочное «Meet не подхватывается» (события идут в первый) или две параллельные записи. Рекомендация: занятый порт трактовать как «уже запущено» — показать сообщение и выйти (или добавить mutex/lock-файл).

### Single points of failure (сводно)

| SPOF | Эффект | Смягчение |
|---|---|---|
| Claude CLI (путь, авторизация, версия флагов) | Оба LLM-этапа стоят | Уже смягчено: prompt-файлы + ручной путь + «Этап выполнен» — хороший design |
| CUDA/VRAM (workers жёстко требуют GPU) | Транскрипция невозможна | Осознанная политика (CPU запрещён); стоит явный тост «нет CUDA» вместо rc=1 |
| Chrome extension + native host + TCP-мост | Автостарт молчит | Есть ручной старт; события дропаются молча — хотя бы raise notification при первом meet_started-фейле было бы полезно |
| app.log (DEBUG, один файл, без ротации) | Растёт неограниченно; PII встреч в логах | RotatingFileHandler(10MB×3) — одна строка |
| Файловые имена как идентичность | См. 🔴 в разделе 4 | session_id в имени |

### Что сломается первым при росте

1. **Коллизии имён документов** — сломается не «при росте», а при первом же дубле названия (раздел 4, 🔴).
2. **Двойной клик «Обработать»** — при первой длинной встрече, когда пользователь нетерпелив (раздел 2, 🔴).
3. **Зависший worker без watchdog** — при первом драйверном сбое GPU (раздел 3, 🟡).
4. **Линейный хардкод этапов** — при первом новом LLM-этапе (раздел 7, 🟡).
5. **App/main.py** — при любой попытке добавить Zoom или второй UI (раздел 1, 🟡).

---

## Топ-5 приоритетных действий

1. **🔴 Добавить session_id в имена документов** (`file_manager.get_doc_paths`) — устраняет молчаливую перезапись/удаление документов встреч-тёзок; заодно упрощает rename и delete. (~1 час, самый высокий риск потери данных на строку кода.)

2. **🔴 Guard от повторного запуска пайплайна на job** — проверка `status='processing'`/реестр активных job в `App._on_process_job` + немедленный refresh трея. (~30 минут.)

3. **🟡 Единый worker-runner с таймаутом и kill-on-cancel** — вынести триждыскопированный Popen-код в `app/workers/runner.py`, добавить duration-based timeout, `proc.kill()` по cancel_event, хвост stderr в текст ошибки, Job Object на Windows против сирот. (~полдня, закрывает разделы 1 и 3 разом.)

4. **🟡 Восстановление после крэша во время записи** — при старте закрывать сессии без `ended_at` и создавать для них pending-job, если есть чанки. Плюс `harden_for_mac` для MeetingStartDialog/MeetingEditDialog (известный крэш-вектор на Mac). (~1 час.)

5. **🟡 Вынести SQL из main.py в repo-слой + JobStatus-enum + pytest-скелет** на чистые функции (align/mix/naming/merge_meetings). Это фундамент, который делает пункты 1–4 безопасными для внесения. (~1 день.)

---
*Отчёт: docs/2026-07-02-review_fable/01-architecture.md. Ревью статическое — код прочитан целиком в перечисленных модулях; UI (spectrum, meetings_window, mac_window) — по диагонали.*
