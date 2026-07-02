# Код-ревью UI-части MeetDescriber

Дата: 2026-07-02. Зона: `app/main.py`, `app/ui/*` (tray, dialogs, meetings_window, notifications, spectrum, status_window, mac_window, user_actions). Проверка велась против правил самого проекта из CLAUDE.md (Mac-инварианты: ручной event loop, `App._schedule`, запрет `root.after` на Mac, `harden_for_mac` для всех Toplevel, bg/fg на кнопках Aqua) и против общих правил (логирование исключений, `log_action` в каждом UI-обработчике).

## Резюме — оценка 6.5/10

Кодовая база выше среднего для desktop-tkinter-приложения: Mac-инварианты не только задокументированы, но и в основном соблюдены (шина `_schedule` протянута через notifications/dialogs/spectrum/status_window, `_btn_kwargs` в notifications корректно разводит Aqua/Windows, tray на Windows делает thread-safe refresh через PostMessage — это редкая аккуратность). Хорошая наблюдаемость: faulthandler в app.log, `_FlushFileHandler`, перехват `report_callback_exception`, дисциплина `log_action` с гранулярностью click/confirm/cancel.

Но найдены: (1) реальная гонка в обработке `meet_started`/`meet_ended`, из-за которой кнопка «Не записывать эту встречу» может удалить **чужую** (ручную) запись, а посторонний `meet_ended` — остановить активную запись; (2) три нарушения собственных Mac-правил проекта (голый `win.after()` в tooltip, три Toplevel без `harden_for_mac`); (3) UI-фризы: обход всего `data/recordings` (гигабайты, тысячи файлов) на **каждое нажатие клавиши** в поиске окна «Все совещания»; (4) кластер проглоченных исключений вопреки правилу «каждый catch логирует exception object».

---

## 1. Нарушения собственных Mac-правил проекта

### 🔴 M-1. Голый `win.after()` / `after_cancel()` в tooltip — документированный SIGABRT-путь
**meetings_window.py:615, 624**

```python
self._tooltip_after_id = self._win.after(500, lambda: self._show_tooltip(row_id))
...
self._win.after_cancel(self._tooltip_after_id)
```

CLAUDE.md прямо говорит: «`root.after()` опасен» — на Mac Tk регистрирует `_runBackgroundLoop` в CFRunLoop, и когда pystray открывает меню, вложенный NSApp-loop может выдернуть after-таймер при отпущенном GIL → SIGABRT `_Py_FatalError_TstateNULL`. Сценарий вполне реален: пользователь водит мышью по таблице (взводится 500-мс таймер), затем кликает на menubar-иконку — таймер стреляет внутри NSMenuTrackingSession. Это **единственное** место в UI-слое, где after() вызывается напрямую; вся остальная кодовая база честно ходит через `_schedule`.

**Фикс:** передавать `schedule_fn` в `MeetingsWindow` (он уже получает `on_data_changed` из `App`) и заменить `after`/`after_cancel` на generation-counter поверх `_schedule`:
```python
self._tooltip_gen += 1
gen = self._tooltip_gen
self._schedule(lambda: self._show_tooltip(row_id) if gen == self._tooltip_gen else None, 500)
```
(`after_cancel` заменяется инкрементом счётчика).

### 🔴 M-2. `MeetingStartDialog`, `MeetingEditDialog`, `_pick_meeting_dialog` — Toplevel без `harden_for_mac`
**dialogs.py:99–159 (MeetingStartDialog), dialogs.py:440–492 (MeetingEditDialog), meetings_window.py:503–556 (_pick_meeting_dialog)**

CLAUDE.md: «Применяется ко всем Toplevel» — но захарднены только `ClaudeManualDialog`, `MeetingsWindow`, `ProcessingStatusWindow`. Три перечисленных диалога рисуются с полноценным заголовком и кликабельным красным «X». Клик в X на Mac — это документированный в этом же проекте путь `NSControlTrackMouse → windowShouldClose → WM_DELETE_WINDOW → PythonCmd → SIGABRT`. `MeetingStartDialog` при этом — один из самых частых диалогов (каждый ручной старт записи).

