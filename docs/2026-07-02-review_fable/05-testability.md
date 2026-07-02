# Обзор тестируемости MeetDescriber

**Дата:** 2026-07-02
**Автор:** review-агент (Fable 5)
**Область:** setup.py, test_mac_tap.py, config.py, app/main.py, app/capture/audio_capture.py, app/processing/pipeline.py + analysis.py, app/storage/ (db.py, meetings_repo.py, file_manager.py), app/transcription/ (backend.py, gigaam_backend.py, gigaam_worker.py), app/diarization/pyannote_diarizer.py, app/extension/native_host.py, prompts.py, requirements-*.txt

---

## Резюме

**Оценка тестируемости: 4/10.**

Автотестов нет вообще — ноль pytest-файлов, ноль CI. При этом сам код тестируемее, чем кажется на первый взгляд: в проекте уже стихийно сложились правильные швы — subprocess-фасады вместо прямых импортов ML-библиотек (`gigaam_backend.py`, `pyannote_diarizer.py` импортируемы без torch), инъекция `ask_claude` в пайплайн, чистые функции выравнивания/именования, stdlib-only `native_host.py`. Балл снижают: глобальный модуль `config` как единственный источник путей, БД-доступ через захардкоженный `config.DB_PATH`, критичная логика (drift-расчёт, микс, flush) внутри замыканий 200-строчного метода `_capture_wasapi`, и god-класс `App`, создающий `tk.Tk()` в конструкторе.

Хорошая новость: **~15 самых ценных тестов можно написать вообще без рефакторинга** — только `monkeypatch` путей в `config` и подмена двух имён в `pipeline`. Плохая: регрессии в capture-слое (там, где исторически жили самые дорогие баги — drift, рассинхрон, тишина вместо звука) автотестами почти не ловятся и потребуют выноса 2–3 чистых функций.

---

## 1. Что проверяется сейчас: setup.py как «замена тестам»

`setup.py` (673 строки) — это **интерактивная диагностика окружения**, а не тесты. Оценка честная: как smoke-проверка инсталляции он полезен, как замена регрессионным тестам — нет.

Что он реально проверяет:

| Гипотеза | Что проверяет | Автоматизируемо? |
|---|---|---|
| deps | импортируемость модулей (`setup.py:92`) | да, тривиально |
| H1/H2 | захват WASAPI/Tap с **живым звуком** и `input()` от пользователя (`setup.py:162`, `:234`) | нет — требует железа и человека |
| H3/H4 | загрузка whisper-модели, транскрипция синусоиды (`setup.py:276`) | нет на CI — качает ~500 МБ, нужен GPU для честного прогона |
| H5 | pyannote pipeline + HF-токен (`setup.py:350`) | нет — gated-модель, ~1 ГБ |
| H6 | корреляция confidence с шумом (`setup.py:411`) | нет — исследовательская проверка, не регрессионная |
| H7 | ping-pong с native_host через pipe (`setup.py:480`) | **да** — это готовый интеграционный тест, его надо просто перенести в pytest |
| H8 | заглушка «отложено» (`setup.py:570`) | — |

Ключевые слабости setup.py как страховки:

1. **Он проверяет окружение, а не код.** Ни одна из 8 гипотез не поймает регрессию в `_mix_audio`, `reset_to_stage` или `_build_speaker_map`. Сломанный silence filter, съехавшая статусная машина, неправильное переименование документов — всё это пройдёт H1–H8 зелёным.
2. **Интерактивность** (`ask()`, `input()`) делает его непригодным для CI и для «прогнать после каждого коммита».
3. **Он дрейфует от кода.** `setup.py:96` до сих пор требует `anthropic`, хотя CLAUDE.md и `config.py:37` явно говорят, что API не используется; `setup.py:351` говорит «pyannote 3.1», requirements-mac — 4.0.4. Диагностика, которую не гоняют регулярно, гниёт так же, как тесты.

`test_mac_tap.py` и `tools/legacy/test_blackhole.py` — ручные железо-скрипты, к автотестам отношения не имеют (и это нормально — их место в ручном чек-листе, см. §3c).

**Вывод:** setup.py стоит сохранить ровно в его нише — «проверь машину перед первым запуском» — а H7 забрать в pytest.

---

## 2. Анализ тестируемости по слоям

### 2.1 Уже чистое и тестируемое (можно писать тесты сегодня)

