# Код-ревью: захват аудио и ML-часть (capture / transcription / diarization)

Дата: 2026-07-02. Ревьюер: Claude (Fable 5).
Файлы: `app/capture/*`, `app/transcription/*`, `app/diarization/*`, `tools/transcribe_file.py` (~2,3 KLOC).

## Резюме

**Общая оценка: 6.5/10.**

Архитектурно код зрелый: subprocess-изоляция cudnn-конфликта решена грамотно и последовательно, протокол stdout=JSON / stderr=логи+PROGRESS документирован прямо в коде, комментарии объясняют *почему* (гонка `communicate()` с собственным stderr-reader'ом, `sys.modules["torch"] = None`, паддинг planes в PyAV). Mac-путь захвата (audiotee) написан аккуратнее Windows-пути.

Но в Windows-захвате найдены **два функциональных бага** (потерянный звук в режиме без микрофона; выбрасывание хвоста loopback на каждой границе чанка) и **одна перф-бомба** (чисто питоновский микс/RMS на ~3 млн сэмплов блокирует чтение микрофона на секунды каждые 30 сек — почти наверняка это и есть первопричина наблюдаемого дрейфа 0.2–0.3 с/чанк, который сейчас лечится обрезкой). Обработка смерти аудио-устройства посреди записи — «тихая»: запись прекращается, а приложение продолжает думать, что пишет. Плюс систематическое дублирование subprocess-runner'а ×3 и заглатывание исключений в нескольких местах вопреки правилам проекта.

### Сильные стороны

- Подробные «почему»-комментарии у всех неочевидных решений: `bufsize=1` + `PYTHONUNBUFFERED`, отказ от `communicate()`, `temperature=(0.0,)` + анти-зацикливание для faster-whisper, `to_ndarray()` вместо `planes[0]` в PyAV.
- `transcribe_worker.py`: faulthandler + `dump_traceback_later(30)`, партиал-сохранение сегментов каждые 10 с — при краше worker'а транскрипция не теряется целиком. Отличная защита от известных крашей ctranslate2.
- Явный запрет CPU-транскрипции с внятным exit вместо молчаливого фолбэка в 40-минутную работу.
- `mac_tap._read_stdout`: буферизация с выравниванием по границе сэмпла (`ready = len(buf) // bpf * bpf`) — сэмпл не рвётся.
- Mac-flush корректно сохраняет остаток `tap_buf` для следующего чанка (в отличие от Windows — см. F2).
- Silence-компенсация в `_lb_reader` (loopback при отсутствии системного звука блокируется — вставляется тишина по wall-clock) — рабочее решение известной проблемы WASAPI loopback.

---

## Находки

### app/capture/audio_capture.py

#### 🔴 F1. Режим «без микрофона» на Windows: записанный звук выбрасывается, в чанк попадает мусор
`audio_capture.py:248-249` + `:289-307`

В loopback-only ветке цикл переносит данные из `lb_raw` в `mc_raw`:

```python
mc_raw.extend(chunk_bytes)          # :302 — реальные 30 сек звука копятся здесь
```

Но `_flush_chunk()` при `mic_stream is None` берёт **`lb_bytes`** (остаток `lb_raw`, который цикл ещё не успел выгрести — десятки миллисекунд), а `mc_bytes` — те самые 30 секунд аудио — **молча выбрасывает**:

```python
mc_bytes = bytes(mc_raw)
mc_raw.clear()
if mic_stream is not None:
    ...
else:
    mixed = lb_bytes                # :249 — БАГ: должно быть mc_bytes
```

Итог: в системе без микрофона чанк содержит случайный хвост в ~0.02–0.5 с, RMS почти наверняка ниже порога → «chunk discarded: silent» → вся встреча записывается в никуда. Ветка редкая (`_find_default_mic` фолбэчится на любой input-девайс), но если срабатывает — это тотальная потеря записи без единой ошибки в логе.

**Фикс:** `mixed = mc_bytes` в else-ветке (или не гонять данные через `mc_raw`, а копить прямо в отдельный буфер и брать его).

#### 🔴 F2. Обрезка loopback при флаше выбрасывает реальный системный звук на каждой границе чанка
`audio_capture.py:214-216, 244-246`

`_flush_chunk` забирает **весь** `lb_raw` (`del lb_raw[:]`), затем обрезает до длины микрофонного буфера:

```python
lb_bytes = lb_bytes[: mc_frames_count * self._channels * 2]   # излишек теряется навсегда
```

Излишек — это не тишина, а последние `diff_sec` (по вашим же логам 0.2–0.3 с) реального системного звука. Он не переносится в следующий чанк, а уничтожается. При CHUNK_DURATION_SEC=30 это ~1% системного звука, выкусываемого ровно на границах чанков — обрезанные слоги собеседников, которые потом «теряет» транскрипция.

Mac-реализация делает правильно (`:432-434`): берёт из `tap_buf` ровно `tap_take` байт, остаток оставляет в буфере.

**Фикс** — симметрично Mac-версии:

```python
with lb_lock:
    take = mc_frames_count * self._channels * 2
    lb_bytes = bytes(lb_raw[:take])
    del lb_raw[:take]               # хвост остаётся на следующий чанк
```

(При этом обрезку/паддинг в `_mix_audio` оставить как страховку для случая lb короче mc.)

#### 🔴 F3. Микс и RMS на чистом Python блокируют чтение микрофона на секунды — вероятная первопричина дрейфа
`audio_capture.py:575-593 (_mix_audio), :596-601 (_calc_rms), :286-287`

`_flush_chunk` выполняется **в том же потоке**, который читает `mic_stream.read()`. При 30-сек чанке @48 кГц stereo:

- `_mix_audio`: `struct.unpack` двух туплей по ~2.9 млн Python-int (это сотни МБ транзиентной памяти: каждый int ~28 байт) + питоновский цикл на 2.9 млн итераций с `result.append`;
- следом `_calc_rms`: ещё один unpack + `sum(v*v for ...)` на 2.9 млн сэмплов.

Суммарно это легко 1–3 секунды на слабом CPU, в течение которых микрофон не читается. WASAPI-буфер переполняется, а `exception_on_overflow=False` **молча дропает сэмплы**. Именно так возникает систематическое «mic отстаёт от loopback на 0.2–0.3 с на чанк», которое сейчас компенсируется обрезкой из F2. То есть F2+F3 вместе: сначала теряем звук микрофона из-за медленного микса, потом выбрасываем звук loopback, чтобы длины сошлись.

**Фикс:** заменить на `audioop`/numpy (audioop уже импортируется в этом методе):

```python
import audioop
# mono→stereo: audioop.tostereo(mc, 2, 1.0, 1.0); stereo→mono: audioop.tomono
mixed = audioop.add(lb_bytes, mc_bytes_conv, 2)   # C-скорость, с сатурацией
rms   = audioop.rms(raw, 2)
```

Это на 2–3 порядка быстрее и скорее всего устранит сам дрейф. Учесть: `audioop` удалён в Python 3.13 (проект на 3.10/3.11 — ок, но numpy-вариант долговечнее: `np.frombuffer(..., np.int16).astype(np.int32)` → сложение → `np.clip` → `astype(np.int16).tobytes()`).

#### 🔴 F4. Смерть аудио-устройства посреди записи — «тихая»: запись останавливается, приложение думает, что пишет
`audio_capture.py:176-177, 267-269`

```python
except OSError:
    break
```

— в обоих местах (`_lb_reader` и главный mic-цикл) исключение проглатывается **без логирования** (нарушение правила проекта «каждый catch логирует exception object целиком»). Хуже: после `break` из mic-цикла `_capture_wasapi` доходит до конца без исключения → `_capture_loop` завершается штатно → `on_error` **не вызывается**, `self._recording` остаётся `True`, `is_recording` возвращает `True`. Пользователь выдернул USB-микрофон — и до конца встречи ни один чанк не пишется, UI показывает идущую запись, в логе пусто.

Отдельно: смерть только `_lb_reader` (сменили default output device) ещё незаметнее — mic-цикл продолжает работать, `lb_raw` перестаёт расти, чанки пишутся mic-only, системный звук молча пропадает (сработает разве что drift-warning с обратным знаком).

**Фикс:**
1. `except OSError as e: log.warning("mic read failed, capture stops", exc_info=e); break` (аналогично в `_lb_reader`).
2. После штатного выхода из цикла — если `self._recording` всё ещё `True`, значит выход был аварийным: вызвать `self.on_error(RuntimeError("audio device lost"))` и/или выставить `self._recording = False`.
3. (Отдельным улучшением — переоткрытие default-устройства с ретраем, но это уже фича.)

#### 🟡 F5. `frames_buffered` считает фреймы до ресемплинга — чанк длиннее заявленного при mic_rate ≠ 48 кГц
`audio_capture.py:270-275`

```python
mc_data, mic_resample_state = audioop.ratecv(mc_data, 2, mic_channels, mic_rate, self._rate, ...)
mc_raw.extend(mc_data)
frames_buffered += frames_per_read      # 512, хотя реально добавлено 512 * rate/mic_rate
```

Микрофон, открытый на native 44100 (ветка `for try_rate in (self._rate, mic_rate_native)`), даёт после ресемплинга ~557 фреймов на чтение, а счётчик прибавляет 512 → флаш срабатывает на ~8.8% позже, чанк ≈ 32.6 с вместо 30. Функционально не смертельно (длительность чанка мягкая), но счётчик врёт, и любые расчёты «сколько накоплено» на нём — тоже.

**Фикс:** `frames_buffered += len(mc_data) // (2 * mic_channels)`.

#### 🟡 F6. `_evaluate_quality`: `except Exception: pass` — полное заглатывание
`audio_capture.py:630-631`

Оценка качества падает молча (нет модели, битый WAV, OOM) — фича quality-toast просто перестаёт работать, диагностировать нечем. Docstring «Не бросает исключений» — ок, но по правилам проекта нужно `log.exception(...)`, хотя бы на DEBUG:

```python
except Exception:
    logging.getLogger(__name__).exception("quality evaluation failed for chunk %d", idx)
```

То же — `list_audio_sources` (`:705-706, :723-724`) и `_find_default_mic` (`:333-334`): `except Exception: pass` без следа.

#### 🟡 F7. `ThreadPoolExecutor` никогда не закрывается
`audio_capture.py:72`

Экзекьютор создаётся на каждый `AudioCapture` (= на каждую встречу) и не имеет `shutdown()`. Потоки non-daemon → два потока `quality-*` утекают на каждую сессию записи и удерживаются до выхода процесса (плюс на выходе atexit-join может тормозить завершение, если whisper-tiny ещё крутится). **Фикс:** в `stop()` — `self._executor.shutdown(wait=False)` (и создавать executor в `start()`, если `AudioCapture` переиспользуется).

#### 🟢 F8. `log.setLevel(logging.DEBUG)` как сайд-эффект
`audio_capture.py:131` — метод захвата глобально переключает уровень модульного логгера. Управление уровнями — дело конфигурации логирования, не рабочего кода. Убрать (drift-логи и так на DEBUG останутся видимы, если так настроен root).

#### 🟢 F9. Мелочи
- `:161` — `import time` внутри `_lb_reader` при уже импортированном `time` во внешней функции.
- `:487` — `threading.Event().wait(0.1)`: создание Event на каждой итерации ради сна; `time.sleep(0.1)` проще и дешевле.
- `:142` — `lb_timeout` объявлен «таймаут ожидания loopback», реально используется как период сна только в no-mic ветке; комментарий вводит в заблуждение.
- `:415-422` (Mac) — `on_audio_frame` вызывается прямо из real-time callback sounddevice; тяжёлый обработчик (спектр в UI) в аудио-callback'e — риск щелчков. Лучше форвардить через очередь.
- CLAUDE.md говорит «WAV-чанки 10 сек», `config.CHUNK_DURATION_SEC = 30` — рассинхрон документации.

#### 🟢 F10. `app/capture/source_selector.py` — пустой файл
Файл существует (1 пустая строка), нигде не импортируется. Мёртвый артефакт — удалить (упоминаю, не трогаю, по правилам проекта).

---

### app/capture/mac_tap.py

#### 🟡 F11. Мгновенная смерть audiotee не приводит к on_error — запись «идёт» без системного звука
`mac_tap.py:186-210, :156-157`

Если audiotee стартует и сразу умирает (нет TCC-permission, битый бинарь), `_read_stdout` получает EOF и тихо выходит через `break` — `on_error` вызывается только при *исключении* чтения, не при EOF/ненулевом exit-коде. `start()` лишь пишет warning «stream_start не получен за 10 сек» и продолжает. Единственная защита — эвристика RMS в `flush_chunk` (warning в лог, не пользователю). **Фикс:** после `break` в `_read_stdout` проверить `self._running and self._proc.poll() is not None` → `on_error(RuntimeError(f"audiotee exited with {rc}"))`; и/или в `start()` после таймаута `_stream_started` проверять `proc.poll()`.

#### 🟡 F12. `stop()`: `except Exception: pass` + необработанный второй таймаут
`mac_tap.py:171-172`

Ошибка terminate/wait глотается без лога (нарушение правила). Кроме того, `self._proc.wait(timeout=2.0)` внутри `except TimeoutExpired` при повторном таймауте выбросит исключение уже наружу `stop()` (except-блок не ловится соседним `except Exception`) — и прилетит в `finally` `_capture_mac`. **Фикс:** залогировать (`log.exception`) и обернуть второй `wait` в собственный try.

#### 🟢 F13. Мелочи
- `:189` — `chunk_bytes` вычисляется и не используется (мёртвая переменная, остаток старой логики «отдавать по chunk_duration»).
- `:208` — `log.error("...: %s", exc)` теряет стектрейс; по правилам проекта — `log.exception(...)` или передать `exc` последним аргументом с `exc_info`.
- `:192-194` — гонка `self._running and self._proc is not None` → `stop()` обнуляет `_proc` → `AttributeError` в читателе, который приведёт к ложному `on_error` при штатной остановке. Скопировать `proc = self._proc` в локальную переменную в начале потока.

---

### Транскрипционные бэкенды и workers

#### 🟡 F14. Три почти идентичных subprocess-runner'а — вынести в общий модуль
`gigaam_backend.py:42-77` ≈ `faster_whisper_backend.py:46-85` ≈ `pyannote_diarizer.py:42-71`

Popen с одинаковыми флагами, один и тот же `_pipe_stderr` (в двух из трёх — с дублированным парсингом `PROGRESS:`), одинаковое чтение stdout, wait, join, проверка returncode. ~50 строк × 3 копии; любое улучшение протокола (таймаут, kill, захват stderr для ошибки) придётся делать трижды — и уже сейчас копии разъехались (gigaam: `s.get("confidence", 1.0)`, whisper: `s["confidence"]`). **Фикс:** общий помощник, например `app/workers/runner.py`:

```python
def run_worker(worker: Path, args: list[str], on_progress=None, timeout=None) -> dict:
    """Popen + stderr-reader (PROGRESS + логи) + stdout=JSON. Возвращает распарсенный dict."""
```

Аналогично у самих workers дублируется обвязка: `sys.path.insert`, load_dotenv + HF_TOKEN-копирование, logging.basicConfig(stderr), faulthandler, nvidia-smi VRAM-инфо, печать PROGRESS — просится `worker_common.py`.

#### 🟡 F15. Нет таймаута и kill: зависший worker вешает pipeline навсегда
`gigaam_backend.py:68-69`, `faster_whisper_backend.py:76-77`, `pyannote_diarizer.py:62-63`

`proc.stdout.read()` + `proc.wait()` без таймаута. Известный режим отказа faster-whisper — зависание (сами же боролись параметрами и `dump_traceback_later`); если оно всё же случится, pipeline-тред заблокирован до перезапуска приложения, а UI показывает вечный «processing». Также при убийстве родителя (крэш, taskkill) дети-workers остаются жить и держать GPU. **Фикс:** `proc.wait(timeout=...)` пропорционально длительности аудио (например `max(600, dur*2)`) с `proc.kill()` и внятным исключением; на Windows — Job Object c `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (или хотя бы kill в `finally` вызывающего кода).

#### 🟡 F16. Ошибки worker'а невидимы: stderr на DEBUG, RuntimeError без контекста, `json.loads` без защиты
`gigaam_backend.py:63,72-75`, `faster_whisper_backend.py:68,80-83`, `pyannote_diarizer.py:55,66-69`

Три связанных дефекта одного протокола:
1. Весь stderr worker'а (включая его `log.error` с traceback'ом) форвардится через `log.debug(...)` — при уровне INFO в app.log причина падения worker'а **не попадает в лог вообще**.
2. `RuntimeError(f"...завершился с кодом {returncode}")` не содержит ни одной строки stderr — по такой ошибке диагностировать нечего.
3. `json.loads(stdout)` упадёт с голым `JSONDecodeError`, если любая зависимость worker'а напечатает в stdout хоть строку (tqdm/torch/gigaam любят это делать) — содержимое stdout при этом не логируется.

**Фикс:** в stderr-reader'е складывать последние ~50 строк в `deque`; строки уровня ERROR/WARNING worker'а форвардить на соответствующем уровне (по префиксу `ERROR`/`WARNING` из formatter'а); при `returncode != 0` включать хвост stderr в RuntimeError; при JSONDecodeError логировать первые/последние строки stdout. Ещё надёжнее — печатать JSON одной последней строкой и парсить `stdout.strip().splitlines()[-1]`.

#### 🟡 F17. `gigaam_worker`: `tempfile.mktemp` — deprecated и race-prone
`gigaam_worker.py:159`

`mktemp` официально deprecated (гонка имя-выдано/файл-создан). Здесь риск практический маленький, но замена тривиальна: `tempfile.NamedTemporaryFile(suffix=".wav", delete=False)` или один `mkstemp`-файл, перезаписываемый на каждый чанк (VAD-чанков сотни — заодно меньше churn'а в темп-каталоге). А лучше вообще без диска: `gigaam` умеет принимать тензор/массив в новых версиях — стоит проверить и передавать чанк напрямую.

#### 🟢 F18. `gigaam_worker` vs `transcribe_worker`: несимметричная защита
- `transcribe_worker.py:196-199` оборачивает всё в `except BaseException` с полным traceback и `exit(1)`; `gigaam_worker.main()` не обёрнут — упадёт с голым traceback в stderr (который родитель залогирует на DEBUG → см. F16).
- `transcribe_worker.py:37` включает `faulthandler.dump_traceback_later(30, repeat=True)`; gigaam_worker — нет, хотя тоже способен зависнуть на CUDA.
- `transcribe_worker` сохраняет partial-результат; gigaam_worker при краше на 95% теряет всё. Для 28x-realtime это менее болезненно, но перенести partial-механику дёшево.

#### 🟢 F19. `backend.py`: дефолт фолбэка расходится с config
`backend.py:65` — `getattr(config, "TRANSCRIPTION_ENGINE", "whisper")`, а реальный дефолт проекта `gigaam` (`config.py:73`). `getattr`-фолбэк мёртвый (атрибут всегда есть), но если когда-нибудь сработает — молча включит не тот движок. Написать просто `engine or config.TRANSCRIPTION_ENGINE`.

#### 🟢 F20. `mlx_whisper_backend`: мелочи
- `:88` и `:113` — `total_duration` и `duration` вычисляются дважды одним и тем же выражением; второе — лишнее.
- `:110-111` — `on_progress` вызывается уже после завершения транскрипции (цикл идёт по готовому результату): прогресс-бар мгновенно пролетает 0→100%. Не баг (heartbeat компенсирует), но стоит комментария, чтобы никто не искал «где стриминг».
- `redirect_stdout` — процессо-глобальный: пока идёт транскрипция, `print` из любого другого потока уйдёт в `_LogWriter`. На практике других print'ов нет, но помнить стоит.

---

### app/diarization/*

#### 🟡 F21. `_load_wav_as_tensor`: гигабайты RAM на часовой записи
`diarize_worker.py:39-44`

```python
samples = struct.unpack(f"<{len(raw) // 2}h", raw)      # tuple из десятков млн Python-int
tensor  = torch.tensor(samples, dtype=torch.float32)     # ещё одна полная копия
```

Час аудио 16 кГц mono = 57.6 млн сэмплов → tuple Python-int'ов ≈ 1.6+ ГБ плюс копия в тензор. Для стерео 48 кГц (чанки приложения мёржатся в WAV сессии) — кратно больше; реальный риск OOM/свопа именно на длинных встречах. **Фикс** — zero-copy:

```python
tensor = torch.frombuffer(bytearray(raw), dtype=torch.int16).to(torch.float32) / 32768.0
# sample_width == 4: dtype=torch.int32, делитель 2147483648.0
```

#### 🟢 F22. Мелочи diarization
- `diarize_worker.py:21-25` — `level=logging.DEBUG` у basicConfig: torch/pyannote на DEBUG чрезвычайно болтливы, весь этот поток гоняется через pipe и `log.debug` родителя. INFO достаточно.
- `diarize_worker.py:76` — `Pipeline.from_pretrained(...)` без обработки отсутствия HF_TOKEN: упадёт глубоким стеком huggingface_hub. Проверить env заранее и выйти с человекочитаемой ошибкой (`log.error("HF_TOKEN not set — gated model"); sys.exit(1)`) — это самый частый сбой при развёртывании.
- `pyannote_diarizer.py` — см. F14–F16 (общий runner, таймаут, stderr).

---

### tools/transcribe_file.py

#### 🟢 F23. Мелочи
- `:5-6` и `:132` — docstring и лог обещают «faster-whisper (whisper-large-v3, CUDA)», но `get_backend()` без аргумента на Windows вернёт **gigaam** (дефолт config). Либо звать `get_backend("whisper")`, либо поправить текст и лог (лог может печатать фактический движок).
- `:147` — импорт приватных `_assign_speakers`, `_build_speaker_map` из `app.processing.pipeline`: скрипт зависит от подчёркнутых внутренностей другого модуля; при рефакторинге pipeline тихо сломается. Переименовать в публичные или вынести в общий модуль выравнивания.
- `:68-85` — `container.close()` не в `finally`: при исключении в цикле декодирования контейнер утекает (то же в `gigaam_worker._load_audio_pyav_int16:75-86`). PyAV поддерживает `with av.open(...) as container:`.
- `_extract_wav_16k_mono` и `gigaam_worker._load_audio_pyav_int16` — третий пункт дублирования PyAV-декодера (см. F14): одна и та же логика decode→resample→flush в двух местах с разными выходами (файл vs ndarray).

---

## Дублирование — сводка (п. F14)

| Что | Где | Объём |
|---|---|---|
| Subprocess-runner (Popen, stderr-reader, PROGRESS-парсер, wait, returncode, json.loads) | gigaam_backend, faster_whisper_backend, pyannote_diarizer | ~50 строк × 3 |
| Обвязка worker'ов (sys.path, dotenv+HF_TOKEN, logging→stderr, faulthandler, nvidia-smi) | gigaam_worker, transcribe_worker (частично diarize_worker) | ~40 строк × 2–3 |
| PyAV decode→resample(16k mono s16) | gigaam_worker, tools/transcribe_file | ~20 строк × 2 |
| `unload()` no-op | 3 модуля | тривиально, но симптом |

Один модуль `app/workers/` (runner + common) убирает ~150 строк и делает F15/F16 одноразовыми фиксами.

---

## Топ-5 приоритетов

1. **F3 + F2** — заменить `_mix_audio`/`_calc_rms` на audioop/numpy и перестать выбрасывать хвост `lb_raw` (брать `del lb_raw[:take]`, как на Mac). Это устраняет и потерю системного звука на границах чанков, и, скорее всего, сам источник дрейфа mic↔loopback.
2. **F4** — логировать `OSError` при чтении устройств и вызывать `on_error`/сбрасывать `_recording` при аварийном выходе из capture-цикла: выдернутый микрофон не должен превращаться в час пустой «записи».
3. **F1** — однострочный фикс `mixed = mc_bytes` в no-mic ветке `_flush_chunk`: сейчас режим без микрофона теряет 100% записи.
4. **F15 + F16** — таймаут+kill для worker-процессов и видимость их ошибок (stderr-хвост в исключении, WARN/ERROR не на DEBUG): сейчас зависший или упавший worker — это вечный «processing» и пустой лог.
5. **F14** — вынести subprocess-runner и worker-обвязку в общий модуль, чтобы фиксы №4 не пришлось делать в трёх копиях (заодно F21 — `torch.frombuffer` в diarize_worker против OOM на длинных встречах).