**Фикс:** добавить `harden_for_mac(win)` после `update_idletasks()` в трёх конструкторах + bind `<Escape>` на destroy, чтобы на Mac диалог вообще можно было отменить (сейчас после харденинга останется только кнопка «Отмена» — она есть, так что ок).

### 🟡 M-3. Кнопка «Отмена» в `ProcessingStatusWindow` со стилями, запрещёнными на Aqua
**status_window.py:160–169**

```python
tk.Button(frame, text="Отмена", ..., relief="flat",
          bg="#3a2a2e", fg="#cc7777", activebackground=..., ...)
```

Правило проекта: «на Mac Aqua-тема игнорирует `bg`/`fg`/`relief="flat"` — не указывать эти параметры». Здесь они указаны безусловно. На Mac это не крэш, но кнопка отрисуется системной белёсой на тёмно-синем `#1a1a2e` фоне — визуально чужеродно, а `fg="#cc7777"` игнорируется. В notifications.py та же задача решена правильно через `_btn_kwargs()`.

**Фикс:** собрать kwargs по образцу `notifications._btn_kwargs` (либо экспортировать её и переиспользовать — см. D-2).

### 🟡 M-4. `harden_for_mac` матчит NSWindow по title — ломается при двух окнах с одинаковым заголовком
**mac_window.py:34–37 + main.py:655–660**

`_on_open_meetings_window` создаёт **новый** `MeetingsWindow` на каждый клик (сингл-инстанса нет). Все копии имеют title «for_meets — Все совещания», и `harden_for_mac` для второго окна найдёт по title первое (уже захарднённое) и `return` — у второго окна красный X останется кликабельным → крэш-путь из M-2. Плюс сам по себе зоопарк дублирующихся окон — UX-мусор и утечка виджетов.

**Фикс:** в `App` держать ссылку на открытое окно: если живо — `deiconify()+lift()` вместо создания нового. В `harden_for_mac` дополнительно можно матчить через `win.frame()` → `NSApp.windowWithWindowNumber_` (у Tk есть `winfo_id`, но надёжнее хранить и проверять уже обработанные NSWindow).

### 🟡 M-5. `parent.wait_window(win)` внутри ручного Mac-цикла — вложенный Tk-loop без прокачки NSApp
**dialogs.py:159, 492; meetings_window.py:556**

Модальные диалоги блокируются в `wait_window`, который крутит внутренний Tk event loop. На Mac главный цикл `App.run()` в это время стоит внутри `root.update()` → NSApp-события (menubar-иконка, дренаж `_mac_queue`) не обрабатываются, пока диалог открыт. Значит: тосты `notifications.*`, `_schedule`-коллбэки с делеем (threading.Timer кладёт в `_mac_queue`) и обновления трея зависают до закрытия диалога. Например, если во время открытого `MeetingStartDialog` придёт `meet_started`, его `show()` выполнится только после закрытия диалога — поведение непредсказуемое для пользователя.

Это архитектурно заложено и, вероятно, осознанно (диалоги короткоживущие), но нигде не задокументировано. **Минимум** — комментарий в CLAUDE.md; правильнее — модальность через `grab_set` без `wait_window` + callback-стиль (как уже сделано в `ClaudeManualDialog`, который корректно работает без wait_window).

---

## 2. Реальные баги и гонки

### 🔴 B-1. «Не записывать эту встречу» может удалить чужую сессию; `_current_tab_id` присваивается даже если запись не началась
**main.py:273–290**

```python
def show():
    def skip():
        self._skip_tab_ids.add(tab_id)
        if self._current_tab_id == tab_id:
            self._discard_current_session()
    notifications.recording_started(title, on_skip=skip)
    self._start_session(title=title, source="meet")
    self._current_tab_id = tab_id          # ← безусловно!
```

