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

# На Mac добавляем Homebrew в PATH — нужен ffmpeg для mlx-whisper
if IS_MAC:
    for _brew_bin in ("/opt/homebrew/bin", "/usr/local/bin"):
        if _brew_bin not in os.environ.get("PATH", ""):
            os.environ["PATH"] = _brew_bin + os.pathsep + os.environ.get("PATH", "")

# UI
UI_FONT = "Segoe UI" if IS_WINDOWS else "Helvetica"

# Настройки захвата
CHUNK_DURATION_SEC = 30          # длина одного чанка
SILENCE_THRESHOLD_RMS = 100      # ниже — считается тишиной
QUALITY_THRESHOLD = 0.70         # confidence ниже этого → toast-предупреждение

# Whisper
WHISPER_MODEL = "large-v3"
WHISPER_LANGUAGE = "ru"

# Claude CLI — путь к исполняемому файлу
# Задаётся через CLAUDE_CLI в .env (например C:/Users/Oleg/.local/bin/claude.exe).
# Фолбек: платформо-зависимые типичные пути, затем PATH, затем "claude".
import glob as _glob
import shutil as _shutil


def _find_claude_cli() -> str:
    """Возвращает путь к claude CLI.

    Приоритет:
    1. CLAUDE_CLI в .env — явный путь
    2. Типичные пути установки для текущей платформы
    3. PATH (shutil.which)
    4. Фолбек: "claude"
    """
    if _env := os.getenv("CLAUDE_CLI"):
        if os.path.isfile(_env):
            return _env

    if IS_WINDOWS:
        # Claude Code на Windows устанавливается в %APPDATA%\Claude\claude-code\<version>\
        _appdata = os.environ.get("APPDATA", "")
        for _p in _glob.glob(os.path.join(_appdata, "Claude", "claude-code", "*", "claude.exe")):
            if os.path.isfile(_p):
                return _p
    elif IS_MAC:
        _mac_candidates = [
            os.path.expanduser("~/.claude/local/claude"),
            "/usr/local/bin/claude",
            "/opt/homebrew/bin/claude",
        ]
        for _p in _mac_candidates:
            if os.path.isfile(_p):
                return _p

    if _which := _shutil.which("claude"):
        return _which
    return "claude"


def ensure_dirs() -> None:
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    DOCUMENTS_DIR.mkdir(parents=True, exist_ok=True)