| Код | Где | Почему тестируемо |
|---|---|---|
| `_assign_speakers` | `app/processing/pipeline.py:69` | чистая функция: (transcription, diarization) → list[dict] |
| `_detect_names` | `pipeline.py:108` | чистая, regex по тексту |
| `_build_speaker_map` | `pipeline.py:129` | чистая, приоритет saved > detected > «Спикер N» |
| `_load/_save_diarization_cache` | `pipeline.py:182,192` | Path in/out, tmp_path достаточно |
| `_safe_name`, `get_doc_paths` | `app/storage/file_manager.py:19,26` | чистая + один monkeypatch `DOCUMENTS_DIR` |
| `_fmt_time`, `_fmt_duration` | `file_manager.py:94,102` | чистые |
| `write_transcription_md` | `file_manager.py:111` | детерминированный MD из списков — идеальный golden-тест |
| `merge_chunks` | `file_manager.py:66` | wave-файлы в tmp_path, включая идемпотентность («merged.wav уже есть → вернуть его») |
| `rename_session_docs` | `file_manager.py:46` | tmp_path |
| `_mix_audio`, `_calc_rms`, `_save_wav` | `app/capture/audio_capture.py:575,596,604` | чистые функции на bytes — тестируются идеально (клиппинг, mono→stereo, разная длина) |
| `App._clean_meet_title` | `app/main.py:250` | staticmethod, чистая |
| `read_message`/`send_message`, `NativeHost`, `AppBridge` | `app/extension/native_host.py:36,48,92,56` | stdlib-only, протокол framing тестируется через `io.BytesIO`, bridge — через реальный TCP-сокет на localhost |
| `prompts.py` + `analysis._build_prompt` / `_build_chat_prompt` | `app/processing/analysis.py:35,53` | шаблоны `.format()` — тест на подстановку и на отсутствие KeyError при `{` в названии встречи (кстати, реальный риск: `title="Sync {Q3}"` уронит `.format()` — тест это и вскроет) |
| `db.init_db` миграции | `app/storage/db.py:63` | достаточно monkeypatch `config.DB_PATH` |
| весь `meetings_repo.py` | `app/storage/meetings_repo.py` | докстринг честно говорит «Не зависит от UI» — и это правда: только БД + fs |

Отдельно приятно: **subprocess-фасады** (`gigaam_backend.py`, `pyannote_diarizer.py`) на уровне модуля импортируют только stdlib. Значит, `pipeline.py` импортируем на машине без torch/CUDA — интеграционный тест пайплайна возможен на голом CI.

### 2.2 Сплетено с I/O / tkinter / глобальным состоянием

**`config` как глобальный модуль.** `DB_PATH`, `RECORDINGS_DIR`, `DOCUMENTS_DIR` — константы модуля, читаемые везде (`db.py:54`, `file_manager.py:36`, `pipeline.py:232`, `meetings_repo.py` повсюду). Это *не* блокер: `monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")` решает всё, потому что чтение происходит в момент вызова, а не импорта. **Рефакторинг не нужен** — нужна одна autouse-фикстура (см. §4).

**`db.get_conn()`** (`db.py:51`) открывает соединение на каждый вызов по `config.DB_PATH`. In-memory `:memory:` не подойдёт (каждый вызов = новая пустая БД), но файловая БД в tmp_path работает так же быстро и вернее отражает прод. **Рефакторинг не нужен.**

**`pipeline.run_transcription`** (`pipeline.py:200`) — оркестратор с правильной идемпотентностью («файл есть → скип», `pipeline.py:250,339,366`), но бэкенды захардкожены: `PyannoteDiarizer()` в `pipeline.py:276` и `get_backend()` в `pipeline.py:293`. `ask_claude` уже инжектится — половина DI сделана. **Минимальный рефакторинг (опционален):** два необязательных параметра `run_transcription(..., backend=None, diarizer=None)` с фолбэком на текущие фабрики — 4 строки. Но и без него `monkeypatch.setattr("app.processing.pipeline.get_backend", ...)` работает.

**`AudioCapture._capture_wasapi`** (`audio_capture.py:124–322`) — главная проблемная зона. 200 строк, внутри которых замыканиями живут: вставка тишины при блокировке loopback (`:159–178`), drift-расчёт и throttling (`:212–247` внутри `_flush_chunk`), обрезка lb до длины mc (`:244–246`). Это код, где были самые дорогие баги проекта, и он недоступен ни одному тесту, кроме запуска с реальным WASAPI. **Минимальный рефакторинг** (без слоёв и абстракций, чистый вынос):

```python
# audio_capture.py — module-level, рядом с _mix_audio
def _compute_drift_sec(lb_bytes_len: int, mc_bytes_len: int,
                       rate: int, lb_ch: int, mc_ch: int) -> float:
    lb_frames = lb_bytes_len // (2 * lb_ch)
    mc_frames = mc_bytes_len // (2 * mc_ch)
    return (lb_frames - mc_frames) / rate

def _trim_lb_to_mic(lb: bytes, mc: bytes, lb_ch: int, mc_ch: int) -> bytes:
    mc_frames = len(mc) // (2 * mc_ch)
    return lb[: mc_frames * lb_ch * 2]
```