`_start_session` (main.py:372–373) молча возвращается, если запись уже идёт. Но `self._current_tab_id = tab_id` выполняется **в любом случае**. Сценарии:

1. Идёт ручная запись → пользователь заходит в Meet. Проверка на bridge-потоке (main.py:270) может пройти до старта ручной записи (гонка bridge-thread ↔ main-thread), либо два `meet_started` от двух вкладок придут подряд. `show()` выполнится, тост «Запись началась» покажется, `_start_session` — no-op, но `_current_tab_id` теперь указывает на новую вкладку, а `_current_session_id` — на **старую** сессию. Клик «Не записывать эту встречу» → `_discard_current_session()` → **безвозвратно удаляется ручная/чужая запись** (DELETE из БД + `shutil.rmtree` аудио).
2. Даже без клика: `_handle_meet_title` теперь будет переименовывать активную сессию заголовком чужой вкладки.

**Фикс:** `_start_session` должна возвращать bool «стартовала ли», и весь блок в `show()` (тост + `_current_tab_id`) выполнять только при успехе:
```python
if not self._start_session(title=title, source="meet"):
    return
self._current_tab_id = tab_id
notifications.recording_started(title, on_skip=skip)
```

### 🔴 B-2. Посторонний `meet_ended` останавливает активную запись
**main.py:292–296**

```python
def _handle_meet_ended(self, msg: dict) -> None:
    tab_id = msg.get("tab_id")
    self._skip_tab_ids.discard(tab_id)
    self._current_tab_id = None
    self._schedule(self._stop_and_offer_processing)
```

Нет проверки, что `tab_id` — это вкладка активной записи. Сценарий: идёт **ручная** запись (`_current_tab_id is None`); пользователь заглянул в чужой Meet (авто-старт не сработал — уже пишем) и вышел из него → приходит `meet_ended` → ручная запись останавливается и предлагается к обработке посреди встречи. Аналогично для «skip»-вкладок: пользователь пометил встречу «не записывать», параллельно стартовала другая запись — выход из skip-вкладки убьёт её.

**Фикс:** останавливать только свою вкладку:
```python
if self._current_tab_id is not None and tab_id != self._current_tab_id:
    return
```

### 🟡 B-3. Мутации UI-состояния из bridge-потока без синхронизации
**main.py:298–321 (`_handle_meet_title`), 262–271 (`_handle_meet_started`)**

`_handle_meet_title` целиком выполняется на bridge-потоке: читает/пишет `self._current_title`, `self._current_session_id`, `self._capture.is_recording` и делает UPDATE в sqlite. Одновременно main-thread в `_stop_and_offer_processing` обнуляет те же поля. GIL делает отдельные присваивания атомарными, но логическая последовательность «проверил is_recording → UPDATE sessions» не атомарна: title может записаться в сессию, которая только что была discard'нута (строка уже удалена — UPDATE молча обновит 0 строк, терпимо) или переименовать сессию после stop. Реальный ущерб мал, но это единственный обработчик, нарушающий соглашение «всё состояние трогаем в main thread через `_schedule`» — `meet_started`/`meet_ended` его соблюдают.

**Фикс:** обернуть тело `_handle_meet_title` в `self._schedule(...)`, как соседние обработчики. SQLite при этом уедет в main thread — это ок, запрос миллисекундный.

### 🟡 B-4. `_on_process_job` не защищён от повторного запуска — два конкурирующих пайплайна
**main.py:513–545**

Клик «Обработать» в трее по тому же (или второму) заданию, пока пайплайн уже идёт, беспрепятственно поднимет второй `ProcessingStatusWindow` и второй поток `run_transcription`. Два GigaAM/pyannote-worker-процесса на одной CUDA → OOM/качели, плюс два модальных окна и две очереди `ask_claude`. Меню трея не перестраивается на время processing (статус в БД меняется, но `_refresh_tray_jobs` вызовется только в `finally`).

