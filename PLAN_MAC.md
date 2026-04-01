# План адаптации MeetDescriber → macOS

> Windows остаётся полностью рабочей. Везде где написано "добавить" — существующий Windows-код не трогаем.

---

## Контекст

Проект изначально разрабатывался и тестировался на Windows. Mac-поддержка была предусмотрена в архитектуре с самого начала (см. `PLAN.md`, `SPEC.md`), но не проверялась на практике. Гипотезы H2 (BlackHole) и H4 (mlx-whisper) отложены до работы на Mac.

**Ключевые архитектурные решения, принятые при разработке на Windows:**
- `pyannote.audio` принудительно на CPU — иначе CUDA OOM с whisper-large-v3
- Quality-модель (whisper-tiny) тоже CPU — CUDA context crash из ThreadPoolExecutor
- Chrome Native Messaging на Windows требует `.exe` (PyInstaller `--onedir`) — `.bat` не работает через CreateProcess
- `claude.exe` не в PATH; путь: `%APPDATA%\Claude\claude-code\<version>\claude.exe`, определяется через `wmic`
- `subprocess.run(["claude", ...])` на Windows падает с WinError 2 если Claude Code не в PATH — используем `config.CLAUDE_CLI`
- `sqlite3.Row` → всегда конвертировать в `dict()` перед `.get()`
- LLM вызывается через `claude -p` subprocess (подписка Claude.ai), Anthropic API намеренно не используется

**Находки при адаптации на Mac (CP1–CP7):**
- Системный Python macOS — 3.9; код использует `str | None` (Python 3.10+) — нужен Python 3.11 через `brew install python@3.11` + `brew install python-tk@3.11` (tkinter идёт отдельно)
- `torchaudio 2.11+` убрал `torchaudio.AudioMetaData` — ломает `pyannote.audio 3.3.2`; решение: обновить до `pyannote.audio==4.0.4` (4.x не использует этот API); тогда torch/torchaudio версии не принципиальны
- `requirements-mac.txt` изначально содержал `rumps` вместо `pystray` — исправлено
- На Mac Native Messaging хост — не `.exe`, а `.sh`-скрипт; создаётся динамически через `create_sh_launcher()` с путём к Python из текущего venv
- BlackHole работает на 48кГц / 2ch (не 44100/1 как было в коде) — исправлено в `audio_capture.py`
- macOS требует разрешение на микрофон даже для виртуальных устройств; диалог появляется только при запуске из Terminal.app напрямую, не из подпроцесса
- `pyannote.audio 3.3.2` несовместима с `huggingface_hub >= 0.23` (убран `use_auth_token=`) — решение: `pyannote.audio==4.0.4`
- pyannote 4.x не нужно явно передавать токен — берёт `HF_TOKEN` из окружения автоматически
- pystray на Mac требует main thread для NSStatusItem — нельзя запускать в фоновом потоке
- `root.mainloop()` отпускает GIL в C-коде Tk; PyObjC NSMenu callback в этот момент → SIGABRT. Решение: ручной цикл `nextEventMatchingMask + root.update()`
- `root.after()` из PyObjC callbacks небезопасен — нужна `SimpleQueue` как промежуточный буфер; main loop дренирует её между итерациями
- Claude Code на Mac: `~/Library/Application Support/Claude/claude-code-vm/<version>/claude`
- `os._exit(0)` нужен для выхода — `sys.exit()` и `ns_app.terminate_()` вызывают cleanup daemon-потоков → crash reporter
- На Mac кнопки tkinter игнорируют `bg`/`fg`/`relief="flat"` — нужно убирать эти параметры для нативного Aqua-вида

---

## Checkpoint 1 — Окружение и запуск