`_flush_chunk` зовёт их вместо инлайна — поведение бит-в-бит то же, а drift-порог/обрезка становятся тестируемыми. Аналогично можно (вторым шагом, не обязательно) вынести «сколько тишины вставить при блокировке read» из `_lb_reader`.

**`AudioCapture._process_chunk`** (`audio_capture.py:541`) — silence filter + запись чанка + events. Тестируется почти как есть: создать `AudioCapture(tmp_path)`, выставить `_rate`/`_channels`, позвать `_process_chunk(raw)`. Единственная ловушка — `self._executor.submit(_evaluate_quality, ...)` в `:572` потянет whisper-tiny. В тесте — `monkeypatch.setattr("app.capture.audio_capture._evaluate_quality", lambda *a: None)`. **Рефакторинг не нужен.**

**`App` (`app/main.py:76`)** — god-класс: `tk.Tk()` в конструкторе (`main.py:92`), tray, bridge-сервер и БД-запросы в обработчиках. Тестировать класс целиком не надо. Но в нём заперта важная логика: парсинг line-protocol моста (`_serve_bridge_conn`, `main.py:218`) и решения по skip/discard (`_handle_meet_started`, `main.py:262`). **Минимальный рефакторинг:** вынести из `_serve_bridge_conn` разбор буфера в модульную функцию `iter_bridge_messages(buf: bytes) -> tuple[list[dict], bytes]` (возвращает распарсенные сообщения + остаток буфера) — метод станет трёхстрочным, а протокол (склейка чанков, битый JSON, пустые строки) — тестируемым. Логику skip-встреч трогать не стоит: она размазана по состоянию App и дешевле покрывается ручным чек-листом.

**`_evaluate_quality`** (`audio_capture.py:612`) — `except Exception: pass` без лога: тест не напишешь, и баг не увидишь. Попутное замечание (нарушает и правило CLAUDE.md «Errors: Log Fully»): добавить `log.debug(..., exc_info=True)`.

**tkinter UI (`app/ui/*`), mac_tap, workers c ML** — не тестировать автоматически, см. §3c.

### 2.3 Сводка минимальных рефакторингов

Всего три, все — «вынос чистой функции», ни одного нового слоя:

1. `_compute_drift_sec` + `_trim_lb_to_mic` из `_flush_chunk` (`audio_capture.py:212–247`) — открывает тесты на самый багоопасный код.
2. `iter_bridge_messages` из `App._serve_bridge_conn` (`main.py:218–245`) — тестируемость bridge-протокола без сокетов и App.
3. (опционально) параметры `backend=None, diarizer=None` в `run_transcription` — вместо monkeypatch по строковым путям.

Всё остальное покрывается фикстурами и monkeypatch без изменения продакшн-кода.

---

## 3. Стратегия тестирования по уровням

### 3a. Unit-тесты чистой логики

Модули и функции (в порядке ценности):

- `pipeline._assign_speakers`, `_detect_names`, `_build_speaker_map`
- `file_manager._safe_name`, `get_doc_paths`, `write_transcription_md`, `merge_chunks`, `_fmt_*`
- `audio_capture._mix_audio`, `_calc_rms`, (после выноса) `_compute_drift_sec`, `_trim_lb_to_mic`
- `main.App._clean_meet_title`
- `native_host.read_message`/`send_message` (framing)
- `analysis._build_prompt`/`_build_chat_prompt` + шаблоны `prompts.py`
- `db.init_db` (миграции на «старой» схеме, сброс processing→pending)
- `meetings_repo.reset_to_stage`, `merge_meetings`, `list_all_meetings` (это уже лёгкая интеграция с tmp-БД, но пишется как unit)

Примеры кода (pytest):