**Фикс:** флаг `self._pipeline_running: set[int]` (или один bool) в `App`; при повторном клике — toast «уже обрабатывается». Сбрасывать в `finally` воркера.

### 🟡 B-5. `ask_claude`: таймаут 30 мин роняет пайплайн, а диалог остаётся жить
**main.py:495–511**

`result_q.get(timeout=1800)` — если пользователь ушёл на обед, `queue.Empty` вылетает в pipeline-поток, джоб помечается error, но сам `ClaudeManualDialog` **не закрывается**: пользователь вернётся, нажмёт «Этап выполнен» — put уйдёт в мёртвую очередь, ничего не произойдёт, окно останется висеть (утечка Toplevel + обман пользователя). Второй под-случай: если `_schedule(lambda: ClaudeManualDialog(...))` упадёт в конструкторе (см. B-8 про `grab_set`), в очередь никогда ничего не попадёт и пайплайн молча висит 30 минут.

**Фикс:** ловить `queue.Empty` в `ask_claude`, по таймауту схедулить `dlg.destroy()` (нужно сохранить ссылку на диалог); конструктор диалога обернуть так, чтобы при исключении в очередь клался None.

### 🟡 B-6. `_on_quit` во время записи: сессия остаётся без `ended_at` и без job — запись «теряется»
**main.py:710–714**

При выходе `capture.stop()` вызывается (аудио дописывается на диск), но `ended_at` не проставляется и job не создаётся. После рестарта эта сессия не появится ни в «Необработанных» (нет job), ни корректно в «Все совещания» (длительность «—», обработку запустить не из чего — только «Перезапустить транскрипцию» через контекстное меню, и то `job_id is None` → warning «Задание не найдено»). Для приложения, чья главная ценность — не потерять запись встречи, это дыра.

**Фикс:** в `_on_quit` при активной записи вызывать урезанный `_stop_and_offer_processing` (stop + ended_at + `_create_job`, без уведомления). Плюс на старте `init_db` мог бы подхватывать sessions с `ended_at IS NULL` и чанками на диске.

### 🟡 B-7. Гонка «Закрыть окно» vs фоновый CLI-worker в `ClaudeManualDialog`
**dialogs.py:313–358, 414–417**

Пользователь нажал «Запустить» (worker пошёл), затем «Закрыть окно» → в очередь уходит `None`, пайплайн пропускает этап. Через минуту CLI успешно завершается: worker кладёт `STAGE_DONE` в уже прочитанную очередь (потеряется — не страшно) и схедулит `self._win.destroy` на уничтоженном окне → `TclError` в main loop (на Windows — CRITICAL в логе через `report_callback_exception`, на Mac — error из `mac_queue`). Плюс файл этапа реально записан, а статус джоба говорит «пропущено» — рассинхрон.

**Фикс:** флаг `self._closed` в `_on_skip`; в worker перед put/destroy проверять его, и в `_safe_after`-коллбэке оборачивать destroy в try/except TclError с комментарием.

### 🟢 B-8. `grab_set()` до того, как окно стало viewable
**dialogs.py:103, 444; meetings_window.py:555**

`win.grab_set()` вызывается сразу после создания Toplevel, до `update_idletasks`/map. На X11 это `TclError: grab failed: window not viewable`; на Windows/Mac обычно проносит, но зависит от WM-таймингов. Проект заявлен как Win+Mac, так что низкий приоритет, но безопаснее `win.wait_visibility(); win.grab_set()` (в `_pick_meeting_dialog` grab_set уже стоит после `update_idletasks` — почти правильно).

### 🟢 B-9. Контекстные меню `tk.Menu` накапливаются, `tk_popup` без `grab_release`
**meetings_window.py:284–337**

На каждый правый клик создаётся новый `tk.Menu(self._win)` и никогда не destroy'ится — дети окна копятся (микро-утечка). Канонический паттерн: `try: menu.tk_popup(...) finally: menu.grab_release()`, а меню либо создавать один раз, либо `menu.destroy()` по `<Unmap>`.

---