- [x] Создать `start_mac.sh` (аналог `start_windows.vbs`)
- [x] Проверить `requirements-mac.txt` — исправлено: `rumps` заменён на `pystray` + `Pillow`
- [x] Убедиться что CUDA DLL блок в `config.py` обёрнут в `IS_WINDOWS` — подтверждено
- [x] Добавить в `config.py` константу `UI_FONT`, использовать во всех UI-файлах (`dialogs.py`, `status_window.py`, `notifications.py`, `spectrum.py`)
- [x] Обновить `README.md` — добавлен mac-раздел

---

## Checkpoint 2 — Claude CLI detection

- [x] Расширить `_find_claude_cli()` в `config.py`: Windows-ветка — glob по `%APPDATA%\Claude\claude-code\*\claude.exe`; Mac-ветка — `~/.claude/local/claude`, `/usr/local/bin/claude`, `/opt/homebrew/bin/claude`; общий фолбек — `shutil.which` → `"claude"`
- [x] Обновить `.env.example` — добавлен пример mac-пути к `CLAUDE_CLI`

---

## Checkpoint 3 — Tray и файловые операции

- [x] В `tray.py` добавлена функция `_open_path()`: `os.startfile()` на Windows, `subprocess.run(["open", ...])` на Mac; оба вызова заменены
- [ ] Проверить pystray на macOS — убедиться что tray/menubar появляется и меню работает
- [x] Диалог выбора источника уже отображает имена устройств из системы — "BlackHole 2ch" на Mac, WASAPI-имя на Windows; хардкода нет

---

## Checkpoint 4 — Chrome Extension / Native Messaging

- [x] `for_meets_host.sh` создаётся динамически при вызове `install_mac()` через `create_sh_launcher()` — шебанг + правильный путь к Python из venv + `chmod +x`
- [x] `get_host_manifest()` на Mac вызывает `create_sh_launcher()`, на Windows — `get_exe_path()` (статический .exe)
- [x] `install_mac()` пишет манифест в `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/`
- [x] Прогнать `python -m app.extension.install_host` — создал `for_meets_host.sh` и manifest в `~/Library/.../NativeMessagingHosts/` ✓
- [x] `setup.py` — H1/H7 на Windows, H2/H4 на Mac — ветвление уже корректное, менять не нужно

---

## Checkpoint 5 — Аудио захват `[H2]`

- [x] Установить BlackHole-2ch
- [x] Настроить Multi-Output Device в Audio MIDI Setup (Динамики MacBook Pro + BlackHole 2ch)
- [x] Протестировать `_capture_blackhole()` — RMS=4177, звук захвачен успешно ✓
- [x] Зафиксировать результат H2 в `PLAN.md`

---

## Checkpoint 6 — Транскрипция `[H4]`

- [x] Убедиться что `mlx-whisper` установлен и работает — транскрибировал синусоиду → "ДИНАМИЧНАЯ МУЗЫКА" ✓
- [x] Проверить что `get_backend()` в `transcription/` выбирает `MLXWhisperBackend` на Mac, `FasterWhisperBackend` на Windows — подтверждено
- [x] Прогнать транскрипцию на тестовом WAV — закрыть H4 ✓
- [x] Проверить `pyannote.audio` диаризацию на Mac (CPU) — обновлено до 4.0.4, работает ✓
- [x] Зафиксировать результат H4 в `PLAN.md`

---

## Checkpoint 7 — End-to-end тест

- [x] Запустить через `start_mac.sh`, убедиться что tray появился ✓
- [x] Ручной запуск записи через tray-меню ✓
- [x] Транскрипция + диаризация отработали на реальном аудио ✓
- [x] Claude Manual Dialog — диалог появляется, кнопки работают ✓
- [x] Все 3 документа созданы (transcription, analysis, followup) ✓
- [x] Выход через tray-меню работает корректно ✓
- [ ] Автодетект через Chrome Extension — проверить на реальной Meet-встрече

---

## Бэклог — Windows-специфичные пункты (на потом)

- **B7** (запрет сна) — использует `ctypes.windll`, для Mac нужен `caffeinate` или `IOKit`. Пока пропускаем.
- **B12** (mute источника) — WASAPI-специфично. Mac-аналог отдельная задача.