```python
# tests/unit/test_align.py
from app.processing.pipeline import _assign_speakers, _build_speaker_map, _detect_names
from app.transcription.backend import TranscriptionResult, TranscriptionSegment
from app.diarization.pyannote_diarizer import DiarizationSegment


def _tr(segs):
    return TranscriptionResult(
        segments=[TranscriptionSegment(start=s, end=e, text=t, confidence=1.0)
                  for s, e, t in segs],
        language="ru", duration=60.0)


def test_assign_speakers_max_overlap():
    tr = _tr([(0.0, 5.0, "привет"), (5.0, 10.0, "и вам")])
    diar = [DiarizationSegment(start=0.0, end=4.0, speaker="SPEAKER_00"),
            DiarizationSegment(start=4.0, end=10.0, speaker="SPEAKER_01")]
    aligned = _assign_speakers(tr, diar)
    # сегмент 0-5: overlap SPEAKER_00 = 4с > SPEAKER_01 = 1с
    assert aligned[0]["speaker"] == "SPEAKER_00"
    assert aligned[1]["speaker"] == "SPEAKER_01"


def test_assign_speakers_no_overlap_leaves_empty():
    tr = _tr([(100.0, 105.0, "поздний хвост")])
    diar = [DiarizationSegment(start=0.0, end=10.0, speaker="SPEAKER_00")]
    assert _assign_speakers(tr, diar)[0]["speaker"] == ""


def test_speaker_map_priority_saved_over_detected():
    aligned = [{"speaker": "SPEAKER_00", "text": "меня зовут Олег"},
               {"speaker": "SPEAKER_01", "text": "ага"}]
    detected = _detect_names(aligned)
    assert detected == {"SPEAKER_00": "Олег"}
    m = _build_speaker_map(aligned, detected, saved_names={"SPEAKER_00": "Олег Иванов"})
    assert m["SPEAKER_00"] == "Олег Иванов"     # БД важнее детекции
    assert m["SPEAKER_01"] == "Спикер 2"        # нумерация по sorted-порядку
```

```python
# tests/unit/test_mix_audio.py
import struct
from app.capture.audio_capture import _mix_audio, _calc_rms


def pcm(*vals):
    return struct.pack(f"<{len(vals)}h", *vals)


def test_mix_clips_at_int16_bounds():
    out = _mix_audio(pcm(30000, -30000), pcm(30000, -30000), lb_ch=1, mc_ch=1)
    assert struct.unpack("<2h", out) == (32767, -32768)


def test_mix_mono_mic_into_stereo_loopback():
    lb = pcm(100, 200)          # 1 stereo-фрейм: L=100 R=200
    mc = pcm(10)                # 1 mono-фрейм
    out = _mix_audio(lb, mc, lb_ch=2, mc_ch=1)
    assert struct.unpack("<2h", out) == (110, 210)   # mono дублируется в оба канала


def test_mix_pads_shorter_stream_with_silence():
    lb = pcm(100, 100)          # 2 фрейма mono
    mc = pcm(50)                # 1 фрейм
    out = _mix_audio(lb, mc, lb_ch=1, mc_ch=1)
    assert struct.unpack("<2h", out) == (150, 100)


def test_rms_of_silence_is_zero():
    assert _calc_rms(pcm(0, 0, 0, 0)) == 0.0
    assert _calc_rms(b"") == 0.0
```

```python
# tests/unit/test_file_manager.py
import wave
from app.storage.file_manager import _safe_name, get_doc_paths, merge_chunks, write_transcription_md


def test_safe_name_strips_forbidden_chars_and_truncates():
    assert _safe_name('Q3: план/факт  <ревью>') == "Q3__план_факт___ревью_"
    assert len(_safe_name("х" * 200)) == 60
    assert _safe_name("") == "без_названия"


def test_get_doc_paths_uses_date_prefix(patched_config):  # фикстура из §4
    paths = get_doc_paths("Синк", "2026-07-01T10:00:00+00:00")
    assert paths["transcription"].name == "2026-07-01_Синк_transcription.md"
    assert set(paths) == {"transcription", "analysis", "analysis_prompt",
                          "followup", "followup_prompt"}


def _write_wav(path, n_frames, value=1000):
    import struct
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes(struct.pack(f"<{n_frames}h", *([value] * n_frames)))


def test_merge_chunks_concatenates_in_order_and_is_idempotent(tmp_path):
    _write_wav(tmp_path / "chunk_0000.wav", 100)
    _write_wav(tmp_path / "chunk_0001.wav", 50)
    merged = merge_chunks(tmp_path)
    with wave.open(str(merged), "rb") as wf:
        assert wf.getnframes() == 150
    # второй вызов не пересобирает и не дублирует
    assert merge_chunks(tmp_path) == merged
    with wave.open(str(merged), "rb") as wf:
        assert wf.getnframes() == 150


def test_transcription_md_golden(tmp_path):
    out = tmp_path / "t.md"
    write_transcription_md(
        path=out, title="Синк", started_at="2026-07-01T10:00:00", agenda="",
        duration=125.0, speaker_map={"SPEAKER_00": "Олег"},
        segments=[{"start": 0.0, "end": 5.0, "text": "привет",
                   "speaker": "SPEAKER_00", "confidence": 0.95}])
    text = out.read_text(encoding="utf-8")
    assert "`[00:00:00]` **Олег:** привет" in text
    assert "**Длительность:** 2мин 5сек" in text
```