## 3. Обработка ошибок (правило: каждый catch логирует exception object)

### 🟡 E-1. `_delete_job_files`: три `except OSError: pass` подряд
**main.py:605–608, 613–617, 622–625**

Удаление файлов документов, промптов и папки аудио молча глотает OSError. Если файл залочен (открыт в редакторе — типично для Windows) — пользователь нажал «Удалить», строка из БД исчезла, а файлы остались навсегда, и никто об этом не узнает. Прямое нарушение правила проекта.

**Фикс:** `except OSError: log.warning("не удалось удалить %s", p, exc_info=True)` — везде. (Заодно: `import shutil` внутри метода дублирует модульный импорт.)

### 🟡 E-2. `meetings_window`: восемь `except Exception as e: messagebox.showerror(...)` без логирования
**meetings_window.py:351–352, 373–374, 401–402, 419–420, 438–439, 488–489, 565–566, 589–590**

Пользователь видит `str(e)`, но stacktrace не попадает в app.log — при разборе «нажал удалить — ошибка» по логам восстановить причину нельзя, хотя вся инфраструктура (app.log, user_action-лента) для этого и строилась. Ирония: `_notify_data_changed` и `_update_stats` в этом же файле логируют правильно через `log.exception`.

**Фикс:** перед каждым messagebox добавить `log.exception("<операция> failed")`.

### 🟡 E-3. `ClaudeManualDialog._on_open_cmd`: исключение только в статус-лейбл
**dialogs.py:400–401**

`except Exception as e: self._set_status(f"Не удалось открыть терминал: {e}")` — стектрейс потерян. Добавить `log.exception(...)`.

### 🟢 E-4. Молчаливые catch на путях завершения/очистки
**main.py:172–175 (`tray.stop()` при выходе), dialogs.py:338–341 (`os.unlink(tmp)`), notifications.py:100–104, 145–149, 218–223 (`except tk.TclError: pass`)**

Формально нарушают правило, но это genuine control flow (гонка «окно уже уничтожено», exit-path). По правилу проекта таким местам полагается короткий комментарий «почему намеренно» — в notifications его нет, в main.py:174 тоже. Одной строкой комментария в каждом месте вопрос закрывается.

---

## 4. Покрытие log_action

Покрытие **хорошее** — все tray-обработчики, кнопки ClaudeManualDialog, контекстное меню meetings_window (с гранулярностью click/confirm/cancel), cancel в status_window. Найденные дыры:

### 🟡 L-1. Кнопки «Отмена» в MeetingStartDialog и MeetingEditDialog не логируются
**dialogs.py:134–138, 474–478**

`command=win.destroy` напрямую — в ленте user_action останется только `tray_start_recording_manual` без исхода. При разборе «почему запись не началась» непонятно: диалог отменён или что-то упало. Фикс: `command=self._on_cancel` с `log_action("dialog_meeting_start_cancel")` + destroy. То же для закрытия через X на Windows (`WM_DELETE_WINDOW` не перехвачен → destroy без лога).

### 🟢 L-2. Авто-dismiss тоста логируется как действие пользователя
**notifications.py:201–204, 218–224**

Через 30 секунд бездействия `_auto_dismiss_process_later` вызывает `do_later()` → в лог падает `notification_process_later`, неотличимое от реального клика «Позже». Фикс: отдельное имя `notification_process_autodismiss`.

### 🟢 L-3. Смена фильтра статуса / поисковый запрос в MeetingsWindow не логируются
**meetings_window.py:122, 137**

Спорно (высокочастотные события), но хотя бы смену фильтра (`<<ComboboxSelected>>`) стоит логировать — это одиночный клик.

---

## 5. UX: блокировки main thread

### 🔴 U-1. Полный обход `data/recordings` + `data/documents` на каждое нажатие клавиши в поиске
**meetings_window.py:122 → 197–206 → 235–247; meetings_repo.get_stats → `_dir_size` (rglob по всем файлам)**

