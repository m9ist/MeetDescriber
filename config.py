"""
Загрузка конфигурации из .env файла.
"""
import os
import sys
import site
from pathlib import Path

from dotenv import load_dotenv

# На Windows Python 3.8+ DLL-поиск не включает site-packages автоматически.
# Регистрируем все nvidia/*/bin директории чтобы ctranslate2 нашёл cublas/cudnn.
if sys.platform == "win32":
    _all_site = site.getsitepackages() + [site.getusersitepackages()]
    _extra_paths = []
    for _sp in _all_site:
        _nvidia = os.path.join(_sp, "nvidia")
        if os.path.isdir(_nvidia):
            for _pkg in os.listdir(_nvidia):
                _bin = os.path.join(_nvidia, _pkg, "bin")
                if os.path.isdir(_bin):
                    _extra_paths.append(_bin)
                    os.add_dll_directory(_bin)
    if _extra_paths:
        os.environ["PATH"] = os.pathsep.join(_extra_paths) + os.pathsep + os.environ.get("PATH", "")

ROOT_DIR = Path(__file__).parent
DATA_DIR = ROOT_DIR / "data"
RECORDINGS_DIR = DATA_DIR / "recordings"
DOCUMENTS_DIR = DATA_DIR / "documents"
DB_PATH = DATA_DIR / "meets.db"

load_dotenv(ROOT_DIR / ".env", override=True)

HUGGINGFACE_TOKEN = os.getenv("HUGGINGFACE_TOKEN", "")

# Anthropic API намеренно не используется: корпоративные Google-аккаунты
# не позволяют подключать сторонние приложения через API.
# LLM вызывается через claude CLI (claude -p), который работает через
# подписку Claude.ai.

# faster-whisper и huggingface_hub читают HF_TOKEN из окружения напрямую
if HUGGINGFACE_TOKEN and not os.getenv("HF_TOKEN"):
    os.environ["HF_TOKEN"] = HUGGINGFACE_TOKEN

# Платформа
IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# Настройки захвата
CHUNK_DURATION_SEC = 30          # длина одного чанка
SILENCE_THRESHOLD_RMS = 100      # ниже — считается тишиной
QUALITY_THRESHOLD = 0.70         # confidence ниже этого → toast-предупреждение

# Whisper
WHISPER_MODEL = "large-v3"
WHISPER_LANGUAGE = "ru"

# Claude CLI — путь к исполняемому файлу
# Стратегия поиска (в порядке приоритета):
# 1. Переменная окружения CLAUDE_CLI
# 2. Запущенные процессы — claude должен быть запущен когда нужен (wmic)
# 3. PATH (shutil.which)
# 4. Glob по известным путям AppData
import shutil as _shutil
import subprocess as _subprocess


def _find_claude_from_processes() -> str:
    """Ищет доступный claude.exe через список запущенных процессов (wmic).

    Собирает уникальные пути всех claude-процессов и возвращает первый,
    для которого os.path.isfile() возвращает True (доступен из текущего процесса).
    """
    try:
        r = _subprocess.run(
            [
                "wmic", "process",
                "where", "name like '%claude%'",
                "get", "ExecutablePath", "/format:list",
            ],
            capture_output=True,
            timeout=10,
        )
        output = r.stdout.decode("utf-8", errors="replace")
        seen: set = set()
        for line in output.splitlines():
            line = line.strip()
            if not line.lower().startswith("executablepath="):
                continue
            path = line[len("executablepath="):].strip()
            if not path or path in seen:
                continue
            seen.add(path)
            if path.lower().endswith(".exe") and os.path.isfile(path):
                return path
    except Exception:
        pass
    return ""


def _find_claude_cli() -> str:
    """Возвращает путь к claude CLI.

    Приоритет:
    1. Переменная окружения CLAUDE_CLI (явный override)
    2. Живые процессы через wmic — берём первый доступный claude.exe
    3. PATH (shutil.which) — если claude добавлен в PATH
    4. Фолбек: "claude" (вызов упадёт с понятной ошибкой)
    """
    if _env := os.getenv("CLAUDE_CLI"):
        return _env
    if sys.platform == "win32":
        _proc = _find_claude_from_processes()
        if _proc:
            return _proc
    if _which := _shutil.which("claude"):
        return _which
    return "claude"


def ensure_dirs() -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