```python
# tests/unit/test_clean_meet_title.py
import pytest
from app.main import App  # ok: класс не инстанцируем, tk.Tk() не создаётся


@pytest.mark.parametrize("raw,expected", [
    ("Meet – Weekly Sync", "Weekly Sync"),
    ("Meet — abc-defg-hij", "abc-defg-hij"),
    ("Meet - планёрка", "планёрка"),
    ("Weekly Sync", "Weekly Sync"),   # без префикса — как есть
    ("", ""),
])
def test_clean_meet_title(raw, expected):
    assert App._clean_meet_title(raw) == expected
```

> Нюанс: `import app.main` на CI тянет `tkinter`, `pystray`, `Pillow` (через `app.ui`). Если это окажется тяжело для CI-матрицы — либо ставить pystray/Pillow (они лёгкие), либо после выноса `iter_bridge_messages` перенести и `_clean_meet_title` в модуль без UI-импортов. До тех пор тест можно пометить `@pytest.mark.skipif(no_tk)`.

### 3b. Интеграционные тесты

**(1) Пайплайн на фейковых бэкендах — самый ценный тест проекта.** Проверяет статусную машину `pending → processing → transcribed → analyzed → done`, идемпотентность и diarization-кэш, не загружая ни одной модели:

```python
# tests/integration/test_pipeline.py
import json, threading, wave, struct
import pytest
from app.transcription.backend import (TranscriptionBackend, TranscriptionResult,
                                       TranscriptionSegment)
from app.diarization.pyannote_diarizer import DiarizationSegment


class FakeBackend(TranscriptionBackend):
    def transcribe(self, audio_path, on_progress=None):
        return TranscriptionResult(
            segments=[TranscriptionSegment(0.0, 5.0, "меня зовут Олег", 0.9)],
            language="ru", duration=5.0)


@pytest.fixture
def job(patched_config):  # patched_config — см. §4
    from app.storage.db import init_db, get_conn
    import config
    init_db()
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO sessions (title, started_at) VALUES ('Синк', '2026-07-01T10:00:00')")
        sid = cur.lastrowid
        jid = conn.execute(
            "INSERT INTO jobs (session_id, status) VALUES (?, 'pending')", (sid,)).lastrowid
    d = config.RECORDINGS_DIR / f"session_{sid}"
    d.mkdir(parents=True)
    with wave.open(str(d / "chunk_0000.wav"), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(16000)
        wf.writeframes(struct.pack("<16000h", *([1000] * 16000)))
    return jid, sid, d


def test_pipeline_happy_path(job, monkeypatch):
    jid, sid, session_dir = job
    import app.processing.pipeline as pl
    monkeypatch.setattr(pl, "get_backend", lambda engine=None: FakeBackend())

    class FakeDiarizer:
        def diarize(self, path):
            return [DiarizationSegment(0.0, 5.0, "SPEAKER_00")]
    monkeypatch.setattr(pl, "PyannoteDiarizer", FakeDiarizer)

    statuses = []
    fake_claude = lambda stage, prompt_path, cli, **kw: f"результат этапа {stage}"

    path = pl.run_transcription(jid, ask_claude=fake_claude,
                                on_progress=lambda s, d="": statuses.append(s))

    from app.storage.db import get_conn
    with get_conn() as conn:
        row = dict(conn.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone())
    assert row["status"] == "done"
    assert path.exists() and "Олег" in path.read_text(encoding="utf-8")
    assert (session_dir / "diarization.json").exists()          # кэш записан
    assert statuses[-1] == "done"
    # промпт-файлы удалены после успеха (cleanup_prompt)
    from app.storage.file_manager import get_doc_paths
    assert not get_doc_paths("Синк", "2026-07-01T10:00:00")["analysis_prompt"].exists()


def test_pipeline_is_idempotent(job, monkeypatch):
    """Повторный запуск done-задания не перезапускает тяжёлые этапы."""
    # ... первый прогон как выше, затем:
    monkeypatch.setattr(pl, "get_backend",
                        lambda engine=None: pytest.fail("transcribe не должен вызываться"))
    pl.run_transcription(jid, ask_claude=lambda *a, **k: pytest.fail("claude не должен вызываться"))


def test_pipeline_cancel_between_stages(job, monkeypatch):
    ev = threading.Event(); ev.set()
    with pytest.raises(pl.PipelineCancelledError):
        pl.run_transcription(jid, cancel_event=ev, ask_claude=lambda *a, **k: "x")


def test_pipeline_no_chunks_sets_error(patched_config):
    # job без wav-файлов → status='error', понятное сообщение
    ...
```