`self._search_var.trace_add("write", lambda *_: self._reload_meetings())` — каждый введённый символ синхронно, в main thread: (1) SQL-запрос; (2) `_populate_table` с `audio_dir.exists() and any(audio_dir.iterdir())` **на каждую строку**; (3) `_update_stats` → `get_stats()` → `_dir_size(RECORDINGS_DIR)` — **rglob по всем WAV-чанкам всех сессий** (гигабайты, тысячи файлов — сами же показываете «N GB записей»). На HDD или после сброса дискового кэша ввод слова из 6 букв — это 6 полных обходов дерева → секундные фризы всего UI, включая тосты и спектр.

**Фикс:** (а) debounce поиска ~250 мс (generation counter + `_schedule`); (б) статистику считать один раз при открытии окна (и после delete-операций), а не на каждый reload — при фильтрации она всё равно не меняется; (в) наличие аудио брать одним проходом `RECORDINGS_DIR.iterdir()` в set, а не exists+iterdir на строку.

### 🟡 U-2. Тяжёлые файловые операции в обработчиках меню — фриз на всё время операции
- **meetings_window.py:487 `merge_meetings`** — `shutil.move` чанков (на разных томах = копирование гигабайт) в main thread; окно и тосты мертвы до конца.
- **meetings_window.py:588 `delete_old_audio`**, **437 `delete_meeting`** — `shutil.rmtree` десятков GB в main thread.
- **main.py:630–646 `_on_delete_job`/`_on_delete_all_pending`** — rmtree на pystray-потоке (Windows): трей-меню не отвечает, но UI жив — терпимее, однако на Mac это main thread.

**Фикс:** выносить в поток: `threading.Thread(target=..., daemon=True)` + по завершении `_schedule(self._reload_meetings)`; на время — курсор watch/заблокированная кнопка.

### 🟡 U-3. `capture.stop()` в main thread при остановке записи
**main.py:408, 458**

`_stop_and_offer_processing` и `_discard_current_session` выполняются через `_schedule` в main thread и синхронно ждут остановки capture (join потока захвата, flush последнего чанка, на Mac — terminate audiotee-subprocess). Плюс в `_discard_current_session` — `shutil.rmtree` всей папки записи. Для длинной встречи это заметные фризы UI в самый видимый момент («нажал стоп — всё зависло»). Фикс аналогичен U-2: stop/rmtree в поток, финализацию БД и тосты — через `_schedule`.

### 🟢 U-4. `deiconify()` на каждом progress-тике транскрипции
**status_window.py:189–191**

`_update` для стадий ≠ analysis/followup безусловно зовёт `self._win.deiconify()`. PROGRESS-сообщения при транскрипции приходят часто; deiconify видимого topmost-окна на Windows может дёргать z-order/фокус. Фикс: флаг `self._withdrawn`, deiconify только при переходе из withdrawn.

### 🟡 U-5. `style.theme_use("default")` — глобальная смена ttk-темы всего приложения
**status_window.py:137–146**

`ttk.Style.theme_use` действует на весь процесс, не на окно. При каждом запуске обработки Combobox/Treeview/Scrollbar в открытом «Все совещания» перерисуются темой «default» (на Windows — уродливая Motif-подобная, вместо vista/aqua). Фикс: не трогать тему; кастомный стиль прогрессбара можно сконфигурировать в текущей теме, либо (если тема vista не даёт красить прогрессбар) — заменить на Canvas-полоску.

---

## 6. God-object App, слои

### 🟡 A-1. `App` совмещает 4 роли (main.py, 726 строк)
1. **TCP-сервер моста** (`_run_bridge_server`, `_serve_bridge_conn`, main.py:185–245) — сетевой код, просится в `app/extension/bridge_server.py` с callback-интерфейсом.
2. **Inline-SQL** (main.py:316–320, 376–381, 423–424, 432–436, 483–491, 518–522, 550–556, 663–679) — сырые запросы к sessions/jobs прямо в UI-оркестраторе, при том что рядом уже существует слой `app/storage/meetings_repo.py` + `db.update_session`. Особенно показателен `_delete_job_files` (main.py:586–628) — почти дословный дубль `meetings_repo.delete_meeting`, с теми же удалениями документов/промптов/папки, но с проглоченными ошибками (E-1) и без переиспользования.
3. **Файловые операции** (rmtree в `_discard_current_session`, `_delete_job_files`) — место им в storage-слое.
4. Собственно UI-оркестрация — единственное, что должно остаться.

**Фикс (поэтапно, без большого рефакторинга):** сперва заменить `_delete_job_files` на вызов `meetings_repo.delete_meeting(session_id)` (устраняет дубль и E-1 разом), `_refresh_tray_jobs`-запросы вынести в `meetings_repo.list_tray_jobs()`. Bridge-сервер — вторым шагом.

### 🟢 A-2. Доступ к приватным полям соседнего объекта
**main.py:395** — `self._spectrum.set_format(self._capture._rate, self._capture._channels)`: UI лезет в приватные атрибуты AudioCapture. Добавить капчуру публичные свойства `rate`/`channels`.

---

## 7. Дублирование, мёртвый код, магия

### 🟡 D-1. `MeetingStartDialog` ≈ `MeetingEditDialog` — ~95% копипаста
**dialogs.py:80–167 vs 424–499**

Отличия: заголовок окна, текст кнопки OK, предзаполнение агенды, имя log_action. Один базовый класс `_MeetingInfoDialog(parent, caption, ok_label, title, agenda, action_name)` сокращает ~70 строк и гарантирует, что фиксы (harden_for_mac из M-2, лог отмены из L-1, grab из B-8) не разъедутся между копиями.

### 🟢 D-2. Четыре реализации одного и того же schedule-fallback
- `dialogs._safe_after` (dialogs.py:38–43)
- `notifications._schedule` (notifications.py:34–38)
- `SpectrumWidget.__init__` default (spectrum.py:56–61)
- `ProcessingStatusWindow.__init__` default (status_window.py:50–55)

Плюс два сеттера-регистратора (`dialogs.set_schedule`, `notifications.set_schedule`). Один модуль `app/ui/scheduling.py` с `set_schedule()`/`schedule(fn, delay_ms)` убирает разнобой и снижает шанс, что новое окно забудет про Mac-путь (что уже случилось в M-1).

### 🟢 D-3. Прочая копипаста и мёртвый код
- **tray.py:380–384** — `_make_job_handler` не используется нигде (меню строится через `_make_pending_submenu`/`_make_done_submenu`). Удалить.
- **tray.py:285–290 vs 352–357** — `make_open_file` определён дважды дословно; вынести на уровень модуля.
- **dialogs.py:388–389** — `self._set_status("Промпт скопирован...")` вызван два раза подряд. Удалить дубль.
- **main.py:427 + 431** — `self._refresh_tray_jobs()` дважды: вызов на 427 до создания job бесполезен. Оставить один после `_create_job`.
- **main.py:497, 515, 588** — локальные `import queue as _queue`, `import threading`, `import shutil` дублируют модульные импорты.
- `_btn_kw = {"padx": 10, "pady": 6} if config.IS_WINDOWS else {"padx": 10}` — повторён 5 раз (dialogs.py:125, 251, 465; meetings_window.py:187, 545). В общий хелпер.
- Центрирование окна по экрану — 4 дословных копии (dialogs.py:144–149, 303–308, 480–485; status_window.py:92–95).
- `meetings_window._open_file` (341–352) дублирует `tray._open_path` (33–38) — второй уже кроссплатформенный, переиспользовать.