**(2) БД и meetings_repo на временной БД.** `init_db` на «старой» схеме (создать таблицы без новых колонок → init_db должен добавить `dismissed` и `*_duration_sec`; `db.py:71–85`), сброс `processing→pending` (`db.py:67`), `reset_to_stage` (все три ветки: какие файлы удалены, какой статус; `meetings_repo.py:225`), `merge_meetings` (перенос чанков с продолжением нумерации, сброс job в pending, удаление второй сессии; `meetings_repo.py:307`), `delete_meeting`/`delete_old_audio`.

**(3) Native messaging протокол через pipe** — портировать H7 из `setup.py:480` в pytest:

```python
# tests/integration/test_native_host.py
import json, struct, subprocess, sys
from pathlib import Path

HOST = Path(__file__).parents[2] / "app" / "extension" / "native_host.py"


def _frame(obj):
    b = json.dumps(obj).encode()
    return struct.pack("<I", len(b)) + b


def test_ping_pong():
    proc = subprocess.Popen([sys.executable, str(HOST)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    try:
        proc.stdin.write(_frame({"type": "ping"})); proc.stdin.close()
        raw_len = proc.stdout.read(4)
        n = struct.unpack("<I", raw_len)[0]
        assert json.loads(proc.stdout.read(n)) == {"type": "pong"}
    finally:
        proc.kill()
```

Плюс unit на framing без процесса (`read_message` через подмену `sys.stdin` на `io.BytesIO`-обёртку: обрезанный заголовок → None, обрезанное тело → None) и тест `AppBridge.forward` против реального `socketserver` на свободном порту (переподключение после обрыва — вторая попытка в `native_host.py:66`).

**(4) Worker-протокол с фейковым worker-скриптом.** `GigaAMBackend.transcribe` (`gigaam_backend.py:37`) — это контракт «stdout=JSON, stderr=PROGRESS+логи, rc≠0=ошибка». Подменяем `_WORKER` на мини-скрипт:

```python
# tests/integration/test_worker_protocol.py
FAKE_WORKER = """
import sys
print("PROGRESS:1/2", file=sys.stderr)
print("PROGRESS:2/2", file=sys.stderr)
print('{"segments": [{"start": 0, "end": 1, "text": "тест"}], "language": "ru", "duration": 1.0}')
"""

def test_gigaam_backend_parses_worker_output(tmp_path, monkeypatch):
    w = tmp_path / "fake_worker.py"
    w.write_text(FAKE_WORKER, encoding="utf-8")
    import app.transcription.gigaam_backend as gb
    monkeypatch.setattr(gb, "_WORKER", w)
    progress = []
    res = gb.GigaAMBackend().transcribe(tmp_path / "in.wav",
                                        on_progress=lambda c, t: progress.append((c, t)))
    assert res.segments[0].text == "тест"
    assert progress == [(1.0, 2.0), (2.0, 2.0)]


def test_gigaam_backend_raises_on_nonzero_exit(tmp_path, monkeypatch):
    w = tmp_path / "boom.py"; w.write_text("import sys; sys.exit(3)")
    ...  # pytest.raises(RuntimeError, match="кодом 3")
```

Тот же приём — для `PyannoteDiarizer` (`pyannote_diarizer.py:42`). Это дёшево и охраняет самое хрупкое место архитектуры: ручное чтение stdout/stderr вместо `communicate()`.

**(5) `AudioCapture._process_chunk`** с monkeypatch `_evaluate_quality`: тишина ниже `SILENCE_THRESHOLD_RMS` → файла нет, `on_audio_stopped` дёрнут; громкий чанк → `chunk_0000.wav` создан, `on_chunk_saved(path, 0)`; чередование → корректные события started/stopped и сквозная нумерация.

### 3c. Что честно НЕ тестировать автоматически

| Область | Почему | Компенсация |
|---|---|---|
| tkinter UI (`app/ui/*`), tray-меню, Mac event loop | краши здесь — GIL/PyObjC/Aqua-специфика, воспроизводимая только на живой системе; headless-эмуляция даст ложную уверенность | ручной чек-лист (ниже) + существующее логирование `app.user_actions` — после ручного прогона грепается `app.user_action` в app.log |
| Реальные ML-модели (gigaam, whisper, pyannote) | гигабайты весов, GPU, gated HF; качество распознавания — не бинарный assert | `setup.py` H3–H5 как installation-check; `research/transcribe_compare` — как offline-оценка качества при смене движка |
| CUDA/cuDNN-изоляция workers (краш 0xC0000409) | воспроизводится только на Windows+GPU через ~8 минут | канареечный ручной сценарий: одна реальная 10-минутная запись после обновления torch/ctranslate2; faulthandler-логи уже настроены |
| Захват WASAPI/Core Audio Tap с железом | нужен звук, драйверы, permissions | `setup.py` H1/H2, `test_mac_tap.py` — оставить как есть |
| Chrome extension ↔ реальный Chrome | Native Messaging регистрация в реестре, TCC | H7 (автоматизирован в §3b) покрывает хост; остальное — пункт чек-листа |
| Claude CLI subprocess | требует подписки и живого CLI | `ask_claude` инжектится — в тестах фейк; реальный вызов проверяется первым же боевым использованием `ClaudeManualDialog` (кнопка «Скопировать команду» как ручной fallback уже есть) |