### 🟢 D-4. Магические константы
- Палитра размазана литералами: `#1a1a2e` (tray.py:54, status_window.py:97+), `#2b2b2b`, `#1e2a1e`, `#3a1f1f`, `#2d6a2d`/`#c0392b` (dialogs.py:421 — статусные цвета), `#5566cc`… Просится `app/ui/colors.py`.
- **notifications.py:232** `_SPECTRUM_CLEARANCE = 80` жёстко связан с `spectrum.HEIGHT = 70` (spectrum.py:31–33) числом, а не импортом: изменение высоты спектра молча вернёт баг «тост под спектром», который уже чинили (коммит 5c0c01e). Фикс: `from app.ui.spectrum import HEIGHT as SPECTRUM_HEIGHT; _SPECTRUM_CLEARANCE = SPECTRUM_HEIGHT + 10`.
- Размеры тостов 320×110/80/120 и таймауты 9000/8000/30000 — терпимо, но заслуживают именованных констант в шапке notifications.py.
- **spectrum.py:183–184** — `struct.unpack(f"<{count*ch}h", ...)` + slice, при том что 15 строками выше (`_compute_rms`, 168) та же задача решена `np.frombuffer(...)[::ch]` — быстрее и короче. Унифицировать на frombuffer.

### 🟢 D-5. ESC закрывает MeetingsWindow только на Mac
**meetings_window.py:106–111** — `win.bind("<Escape>", ...)` живёт в ветке `IS_MAC`. На Windows ESC не работает — бесплатная консистентность теряется. Вынести bind из ветки.

---

## Сильные стороны

- **Mac-инварианты реально соблюдаются**, а не только задокументированы: `_schedule` протянут через все окна, `_mac_queue` с дренажом в ручном цикле, `harden_for_mac` на трёх ключевых окнах, `_btn_kwargs` в notifications образцово разводит Aqua/Windows, комментарии на местах объясняют «почему» со ссылкой на симптом (SIGABRT-цепочки расписаны).
- **tray.py — образцовая потокобезопасность на Windows**: refresh через кастомный `_WM_TRAY_REFRESH` + PostMessageW на HWND pystray-потока — правильное решение редко встречающейся проблемы, с внятным комментарием про момент валидности HWND.
- **Наблюдаемость**: faulthandler в app.log, `_FlushFileHandler` (flush на каждую запись — переживает SIGABRT), `report_callback_exception` перехвачен, bridge-обработчики изолированы try/except с `exc_info=True`, лента `log_action` с раздельными click/confirm/cancel в meetings_window.
- **`_on_process_job`**: пайплайн в daemon-потоке, `finally` гарантированно закрывает модалку и обновляет трей; `PipelineCancelledError` отделён от настоящих ошибок; cancel-кнопка честно объясняет, что остановка — на границе этапа.
- `ClaudeManualDialog._on_run` — subprocess в worker-потоке с маршалингом результата через `_safe_after`, а не блокировка UI.
- Спектр: кольцевой буфер под локом, `_tick` с защитой от TclError и логированием, разумный отказ от FFT-мусора при тишине.

---

## Топ-5 приоритетов

1. **🔴 B-1 + B-2 (main.py:273–296):** guard'ы по `tab_id` — «Не записывать» не должен удалять чужую сессию, посторонний `meet_ended` не должен останавливать активную запись. Риск безвозвратной потери записи встречи.
2. **🔴 M-2 (dialogs.py, meetings_window.py):** `harden_for_mac` для `MeetingStartDialog`, `MeetingEditDialog`, `_pick_meeting_dialog` — сейчас клик в красный X на Mac ведёт по задокументированному крэш-пути.
3. **🔴 M-1 (meetings_window.py:615):** убрать голый `win.after()` в tooltip — единственное нарушение главного Mac-правила проекта.
4. **🔴 U-1 (meetings_window.py + meetings_repo.get_stats):** убрать rglob по всем записям с каждого нажатия клавиши в поиске: debounce + статистика один раз за открытие окна.
5. **🟡 E-1/E-2 (main.py:605–625, meetings_window.py):** проглоченные исключения при удалении файлов и в обработчиках меню — привести к правилу «каждый catch логирует exception object»; заодно заменить `_delete_job_files` на `meetings_repo.delete_meeting` (A-1).