**Ручной чек-лист** (docs/MANUAL_TESTING.md, прогонять перед «релизом» на вторую машину): старт/стоп из трея; автостарт из Meet; «Не записывать эту встречу» во время записи (сессия удалена с диска); переименование встречи (файлы переименованы); обработка → три документа; повторная обработка done-задания (мгновенный скип); выход из приложения на Mac (нет crash-репорта); drift-toast при захвате mic другим приложением.

---

## 4. Инфраструктура

### Структура

```
tests/
  conftest.py               # фикстуры patched_config, фабрики WAV/сессий
  unit/
    test_align.py           # _assign_speakers, _detect_names, _build_speaker_map
    test_mix_audio.py       # _mix_audio, _calc_rms, drift (после выноса)
    test_file_manager.py    # _safe_name, get_doc_paths, merge_chunks, MD golden
    test_prompts.py         # шаблоны + _build_prompt
    test_clean_meet_title.py
    test_native_framing.py  # read_message/send_message на BytesIO
  integration/
    test_db.py              # init_db, миграции, processing→pending
    test_meetings_repo.py   # reset_to_stage, merge_meetings, delete_*
    test_pipeline.py        # фейковые backend/diarizer/ask_claude
    test_native_host.py     # subprocess ping-pong (ex-H7), AppBridge через TCP
    test_worker_protocol.py # фейковые worker-скрипты
pytest.ini                  # или [tool.pytest] в pyproject
requirements-dev.txt        # pytest>=8
```

### Ключевая фикстура (решает проблему глобального config)

```python
# tests/conftest.py
import pytest
import config


@pytest.fixture
def patched_config(tmp_path, monkeypatch):
    """Уводит все пути config в tmp_path. БД — файловая во временной папке:
    get_conn() открывает соединение на каждый вызов, поэтому :memory: не годится."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "test.db")
    monkeypatch.setattr(config, "RECORDINGS_DIR", tmp_path / "recordings")
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path / "documents")
    config.ensure_dirs()
    return config
```

Важно: `db.py`, `file_manager.py`, `meetings_repo.py`, `pipeline.py` читают эти атрибуты в момент вызова (`config.DB_PATH` внутри `get_conn`), поэтому monkeypatch модуля-синглтона работает надёжно и honest — без переписывания продакшн-кода. Единственное, за чем следить: тесты не должны импортировать пути в свои локальные переменные на уровне модуля.

### Платформозависимые импорты

Проблема почти решена самим кодом: `pyaudiowpatch`, `sounddevice`, `mlx_whisper`, `faster_whisper`, `AppKit` импортируются **внутри функций** (`audio_capture.py:128,370`, `backend.py:62-71`, `main.py:145`). На уровне модулей — только stdlib + `config` + `dotenv`. Что остаётся:

- `pytest.ini`: маркеры `@pytest.mark.windows_only` / `mac_only` + в `conftest.py` автоскип по `sys.platform`;
- тесты, зовущие ветки с платформенными импортами (`_capture_wasapi`, `_find_mac_mic`), маркировать соответствующе; вся логика из §3a/3b платформонезависима;
- не импортировать в тестах `app.ui.*` (тянет pystray/Pillow/tkinter) — для юнитов это не нужно.

### Что мокать (и что нет)

Мокать: `pipeline.get_backend` / `PyannoteDiarizer` (фейк-классы, не MagicMock — контракт типизирован датаклассами), `ask_claude` (лямбда), `_evaluate_quality` (no-op), `_WORKER`-пути (фейк-скрипты). **Не мокать:** sqlite (реальная tmp-БД), файловую систему (tmp_path), wave (реальные крошечные WAV), subprocess в тестах worker-протокола (реальный `sys.executable`).

### CI (GitHub Actions)

Реально и дёшево. Вся тестовая пирамида из §3a/3b не требует ни GPU, ни моделей, ни звука:

```yaml
# .github/workflows/tests.yml
name: tests
on: [push, pull_request]
jobs:
  test:
    strategy:
      matrix:
        os: [windows-latest, macos-latest]
        python: ["3.11"]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "${{ matrix.python }}" }
      - run: pip install pytest python-dotenv
      - run: pytest tests/ -v
```

Заметьте: **устанавливаются только pytest и python-dotenv.** Ни torch, ни pyannote, ни pyaudiowpatch — subprocess-фасады и ленивые импорты делают весь тестируемый код импортируемым на голом Python. Это главный дивиденд существующей архитектуры. `ubuntu-latest` можно добавить третьим — вся логика из §3a/3b платформонезависима, а он в 10 раз дешевле по минутам; Windows/macOS оставить как «правда жизни» (пути, `datetime('now')`, wave).

Чего на CI не будет честно: H1–H6 (железо/модели), tkinter-диалоги, CUDA-краши. Это и есть граница §3c.

---

## 5. Приоритизированный план: первые 10 тестов

Критерий: (ценность защищаемого поведения × частота изменений кода) / стоимость написания.

| # | Тест | Что защищает | Стоимость |
|---|---|---|---|
| 1 | `test_pipeline_happy_path` (фейковые backend/diarizer/ask_claude) | статусную машину jobs целиком, запись MD, кэш диаризации, cleanup промптов — сердце приложения | средняя (нужен conftest) |
| 2 | `test_pipeline_is_idempotent` | повторная обработка не жжёт часы GPU и не зовёт Claude | низкая (поверх #1) |
| 3 | `test_mix_audio_*` (клиппинг, mono→stereo, паддинг) — `audio_capture.py:575` | микс — код, через который проходит каждый байт каждой записи | низкая |
| 4 | `test_merge_chunks_concatenates_in_order_and_is_idempotent` — `file_manager.py:66` | порядок чанков и «не пересобирать merged.wav» (на нём стоит идемпотентность пайплайна) | низкая |
| 5 | `test_reset_to_stage_*` (3 ветки) — `meetings_repo.py:225` | ручной перезапуск этапов: какие файлы удалять и какой статус ставить — легко сломать при добавлении этапа | низкая |
| 6 | `test_gigaam_backend_parses_worker_output` + `raises_on_nonzero_exit` (фейк-worker) | subprocess-контракт stdout/stderr/PROGRESS — самое хрупкое архитектурное место | низкая |
| 7 | `test_native_host_ping_pong` (портировать H7 из setup.py) + framing на BytesIO | протокол Chrome-интеграции; уже написан наполовину в `setup.py:480` | минимальная |
| 8 | `test_assign_speakers_*` + `test_speaker_map_priority` — `pipeline.py:69,129` | качество итогового документа: неправильное выравнивание = мусорная расшифровка молча | низкая |
| 9 | `test_init_db_migrates_old_schema` + `resets_stuck_processing` — `db.py:63` | каждый новый ALTER TABLE проходит через этот код на живой БД пользователя | низкая |
| 10 | `test_process_chunk_silence_filter` (с mock `_evaluate_quality`) — `audio_capture.py:541` | порог тишины и события started/stopped — регрессия здесь = молча потерянные куски встречи | средняя |

Вторая волна: `merge_meetings`, `_clean_meet_title`, golden-тест `write_transcription_md`, `_safe_name`/`get_doc_paths`/`rename_session_docs`, drift-расчёт (после выноса `_compute_drift_sec`), `_build_prompt` с `{`-инъекцией в title, `AppBridge` reconnect.

---

## Топ-5 приоритетов

1. **Создать tests/ + conftest.py с фикстурой `patched_config` и поднять CI (windows+macos, только pytest+dotenv)** — вся ценность дальнейших шагов существует только если тесты гоняются автоматически; инфраструктура здесь — один вечер, потому что рефакторинг для неё не нужен.
2. **Интеграционный тест пайплайна на фейковых бэкендах (тесты #1–2)** — статусная машина `pending→…→done`, идемпотентность и cleanup — единственное место, где регрессия стоит часов GPU-времени и потерянных встреч; monkeypatch двух имён в `pipeline.py:276,293` — рефакторинг не требуется.
3. **Юниты чистого ядра: `_mix_audio`/`_calc_rms`, `merge_chunks`, `_assign_speakers`/`_build_speaker_map`, `reset_to_stage` (тесты #3–5, #8)** — максимум покрытия багоопасной логики за минимальную цену, ноль изменений продакшн-кода.
4. **Контрактные тесты subprocess-протоколов: фейковые worker-скрипты + портированный H7 (тесты #6–7)** — охраняют самые хрупкие архитектурные решения проекта (ручной stdout/stderr-ридер вместо `communicate()`, Native Messaging framing).
5. **Единственный обязательный рефакторинг: вынести `_compute_drift_sec`/`_trim_lb_to_mic` из замыкания `_flush_chunk` (`audio_capture.py:212–247`) и `iter_bridge_messages` из `App._serve_bridge_conn` (`main.py:218`)** — два выноса чистых функций (без новых слоёв) открывают тестам код, где исторически жили самые дорогие баги: drift, рассинхрон, потерянные bridge-сообщения.
